"""GitHub client wrapping PyGithub with PAT auth, rate-limit retry, and evidence bundling."""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable

from github import Auth, Github, GithubException, UnknownObjectException
from github.GithubException import RateLimitExceededException

log = logging.getLogger(__name__)

GITHUB_PAT_ENV = "GITHUB_PAT"
MAX_RATE_LIMIT_RETRIES = 10
MAX_COMMITS_COLLECTED = 50


class GitHubAPIError(Exception):
    """Raised on non-404 GitHub API errors (5xx or other 4xx)."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"GitHub API error {status_code}: {message}")
        self.status_code = status_code
        self.message = message


_HTTPS_PATTERN = re.compile(
    r"^https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/?#\s]+?)(?:\.git)?/?$"
)
_SSH_PATTERN = re.compile(
    r"^git@github\.com:(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?$"
)


def parse_repo_url(repo_url: str) -> str:
    """Normalize a GitHub URL to ``owner/repo``.

    Accepts ``https://github.com/owner/repo``, ``...repo.git``, and
    ``git@github.com:owner/repo.git``. Raises ``ValueError`` otherwise.
    """
    if not isinstance(repo_url, str) or not repo_url.strip():
        raise ValueError("repo_url precisa ser string nao vazia")
    candidate = repo_url.strip()
    for pat in (_HTTPS_PATTERN, _SSH_PATTERN):
        m = pat.match(candidate)
        if m:
            return f"{m.group('owner')}/{m.group('repo')}"
    raise ValueError(f"URL GitHub nao reconhecida: {repo_url!r}")


def _extract_reset_ts(exc: RateLimitExceededException) -> float:
    headers = getattr(exc, "headers", None) or {}
    for k, v in headers.items():
        if k.lower() == "x-ratelimit-reset":
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _call_with_rate_limit_retry(
    fn: Callable[[], Any],
    *,
    max_retries: int = MAX_RATE_LIMIT_RETRIES,
) -> Any:
    """Invoke ``fn``; on ``RateLimitExceededException`` sleep until
    ``X-RateLimit-Reset + 1s`` and retry up to ``max_retries`` times.
    """
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except RateLimitExceededException as exc:
            if attempt == max_retries:
                raise
            reset_ts = _extract_reset_ts(exc)
            wait_s = max(0.0, reset_ts - time.time()) + 1.0
            log.warning(
                "github rate-limit hit; sleeping %.1fs (attempt %d/%d)",
                wait_s,
                attempt + 1,
                max_retries,
            )
            time.sleep(wait_s)


class GitHubClient:
    """Thin wrapper around :class:`github.Github` with rate-limit retry."""

    def __init__(self, pat: str | None = None):
        if pat is None:
            pat = os.environ.get(GITHUB_PAT_ENV) or None
        if pat:
            self._github = Github(auth=Auth.Token(pat), retry=0)
        else:
            log.warning(
                "GITHUB_PAT ausente — usando auth anonimo (rate limit 60/h)"
            )
            self._github = Github(retry=0)
        self._pat = pat

    def repo(self, repo_url: str):
        owner_repo = parse_repo_url(repo_url)
        return _call_with_rate_limit_retry(lambda: self._github.get_repo(owner_repo))

    def commits(self, repo_url: str, n: int = 10) -> list[dict]:
        repo = self.repo(repo_url)

        def _fetch() -> list[dict]:
            out: list[dict] = []
            for i, c in enumerate(repo.get_commits()):
                if i >= n:
                    break
                out.append(_commit_to_dict(c))
            return out

        return _call_with_rate_limit_retry(_fetch)

    def files(self, repo_url: str, ref: str = "HEAD") -> list[str]:
        repo = self.repo(repo_url)

        def _fetch() -> list[str]:
            target = repo.default_branch if ref == "HEAD" else ref
            tree = repo.get_git_tree(target, recursive=True)
            return [t.path for t in tree.tree if t.type == "blob"]

        return _call_with_rate_limit_retry(_fetch)

    def get_file(self, repo_url: str, path: str) -> bytes | None:
        repo = self.repo(repo_url)

        def _fetch() -> bytes | None:
            try:
                content = repo.get_contents(path)
            except UnknownObjectException:
                return None
            if isinstance(content, list):
                return None
            return content.decoded_content

        return _call_with_rate_limit_retry(_fetch)

    def collect_evidence(self, repo_url: str) -> dict[str, Any]:
        owner_repo = parse_repo_url(repo_url)
        empty: dict[str, Any] = {
            "owner_repo": owner_repo,
            "repo_exists": False,
            "repo_public": False,
            "files_list": [],
            "file_sizes": {},
            "commits": [],
            "branches": [],
            "prs_open": [],
            "prs_merged": [],
        }
        try:
            repo = self.repo(repo_url)
        except UnknownObjectException:
            return empty
        except GithubException as exc:
            if exc.status == 404:
                return empty
            raise GitHubAPIError(exc.status, _msg_from_github_exc(exc)) from exc

        try:
            commits = self.commits(repo_url, n=MAX_COMMITS_COLLECTED)
            files, file_sizes = _call_with_rate_limit_retry(
                lambda: _files_and_sizes(repo)
            )
            branches = _call_with_rate_limit_retry(
                lambda: [b.name for b in repo.get_branches()]
            )
            prs_open = _call_with_rate_limit_retry(
                lambda: [_pr_to_dict(p) for p in repo.get_pulls(state="open")]
            )
            prs_closed = _call_with_rate_limit_retry(
                lambda: [_pr_to_dict(p) for p in repo.get_pulls(state="closed")]
            )
        except UnknownObjectException:
            return empty
        except GithubException as exc:
            if exc.status == 404:
                return empty
            if exc.status == 409:
                # "Git Repository is empty" — repo existe mas sem commits.
                # Evidencia vazia com repo_exists=True; a rubrica penaliza
                # ausencia de commits/arquivos normalmente (nao e' falha de infra).
                return {**empty, "repo_exists": True, "repo_public": not repo.private}
            raise GitHubAPIError(exc.status, _msg_from_github_exc(exc)) from exc

        prs_merged = [p for p in prs_closed if p["merged_at"] is not None]

        return {
            "owner_repo": owner_repo,
            "repo_exists": True,
            "repo_public": not repo.private,
            "files_list": files,
            "file_sizes": file_sizes,
            "commits": commits,
            "branches": branches,
            "prs_open": prs_open,
            "prs_merged": prs_merged,
        }


def _files_and_sizes(repo: Any) -> tuple[list[str], dict[str, int]]:
    target = repo.default_branch
    tree = repo.get_git_tree(target, recursive=True)
    files: list[str] = []
    sizes: dict[str, int] = {}
    for t in tree.tree:
        if t.type != "blob":
            continue
        files.append(t.path)
        raw_size = getattr(t, "size", None)
        try:
            sizes[t.path] = int(raw_size) if raw_size is not None else 0
        except (TypeError, ValueError):
            sizes[t.path] = 0
    return files, sizes


def _commit_to_dict(c: Any) -> dict:
    inner = getattr(c, "commit", None)
    author_email = ""
    committed_at: str | None = None
    message = ""
    if inner is not None:
        message = inner.message or ""
        author = getattr(inner, "author", None)
        if author is not None:
            author_email = author.email or ""
            if author.date is not None:
                committed_at = author.date.isoformat()
    return {
        "sha": c.sha,
        "message": message,
        "author_email": author_email,
        "committed_at": committed_at,
    }


def _pr_to_dict(p: Any) -> dict:
    return {
        "number": p.number,
        "title": p.title,
        "state": p.state,
        "merged_at": p.merged_at.isoformat() if p.merged_at else None,
    }


def _msg_from_github_exc(exc: GithubException) -> str:
    data = getattr(exc, "data", None)
    if isinstance(data, dict) and "message" in data:
        return str(data["message"])
    return str(data) if data is not None else str(exc)
