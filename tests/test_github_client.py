from __future__ import annotations

import logging
import re
import time

import pytest
import responses

from app.github_client import (
    GITHUB_PAT_ENV,
    GitHubAPIError,
    GitHubClient,
    parse_repo_url,
)

API = "https://api.github.com"


# ---------- helpers ---------------------------------------------------------


def _repo_payload(
    owner: str = "alice",
    name: str = "repo1",
    private: bool = False,
    default_branch: str = "main",
) -> dict:
    full = f"{owner}/{name}"
    return {
        "id": 1,
        "node_id": "n",
        "name": name,
        "full_name": full,
        "private": private,
        "owner": {"login": owner, "id": 1, "type": "User"},
        "html_url": f"https://github.com/{full}",
        "description": "",
        "fork": False,
        "url": f"{API}/repos/{full}",
        "default_branch": default_branch,
    }


def _commit_payload(
    sha: str,
    message: str = "hello",
    email: str = "a@b.com",
    date: str = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "sha": sha,
        "node_id": "c",
        "url": f"{API}/repos/alice/repo1/commits/{sha}",
        "html_url": f"https://github.com/alice/repo1/commit/{sha}",
        "commit": {
            "author": {"name": "A", "email": email, "date": date},
            "committer": {"name": "A", "email": email, "date": date},
            "message": message,
            "tree": {"sha": "t", "url": ""},
        },
        "author": None,
        "committer": None,
        "parents": [],
    }


def _re(path_suffix: str) -> re.Pattern:
    return re.compile(
        r"https://api\.github\.com(?::\d+)?" + re.escape(path_suffix) + r"(?:\?.*)?$"
    )


# ---------- URL parsing -----------------------------------------------------


class TestParseRepoUrl:
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://github.com/alice/my-repo", "alice/my-repo"),
            ("https://github.com/alice/my-repo.git", "alice/my-repo"),
            ("git@github.com:alice/my-repo.git", "alice/my-repo"),
            ("https://github.com/alice/my-repo/", "alice/my-repo"),
            ("  https://github.com/alice/my-repo  ", "alice/my-repo"),
        ],
    )
    def test_three_formats(self, url: str, expected: str) -> None:
        assert parse_repo_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "   ",
            "not-a-url",
            "https://gitlab.com/alice/repo",
            "https://github.com/alice",
        ],
    )
    def test_rejects_invalid(self, url: str) -> None:
        with pytest.raises(ValueError):
            parse_repo_url(url)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValueError):
            parse_repo_url(None)  # type: ignore[arg-type]


# ---------- PAT / env handling ---------------------------------------------


class TestPatHandling:
    def test_anonymous_warning_when_pat_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
        with caplog.at_level(logging.WARNING, logger="app.github_client"):
            client = GitHubClient()
        assert client._pat is None
        assert any("anonimo" in r.message.lower() for r in caplog.records)

    def test_uses_env_pat_when_not_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GITHUB_PAT_ENV, "envpat123")
        client = GitHubClient()
        assert client._pat == "envpat123"

    def test_explicit_pat_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GITHUB_PAT_ENV, "envpat")
        client = GitHubClient(pat="explicit")
        assert client._pat == "explicit"


# ---------- happy path ------------------------------------------------------


@responses.activate
def test_repo_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    responses.add(responses.GET, _re("/repos/alice/repo1"), json=_repo_payload(), status=200)

    client = GitHubClient()
    repo = client.repo("https://github.com/alice/repo1")
    assert repo.full_name == "alice/repo1"
    assert repo.private is False


@responses.activate
def test_commits_returns_dicts_with_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    responses.add(responses.GET, _re("/repos/alice/repo1"), json=_repo_payload(), status=200)
    responses.add(
        responses.GET,
        _re("/repos/alice/repo1/commits"),
        json=[
            _commit_payload("sha1", "first", "x@y.com", "2026-01-02T03:04:05Z"),
            _commit_payload("sha2", "second"),
        ],
        status=200,
    )

    client = GitHubClient()
    commits = client.commits("https://github.com/alice/repo1", n=10)
    assert len(commits) == 2
    assert commits[0]["sha"] == "sha1"
    assert commits[0]["message"] == "first"
    assert commits[0]["author_email"] == "x@y.com"
    assert commits[0]["committed_at"] == "2026-01-02T03:04:05+00:00"


