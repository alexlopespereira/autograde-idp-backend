"""Unit tests for the new primitives introduced by Ex 1.2 (US-12):
``github.pr.count`` and ``github.pr.has_descriptive_title``. Registration of
``evidence.shell.*`` is also checked; their real behavior is exercised in
``test_primitives_evidence_shell.py`` (US-14).
"""

from __future__ import annotations

from app.primitives import registry  # noqa: F401  -- triggers github + evidence_shell imports

# ---------- registration sanity --------------------------------------------


EX12_NEW_PRIMITIVES = (
    "github.pr.count",
    "github.pr.has_descriptive_title",
    "evidence.shell.gh_auth_ok",
    "evidence.shell.gh_version_present",
    "evidence.shell.gh_repo_view_ok",
)


def test_ex12_new_primitives_registered():
    missing = [name for name in EX12_NEW_PRIMITIVES if name not in registry]
    assert missing == []


# ---------- github.pr.count -------------------------------------------------


def _pr(number: int, title: str = "feat: add CLI", state: str = "open",
        merged_at: str | None = None) -> dict:
    return {"number": number, "title": title, "state": state, "merged_at": merged_at}


def test_pr_count_state_all_pass_with_min_1():
    evidence = {
        "prs_open": [_pr(1)],
        "prs_merged": [],
    }
    r = registry["github.pr.count"]({"_peso": 20, "state": "all", "min": 1}, evidence)
    assert r.passed is True
    assert r.points_earned == 20
    assert r.points_max == 20


def test_pr_count_state_all_fail_when_empty():
    evidence = {"prs_open": [], "prs_merged": []}
    r = registry["github.pr.count"]({"_peso": 20, "state": "all", "min": 1}, evidence)
    assert r.passed is False
    assert r.points_earned == 0


def test_pr_count_state_open_only_counts_open():
    evidence = {
        "prs_open": [_pr(1)],
        "prs_merged": [_pr(2, state="closed", merged_at="2026-01-01T00:00:00Z")],
    }
    r = registry["github.pr.count"]({"_peso": 10, "state": "open", "min": 2}, evidence)
    # only 1 open -> fails min=2
    assert r.passed is False


def test_pr_count_state_merged_only_counts_merged():
    evidence = {
        "prs_open": [_pr(1)],
        "prs_merged": [_pr(2, state="closed", merged_at="2026-01-01T00:00:00Z")],
    }
    r = registry["github.pr.count"]({"_peso": 10, "state": "merged", "min": 1}, evidence)
    assert r.passed is True


def test_pr_count_state_closed_counts_merged_plus_closed_unmerged():
    evidence = {
        "prs_open": [],
        "prs_merged": [_pr(2, state="closed", merged_at="2026-01-01T00:00:00Z")],
        "prs_closed_unmerged": [_pr(3, state="closed", title="abandoned")],
    }
    r = registry["github.pr.count"]({"_peso": 10, "state": "closed", "min": 2}, evidence)
    assert r.passed is True
    assert r.points_earned == 10


def test_pr_count_state_all_combines_open_merged_and_closed_unmerged():
    evidence = {
        "prs_open": [_pr(1)],
        "prs_merged": [_pr(2, state="closed", merged_at="2026-01-01T00:00:00Z")],
        "prs_closed_unmerged": [_pr(3, state="closed", title="abandoned")],
    }
    r = registry["github.pr.count"]({"_peso": 10, "state": "all", "min": 3}, evidence)
    assert r.passed is True


def test_pr_count_invalid_state():
    r = registry["github.pr.count"]({"_peso": 10, "state": "bogus", "min": 1}, {})
    assert r.passed is False
    assert "state" in r.message.lower()


def test_pr_count_invalid_min():
    r = registry["github.pr.count"]({"_peso": 10, "state": "all", "min": "abc"}, {})
    assert r.passed is False
    assert "min" in r.message.lower()


# ---------- github.pr.has_descriptive_title --------------------------------


def test_descriptive_title_pass_when_long_and_non_placeholder():
    evidence = {
        "prs_open": [_pr(1, title="feat: add device-code login flow")],
        "prs_merged": [],
    }
    r = registry["github.pr.has_descriptive_title"]({"_peso": 20}, evidence)
    assert r.passed is True
    assert r.points_earned == 20


def test_descriptive_title_fail_when_too_short():
    evidence = {"prs_open": [_pr(1, title="fix bug")], "prs_merged": []}
    r = registry["github.pr.has_descriptive_title"]({"_peso": 20}, evidence)
    assert r.passed is False
    assert r.points_earned == 0


def test_descriptive_title_fail_when_placeholder_update_readme():
    evidence = {"prs_open": [_pr(1, title="Update README")], "prs_merged": []}
    r = registry["github.pr.has_descriptive_title"]({"_peso": 20}, evidence)
    assert r.passed is False


def test_descriptive_title_fail_when_no_prs():
    evidence = {"prs_open": [], "prs_merged": []}
    r = registry["github.pr.has_descriptive_title"]({"_peso": 20}, evidence)
    assert r.passed is False
    assert "nenhum PR" in r.message


def test_descriptive_title_pass_from_merged_pool():
    evidence = {
        "prs_open": [],
        "prs_merged": [_pr(7, title="implement evidence-local validation",
                           state="closed", merged_at="2026-04-01T10:00:00Z")],
    }
    r = registry["github.pr.has_descriptive_title"]({"_peso": 20}, evidence)
    assert r.passed is True


def test_descriptive_title_strips_whitespace_before_length_check():
    evidence = {"prs_open": [_pr(1, title="   short   ")], "prs_merged": []}
    r = registry["github.pr.has_descriptive_title"]({"_peso": 20}, evidence)
    assert r.passed is False


def test_descriptive_title_custom_min_chars():
    evidence = {"prs_open": [_pr(1, title="fix bug")], "prs_merged": []}
    r = registry["github.pr.has_descriptive_title"]({"_peso": 20, "min_chars": 5}, evidence)
    assert r.passed is True


# evidence.shell.* behavior is covered in test_primitives_evidence_shell.py.
