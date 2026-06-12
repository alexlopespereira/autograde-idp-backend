"""Tests for app.evidence.shell.validate_shell_evidence (US-14).

Covers:
- whitelist enforcement (tool, cmd_joined)
- captured_at time window (disponivel_a_partir_de .. submitted_at + 30min)
- gh auth user extraction + mismatch with expected roster username
- gh --version extraction
- gh repo view JSON parsing
- empty payload returns default context (no raise)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.curriculum import Criterio, Exercise
from app.evidence.shell import (
    InvalidShellEvidence,
    ShellEvidenceContext,
    validate_shell_evidence,
)

NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
DISPONIVEL = NOW - timedelta(days=7)


def _ex(exercicio_id: str = "1.2") -> Exercise:
    return Exercise(
        id=exercicio_id,
        titulo="GitHub CLI",
        turmas=("TD-2026-01",),
        disponivel_a_partir_de=DISPONIVEL,
        prazo={"recomendado_ate": NOW + timedelta(days=7)},
        criterios=(Criterio(id="x", peso=10, check="evidence.shell.gh_version_present", args={}),),
    )


def _cmd(
    cmd_joined: str,
    stdout: str = "",
    *,
    tool: str = "shell",
    captured_at: datetime | None = None,
    exit_code: int = 0,
) -> dict:
    return {
        "tool": tool,
        "cmd_joined": cmd_joined,
        "exit_code": exit_code,
        "stdout": stdout,
        "captured_at": (captured_at or NOW).isoformat(),
    }


# ---------- empty / shape -----------------------------------------------------


def test_empty_payload_returns_default_context():
    ctx = validate_shell_evidence(
        [], _ex(), expected_github_user="fulano", submitted_at=NOW
    )
    assert ctx == ShellEvidenceContext()
    assert ctx.gh_auth_ok is False
    assert ctx.gh_version is None
    assert ctx.gh_repo_view is None


def test_none_payload_returns_default_context():
    ctx = validate_shell_evidence(
        None, _ex(), expected_github_user="fulano", submitted_at=NOW
    )
    assert ctx.commands_seen == ()


def test_non_dict_item_raises():
    with pytest.raises(InvalidShellEvidence, match="precisa ser objeto"):
        validate_shell_evidence(
            ["not-a-dict"], _ex(), expected_github_user="fulano", submitted_at=NOW
        )


def test_tool_other_than_shell_rejected():
    payload = [_cmd("gh --version", tool="ai")]
    with pytest.raises(InvalidShellEvidence, match="tool="):
        validate_shell_evidence(
            payload, _ex(), expected_github_user="fulano", submitted_at=NOW
        )


# ---------- whitelist ---------------------------------------------------------


def test_whitelist_accepts_gh_version():
    payload = [_cmd("gh --version", stdout="gh version 2.40.1 (2024-01-01)\n")]
    ctx = validate_shell_evidence(
        payload, _ex(), expected_github_user="fulano", submitted_at=NOW
    )
    assert ctx.gh_version == "2.40.1"
    assert ctx.commands_seen == ("gh --version",)


def test_whitelist_rejects_unknown_command():
    payload = [_cmd("rm -rf /", stdout="oops")]
    with pytest.raises(InvalidShellEvidence, match="fora do whitelist"):
        validate_shell_evidence(
            payload, _ex(), expected_github_user="fulano", submitted_at=NOW
        )


def test_whitelist_rejects_gh_with_unknown_subcommand():
    payload = [_cmd("gh secret list", stdout="")]
    with pytest.raises(InvalidShellEvidence, match="fora do whitelist"):
        validate_shell_evidence(
            payload, _ex(), expected_github_user="fulano", submitted_at=NOW
        )


def test_whitelist_accepts_gh_repo_view_with_json_flag():
    stdout = '{"name":"projeto","visibility":"PUBLIC","isPrivate":false}'
    payload = [
        _cmd(
            "gh repo view fulano/projeto --json visibility,name,isPrivate", stdout=stdout
        )
    ]
    ctx = validate_shell_evidence(
        payload, _ex(), expected_github_user="fulano", submitted_at=NOW
    )
    assert ctx.gh_repo_view == {
        "name": "projeto",
        "visibility": "PUBLIC",
        "isPrivate": False,
    }


def test_whitelist_empty_for_unknown_exercise():
    payload = [_cmd("gh --version", stdout="gh version 2.40.0")]
    with pytest.raises(InvalidShellEvidence, match="fora do whitelist"):
        validate_shell_evidence(
            payload, _ex(exercicio_id="9.9"), expected_github_user="fulano", submitted_at=NOW
        )


# ---------- time window -------------------------------------------------------


def test_captured_at_before_disponivel_a_partir_de_rejected():
    too_early = DISPONIVEL - timedelta(seconds=1)
    payload = [_cmd("gh --version", stdout="gh version 2.0.0", captured_at=too_early)]
    with pytest.raises(InvalidShellEvidence, match="anterior a disponivel"):
        validate_shell_evidence(
            payload, _ex(), expected_github_user="fulano", submitted_at=NOW
        )


def test_captured_at_within_30min_skew_accepted():
    just_past = NOW + timedelta(minutes=29)
    payload = [_cmd("gh --version", stdout="gh version 2.0.0", captured_at=just_past)]
    ctx = validate_shell_evidence(
        payload, _ex(), expected_github_user="fulano", submitted_at=NOW
    )
    assert ctx.gh_version == "2.0.0"


def test_captured_at_more_than_30min_in_future_rejected():
    too_late = NOW + timedelta(minutes=31)
    payload = [_cmd("gh --version", stdout="gh version 2.0.0", captured_at=too_late)]
    with pytest.raises(InvalidShellEvidence, match="posterior a submitted_at"):
        validate_shell_evidence(
            payload, _ex(), expected_github_user="fulano", submitted_at=NOW
        )


def test_captured_at_missing_rejected():
    payload = [{"tool": "shell", "cmd_joined": "gh --version", "stdout": ""}]
    with pytest.raises(InvalidShellEvidence, match="captured_at"):
        validate_shell_evidence(
            payload, _ex(), expected_github_user="fulano", submitted_at=NOW
        )


def test_captured_at_naive_treated_as_utc():
    naive = datetime(2026, 5, 10, 12, 0, 0)
    payload = [
        {
            "tool": "shell",
            "cmd_joined": "gh --version",
            "stdout": "gh version 2.0.0",
            "captured_at": naive.isoformat(),
        }
    ]
    ctx = validate_shell_evidence(
        payload, _ex(), expected_github_user="fulano", submitted_at=NOW
    )
    assert ctx.gh_version == "2.0.0"


# ---------- gh auth extraction + user mismatch -------------------------------


def test_gh_auth_status_matches_expected_user():
    stdout = (
        "github.com\n"
        "  ✓ Logged in to github.com as fulano-gh (oauth_token)\n"
        "  ✓ Git operations for github.com configured to use https protocol.\n"
    )
    payload = [_cmd("gh auth status", stdout=stdout)]
    ctx = validate_shell_evidence(
        payload, _ex(), expected_github_user="fulano-gh", submitted_at=NOW
    )
    assert ctx.gh_auth_user == "fulano-gh"
    assert ctx.gh_auth_ok is True


def test_gh_auth_status_user_mismatch_keeps_user_but_not_ok():
    stdout = "✓ Logged in to github.com as outro-user (oauth_token)\n"
    payload = [_cmd("gh auth status", stdout=stdout)]
    ctx = validate_shell_evidence(
        payload, _ex(), expected_github_user="fulano-gh", submitted_at=NOW
    )
    assert ctx.gh_auth_user == "outro-user"
    assert ctx.gh_auth_ok is False


def test_gh_auth_status_case_insensitive_match():
    stdout = "Logged in to github.com as Fulano-GH"
    payload = [_cmd("gh auth status", stdout=stdout)]
    ctx = validate_shell_evidence(
        payload, _ex(), expected_github_user="fulano-gh", submitted_at=NOW
    )
    assert ctx.gh_auth_ok is True


def test_gh_auth_status_no_match_in_stdout():
    stdout = "You are not logged in to any GitHub hosts. Run gh auth login\n"
    payload = [_cmd("gh auth status", stdout=stdout, exit_code=1)]
    ctx = validate_shell_evidence(
        payload, _ex(), expected_github_user="fulano-gh", submitted_at=NOW
    )
    assert ctx.gh_auth_user is None
    assert ctx.gh_auth_ok is False


def test_gh_auth_status_account_variant_supported():
    stdout = "✓ Logged in to github.com account fulano-gh (keyring)\n"
    payload = [_cmd("gh auth status", stdout=stdout)]
    ctx = validate_shell_evidence(
        payload, _ex(), expected_github_user="fulano-gh", submitted_at=NOW
    )
    assert ctx.gh_auth_ok is True


# ---------- gh repo view parsing ---------------------------------------------


def test_gh_repo_view_non_json_yields_none():
    payload = [_cmd("gh repo view fulano/projeto", stdout="some prose, no json here")]
    ctx = validate_shell_evidence(
        payload, _ex(), expected_github_user="fulano", submitted_at=NOW
    )
    assert ctx.gh_repo_view is None


def test_gh_repo_view_malformed_json_yields_none():
    payload = [_cmd("gh repo view fulano/projeto --json visibility", stdout="{oops")]
    ctx = validate_shell_evidence(
        payload, _ex(), expected_github_user="fulano", submitted_at=NOW
    )
    assert ctx.gh_repo_view is None


# ---------- 4.1 / 4.2 commands map (execução real HTTP/MCP) -------------------

_CURL_POST_JOINED = (
    "curl -s -X POST http://localhost:8000/tarefas "
    "-H Content-Type: application/json "
    '-d {"titulo":"estudar APIs"}'
)


def _scmd(cmd_joined: str, stdout: str, extract: str, *, exit_code: int = 0) -> dict:
    return {
        "tool": "shell",
        "cmd_joined": cmd_joined,
        "exit_code": exit_code,
        "stdout": stdout,
        "extract": extract,
        "captured_at": NOW.isoformat(),
    }


def test_4_1_curl_commands_populate_commands_map():
    payload = [
        _scmd("curl -s http://localhost:8000/health", '{"status":"ok"}', "health"),
        _scmd(
            _CURL_POST_JOINED,
            '{"id":1,"titulo":"estudar APIs","concluida":false}',
            "post_tarefa",
        ),
    ]
    ctx = validate_shell_evidence(
        payload, _ex("4.1"), expected_github_user="fulano", submitted_at=NOW
    )
    ev = ctx.to_evidence_dict()
    assert "commands" in ev
    assert ev["commands"]["health"]["json"] == {"status": "ok"}
    assert ev["commands"]["post_tarefa"]["json"]["id"] == 1
    assert ev["commands"]["post_tarefa"]["exit_code"] == 0


def test_4_1_non_json_stdout_yields_json_none():
    payload = [
        _scmd(
            "curl -s http://localhost:8000/health",
            "curl: (7) Failed to connect",
            "health",
            exit_code=7,
        )
    ]
    ctx = validate_shell_evidence(
        payload, _ex("4.1"), expected_github_user="fulano", submitted_at=NOW
    )
    assert ctx.to_evidence_dict()["commands"]["health"]["json"] is None


def test_4_1_forged_command_outside_whitelist_rejected():
    payload = [_scmd("curl -s http://evil.example/steal", "{}", "health")]
    with pytest.raises(InvalidShellEvidence, match="fora do whitelist"):
        validate_shell_evidence(
            payload, _ex("4.1"), expected_github_user="fulano", submitted_at=NOW
        )


def test_4_2_python_client_command_accepted_and_parsed():
    envelope = (
        '{"tools":["criar_tarefa","listar_tarefas"],'
        '"criar_resultado":{"id":1,"titulo":"tarefa via mcp","concluida":false},'
        '"listar_resultado":[{"id":1,"titulo":"tarefa via mcp","concluida":false}]}'
    )
    payload = [_scmd("python cliente_teste.py", envelope, "mcp_test")]
    ctx = validate_shell_evidence(
        payload, _ex("4.2"), expected_github_user="fulano", submitted_at=NOW
    )
    cmds = ctx.to_evidence_dict()["commands"]
    assert cmds["mcp_test"]["json"]["tools"] == ["criar_tarefa", "listar_tarefas"]
    assert cmds["mcp_test"]["json"]["criar_resultado"]["id"] == 1


def test_4_2_forged_python_command_rejected():
    payload = [_scmd("python evil.py", "{}", "mcp_test")]
    with pytest.raises(InvalidShellEvidence, match="fora do whitelist"):
        validate_shell_evidence(
            payload, _ex("4.2"), expected_github_user="fulano", submitted_at=NOW
        )