@responses.activate
def test_commits_respects_n_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    responses.add(responses.GET, _re("/repos/alice/repo1"), json=_repo_payload(), status=200)
    responses.add(
        responses.GET,
        _re("/repos/alice/repo1/commits"),
        json=[_commit_payload(f"s{i}") for i in range(5)],
        status=200,
    )

    client = GitHubClient()
    commits = client.commits("https://github.com/alice/repo1", n=2)
    assert len(commits) == 2


@responses.activate
def test_files_uses_tree_recursive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    responses.add(responses.GET, _re("/repos/alice/repo1"), json=_repo_payload(), status=200)
    responses.add(
        responses.GET,
        _re("/repos/alice/repo1/git/trees/main"),
        json={
            "sha": "t",
            "url": "",
            "tree": [
                {"path": "README.md", "type": "blob", "sha": "b1", "url": ""},
                {"path": "src", "type": "tree", "sha": "tt", "url": ""},
                {"path": "src/x.py", "type": "blob", "sha": "b2", "url": ""},
            ],
            "truncated": False,
        },
        status=200,
    )

    client = GitHubClient()
    files = client.files("https://github.com/alice/repo1")
    assert files == ["README.md", "src/x.py"]


@responses.activate
def test_get_file_returns_decoded_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    responses.add(responses.GET, _re("/repos/alice/repo1"), json=_repo_payload(), status=200)
    content_b64 = base64.b64encode(b"hello world").decode("ascii")
    responses.add(
        responses.GET,
        _re("/repos/alice/repo1/contents/README.md"),
        json={
            "name": "README.md",
            "path": "README.md",
            "sha": "x",
            "size": 11,
            "type": "file",
            "encoding": "base64",
            "content": content_b64,
            "url": "",
            "html_url": "",
            "git_url": "",
            "download_url": "",
        },
        status=200,
    )

    client = GitHubClient()
    data = client.get_file("https://github.com/alice/repo1", "README.md")
    assert data == b"hello world"


@responses.activate
def test_get_file_returns_none_when_path_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    responses.add(responses.GET, _re("/repos/alice/repo1"), json=_repo_payload(), status=200)
    responses.add(
        responses.GET,
        _re("/repos/alice/repo1/contents/missing.txt"),
        json={"message": "Not Found"},
        status=404,
    )

    client = GitHubClient()
    assert client.get_file("https://github.com/alice/repo1", "missing.txt") is None


# ---------- 404 / error mapping --------------------------------------------


@responses.activate
def test_collect_evidence_returns_empty_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    responses.add(
        responses.GET,
        _re("/repos/alice/missing"),
        json={"message": "Not Found"},
        status=404,
    )

    client = GitHubClient()
    evidence = client.collect_evidence("https://github.com/alice/missing")
    assert evidence == {
        "owner_repo": "alice/missing",
        "repo_exists": False,
        "repo_public": False,
        "files_list": [],
        "file_sizes": {},
        "commits": [],
        "branches": [],
        "prs_open": [],
        "prs_merged": [],
    }


@responses.activate
def test_non_404_4xx_raises_github_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    responses.add(
        responses.GET,
        _re("/repos/alice/repo1"),
        json={"message": "Unprocessable"},
        status=422,
    )

    client = GitHubClient()
    with pytest.raises(GitHubAPIError) as ei:
        client.collect_evidence("https://github.com/alice/repo1")
    assert ei.value.status_code == 422
    assert "Unprocessable" in ei.value.message


# ---------- 403 rate-limit retry -------------------------------------------


