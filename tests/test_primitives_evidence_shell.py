"""Tests for evidence.shell.* primitives (US-14, AC4).

Each primitive consumes ``evidence['shell']`` which is the dict produced by
``validate_shell_evidence(...).to_evidence_dict()``. We test pass + fail paths
for the 3 primitives plus the missing-context defensive path.
"""

from __future__ import annotations

from app.primitives import registry


def _shell(**fields):
    base = {
        "gh_version": None,
        "gh_auth_ok": False,
        "gh_auth_user": None,
        "gh_repo_view": None,
        "commands_seen": [],
    }
    base.update(fields)
    return {"shell": base}


# ---------- gh_auth_ok --------------------------------------------------------


def test_gh_auth_ok_pass_when_context_flag_true():
    r = registry["evidence.shell.gh_auth_ok"](
        {"_peso": 15}, _shell(gh_auth_ok=True, gh_auth_user="fulano-gh")
    )
    assert r.passed is True
    assert r.points_earned == 15
    assert r.points_max == 15
    assert "fulano-gh" in r.message


def test_gh_auth_ok_fail_when_user_mismatch():
    r = registry["evidence.shell.gh_auth_ok"](
        {"_peso": 15}, _shell(gh_auth_ok=False, gh_auth_user="outro-user")
    )
    assert r.passed is False
    assert r.points_earned == 0
    assert "outro-user" in r.message


def test_gh_auth_ok_fail_when_no_user_extracted():
    r = registry["evidence.shell.gh_auth_ok"](
        {"_peso": 15}, _shell(gh_auth_ok=False, gh_auth_user=None)
    )
    assert r.passed is False
    assert "nao confirma" in r.message


def test_gh_auth_ok_fail_when_shell_context_missing():
    r = registry["evidence.shell.gh_auth_ok"]({"_peso": 15}, {})
    assert r.passed is False
    assert r.message == "shell_evidence ausente"


# ---------- gh_version_present ------------------------------------------------


def test_gh_version_present_pass_with_semver():
    r = registry["evidence.shell.gh_version_present"](
        {"_peso": 10}, _shell(gh_version="2.40.1")
    )
    assert r.passed is True
    assert r.points_earned == 10


def test_gh_version_present_pass_with_two_part_version():
    r = registry["evidence.shell.gh_version_present"](
        {"_peso": 10}, _shell(gh_version="2.40")
    )
    assert r.passed is True


def test_gh_version_present_fail_when_none():
    r = registry["evidence.shell.gh_version_present"](
        {"_peso": 10}, _shell(gh_version=None)
    )
    assert r.passed is False
    assert r.points_earned == 0


def test_gh_version_present_fail_when_garbage():
    r = registry["evidence.shell.gh_version_present"](
        {"_peso": 10}, _shell(gh_version="not-a-version")
    )
    assert r.passed is False


# ---------- gh_repo_view_ok ---------------------------------------------------


def test_gh_repo_view_ok_pass_with_public_named_repo():
    r = registry["evidence.shell.gh_repo_view_ok"](
        {"_peso": 15},
        _shell(gh_repo_view={"name": "projeto", "visibility": "PUBLIC"}),
    )
    assert r.passed is True
    assert r.points_earned == 15
    assert "projeto" in r.message


def test_gh_repo_view_ok_fail_when_private():
    r = registry["evidence.shell.gh_repo_view_ok"](
        {"_peso": 15},
        _shell(gh_repo_view={"name": "projeto", "visibility": "PRIVATE"}),
    )
    assert r.passed is False
    assert r.points_earned == 0
    assert "PRIVATE" in r.message


def test_gh_repo_view_ok_fail_when_view_none():
    r = registry["evidence.shell.gh_repo_view_ok"]({"_peso": 15}, _shell())
    assert r.passed is False
    assert "nao capturado" in r.message


def test_gh_repo_view_ok_fail_when_name_missing():
    r = registry["evidence.shell.gh_repo_view_ok"](
        {"_peso": 15}, _shell(gh_repo_view={"visibility": "PUBLIC"})
    )
    assert r.passed is False
    assert "name" in r.message
