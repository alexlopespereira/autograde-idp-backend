"""Unit tests for the 7 GitHub primitives used by Ex 1.1 (US-11).

Each primitive is exercised via the registry with a synthetic ``evidence``
dict shaped like the output of ``GitHubClient.collect_evidence``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.primitives import github as github_primitives
from app.primitives import registry

# ---------- fixtures --------------------------------------------------------


def _commit(sha: str, ts: datetime, message: str = "msg") -> dict:
    return {
        "sha": sha,
        "message": message,
        "author_email": "a@b.com",
        "committed_at": ts.isoformat(),
    }


@pytest.fixture
def now_utc() -> datetime:
    return datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def freeze_now(monkeypatch: pytest.MonkeyPatch, now_utc: datetime):
    monkeypatch.setattr(github_primitives, "_now_utc", lambda: now_utc)
    return now_utc


@pytest.fixture
def evidence_pass(now_utc: datetime) -> dict:
    return {
        "owner_repo": "fulano/meu-primeiro-repo",
        "repo_exists": True,
        "repo_public": True,
        "files_list": ["README.md", "src/main.py"],
        "file_sizes": {"README.md": 120, "src/main.py": 50},
        "commits": [
            _commit("c1", now_utc - timedelta(hours=1)),
            _commit("c2", now_utc - timedelta(days=2)),
        ],
        "branches": ["main"],
        "prs_open": [],
        "prs_merged": [],
    }


@pytest.fixture
def evidence_fail(now_utc: datetime) -> dict:
    return {
        "owner_repo": "fulano/projeto-errado",
        "repo_exists": False,
        "repo_public": False,
        "files_list": ["docs/index.md"],
        "file_sizes": {"docs/index.md": 0},
        "commits": [],
        "branches": [],
        "prs_open": [],
        "prs_merged": [],
    }


# ---------- registration sanity --------------------------------------------


EX11_PRIMITIVE_NAMES = (
    "github.repo.exists",
    "github.repo.public",
    "github.repo.has_file",
    "github.repo.file_not_empty",
    "github.repo.name_matches",
    "github.commits.count_at_least",
    "github.commits.last_within",
)


def test_all_seven_primitives_are_registered():
    missing = [name for name in EX11_PRIMITIVE_NAMES if name not in registry]
    assert missing == []


# ---------- github.repo.exists ---------------------------------------------


def test_repo_exists_pass(evidence_pass):
    r = registry["github.repo.exists"]({"_peso": 15}, evidence_pass)
    assert r.passed is True
    assert r.points_earned == 15
    assert r.points_max == 15


def test_repo_exists_fail(evidence_fail):
    r = registry["github.repo.exists"]({"_peso": 15}, evidence_fail)
    assert r.passed is False
    assert r.points_earned == 0
    assert r.points_max == 15


# ---------- github.repo.public ---------------------------------------------


def test_repo_public_pass(evidence_pass):
    r = registry["github.repo.public"]({"_peso": 10}, evidence_pass)
    assert r.passed is True
    assert r.points_earned == 10


def test_repo_public_fail_when_private(evidence_pass):
    private_evidence = {**evidence_pass, "repo_public": False}
    r = registry["github.repo.public"]({"_peso": 10}, private_evidence)
    assert r.passed is False
    assert r.points_earned == 0
    assert r.points_max == 10


# ---------- github.repo.has_file -------------------------------------------


def test_has_file_pass(evidence_pass):
    r = registry["github.repo.has_file"](
        {"_peso": 10, "path": "README.md"}, evidence_pass
    )
    assert r.passed is True
    assert r.points_earned == 10


def test_has_file_fail(evidence_pass):
    r = registry["github.repo.has_file"](
        {"_peso": 10, "path": "LICENSE"}, evidence_pass
    )
    assert r.passed is False
    assert r.points_earned == 0


def test_has_file_requires_path_arg(evidence_pass):
    r = registry["github.repo.has_file"]({"_peso": 10}, evidence_pass)
    assert r.passed is False
    assert "path" in r.message


# ---------- github.repo.file_not_empty -------------------------------------


def test_file_not_empty_pass(evidence_pass):
    r = registry["github.repo.file_not_empty"](
        {"_peso": 10, "path": "README.md"}, evidence_pass
    )
    assert r.passed is True
    assert r.points_earned == 10


def test_file_not_empty_fail_when_size_zero(evidence_fail):
    r = registry["github.repo.file_not_empty"](
        {"_peso": 10, "path": "docs/index.md"}, evidence_fail
    )
    assert r.passed is False
    assert r.points_earned == 0


def test_file_not_empty_fail_when_path_missing(evidence_pass):
    r = registry["github.repo.file_not_empty"](
        {"_peso": 10, "path": "naoexiste.txt"}, evidence_pass
    )
    assert r.passed is False


# ---------- github.repo.name_matches ---------------------------------------


def test_name_matches_pass_exact(evidence_pass):
    r = registry["github.repo.name_matches"](
        {"_peso": 10, "pattern": "meu-primeiro-repo"}, evidence_pass
    )
    assert r.passed is True
    assert r.points_earned == 10


def test_name_matches_pass_glob(evidence_pass):
    r = registry["github.repo.name_matches"](
        {"_peso": 10, "pattern": "meu-*-repo"}, evidence_pass
    )
    assert r.passed is True


def test_name_matches_fail(evidence_fail):
    r = registry["github.repo.name_matches"](
        {"_peso": 10, "pattern": "meu-primeiro-repo"}, evidence_fail
    )
    assert r.passed is False
    assert r.points_earned == 0


def test_name_matches_requires_owner_repo(evidence_pass):
    bad = {**evidence_pass, "owner_repo": ""}
    r = registry["github.repo.name_matches"](
        {"_peso": 10, "pattern": "anything"}, bad
    )
    assert r.passed is False


# ---------- github.commits.count_at_least ----------------------------------


def test_count_at_least_pass(evidence_pass):
    r = registry["github.commits.count_at_least"](
        {"_peso": 20, "n": 2}, evidence_pass
    )
    assert r.passed is True
    assert r.points_earned == 20


def test_count_at_least_fail(evidence_fail):
    r = registry["github.commits.count_at_least"](
        {"_peso": 20, "n": 1}, evidence_fail
    )
    assert r.passed is False
    assert r.points_earned == 0


def test_count_at_least_threshold_strict(evidence_pass):
    # evidence_pass has 2 commits; n=3 must fail
    r = registry["github.commits.count_at_least"](
        {"_peso": 15, "n": 3}, evidence_pass
    )
    assert r.passed is False


# ---------- github.commits.last_within -------------------------------------


def test_last_within_pass_24h(evidence_pass):
    r = registry["github.commits.last_within"](
        {"_peso": 10, "duration": "24h"}, evidence_pass
    )
    assert r.passed is True
    assert r.points_earned == 10


def test_last_within_fail_24h_when_all_old(now_utc: datetime):
    old_only = {
        "commits": [
            _commit("c1", now_utc - timedelta(days=5)),
            _commit("c2", now_utc - timedelta(days=10)),
        ],
    }
    r = registry["github.commits.last_within"](
        {"_peso": 10, "duration": "24h"}, old_only
    )
    assert r.passed is False
    assert r.points_earned == 0


def test_last_within_pass_with_days_unit(now_utc: datetime):
    evidence = {"commits": [_commit("c1", now_utc - timedelta(days=3))]}
    r = registry["github.commits.last_within"](
        {"_peso": 10, "duration": "7d"}, evidence
    )
    assert r.passed is True


def test_last_within_pass_with_weeks_unit(now_utc: datetime):
    evidence = {"commits": [_commit("c1", now_utc - timedelta(days=10))]}
    r = registry["github.commits.last_within"](
        {"_peso": 10, "duration": "2w"}, evidence
    )
    assert r.passed is True


def test_last_within_fail_when_no_commits(evidence_fail):
    r = registry["github.commits.last_within"](
        {"_peso": 10, "duration": "24h"}, evidence_fail
    )
    assert r.passed is False


def test_last_within_invalid_duration_returns_failure(evidence_pass):
    r = registry["github.commits.last_within"](
        {"_peso": 10, "duration": "tomorrow"}, evidence_pass
    )
    assert r.passed is False
    assert "duration" in r.message.lower()


def test_last_within_handles_zulu_timestamp(now_utc: datetime):
    # commits coming back from PyGithub carry "+00:00"; ensure 'Z' suffix also works
    evidence = {
        "commits": [
            {
                "sha": "z1",
                "message": "m",
                "author_email": "a@b.com",
                "committed_at": (now_utc - timedelta(hours=2)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
        ],
    }
    r = registry["github.commits.last_within"](
        {"_peso": 10, "duration": "24h"}, evidence
    )
    assert r.passed is True