@responses.activate
def test_retry_on_rate_limit_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    # avoid real sleeps
    slept: list[float] = []
    monkeypatch.setattr(
        "app.github_client.time.sleep", lambda s: slept.append(s)
    )

    reset_ts = int(time.time()) + 2
    responses.add(
        responses.GET,
        _re("/repos/alice/repo1"),
        json={"message": "API rate limit exceeded"},
        status=403,
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(reset_ts),
        },
    )
    responses.add(
        responses.GET,
        _re("/repos/alice/repo1"),
        json=_repo_payload(),
        status=200,
    )

    client = GitHubClient()
    repo = client.repo("https://github.com/alice/repo1")
    assert repo.full_name == "alice/repo1"
    # one of the sleeps must be ours (>= 1s, until X-RateLimit-Reset)
    assert any(s >= 1.0 for s in slept), f"expected >=1s sleep, got {slept}"
    assert len(responses.calls) == 2


@responses.activate
def test_retry_gives_up_after_max_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from github.GithubException import RateLimitExceededException

    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    monkeypatch.setattr("app.github_client.time.sleep", lambda s: None)

    reset_ts = int(time.time()) + 1
    for _ in range(3):  # 1 initial + 2 retries = 3 attempts
        responses.add(
            responses.GET,
            _re("/repos/alice/repo1"),
            json={"message": "API rate limit exceeded"},
            status=403,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset_ts),
            },
        )

    client = GitHubClient()
    with pytest.raises(RateLimitExceededException):
        client.repo("https://github.com/alice/repo1")


# ---------- collect_evidence full bundle -----------------------------------


@responses.activate
def test_collect_evidence_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    monkeypatch.setattr("app.github_client.time.sleep", lambda s: None)

    responses.add(
        responses.GET,
        _re("/repos/alice/repo1"),
        json=_repo_payload(private=False),
        status=200,
    )
    responses.add(
        responses.GET,
        _re("/repos/alice/repo1/commits"),
        json=[_commit_payload("sha1"), _commit_payload("sha2")],
        status=200,
    )
    responses.add(
        responses.GET,
        _re("/repos/alice/repo1/git/trees/main"),
        json={
            "sha": "t",
            "url": "",
            "tree": [
                {"path": "README.md", "type": "blob", "sha": "b1", "url": "", "size": 42},
            ],
            "truncated": False,
        },
        status=200,
    )
    responses.add(
        responses.GET,
        _re("/repos/alice/repo1/branches"),
        json=[
            {"name": "main", "commit": {"sha": "x", "url": ""}, "protected": False},
            {"name": "dev", "commit": {"sha": "y", "url": ""}, "protected": False},
        ],
        status=200,
    )
    responses.add(
        responses.GET,
        _re("/repos/alice/repo1/pulls"),
        json=[
            {
                "id": 1,
                "number": 7,
                "state": "open",
                "title": "open pr",
                "url": "",
                "html_url": "",
                "merged_at": None,
            }
        ],
        status=200,
    )
    responses.add(
        responses.GET,
        _re("/repos/alice/repo1/pulls"),
        json=[
            {
                "id": 2,
                "number": 5,
                "state": "closed",
                "title": "merged pr",
                "url": "",
                "html_url": "",
                "merged_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": 3,
                "number": 6,
                "state": "closed",
                "title": "closed-not-merged",
                "url": "",
                "html_url": "",
                "merged_at": None,
            },
        ],
        status=200,
    )

    client = GitHubClient()
    evidence = client.collect_evidence("https://github.com/alice/repo1")

    assert evidence["owner_repo"] == "alice/repo1"
    assert evidence["repo_exists"] is True
    assert evidence["repo_public"] is True
    assert evidence["files_list"] == ["README.md"]
    assert evidence["file_sizes"] == {"README.md": 42}
    assert [c["sha"] for c in evidence["commits"]] == ["sha1", "sha2"]
    assert evidence["branches"] == ["main", "dev"]
    assert [p["number"] for p in evidence["prs_open"]] == [7]
    assert [p["number"] for p in evidence["prs_merged"]] == [5]
