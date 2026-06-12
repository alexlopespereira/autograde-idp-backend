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


# ---------- http_json_match / json_list_* (4.1 + 4.2, execução real) ----------


def _shell_cmds(commands):
    """Contexto shell com o mapa genérico commands[extract]={stdout,exit_code,json}."""
    return {"shell": {"commands": commands}}


def test_new_primitives_registered():
    for name in (
        "evidence.shell.http_json_match",
        "evidence.shell.json_list_includes",
        "evidence.shell.json_list_min_len",
    ):
        assert name in registry


def test_http_json_match_pass_equals_and_present():
    r = registry["evidence.shell.http_json_match"](
        {
            "_peso": 25,
            "extract": "post_tarefa",
            "equals": {"titulo": "estudar APIs", "concluida": False},
            "present": ["id"],
        },
        _shell_cmds(
            {"post_tarefa": {"json": {"id": 1, "titulo": "estudar APIs", "concluida": False}}}
        ),
    )
    assert r.passed is True
    assert r.points_earned == 25
    assert r.points_max == 25


def test_http_json_match_fail_when_api_down_json_none():
    r = registry["evidence.shell.http_json_match"](
        {"_peso": 5, "extract": "health", "equals": {"status": "ok"}},
        _shell_cmds({"health": {"json": None, "stdout": "", "exit_code": 7}}),
    )
    assert r.passed is False
    assert r.points_earned == 0
    assert "no ar" in r.message


def test_http_json_match_fail_when_concluida_is_string():
    r = registry["evidence.shell.http_json_match"](
        {"_peso": 25, "extract": "put_tarefa", "equals": {"concluida": True}},
        _shell_cmds({"put_tarefa": {"json": {"id": 1, "concluida": "true"}}}),
    )
    assert r.passed is False
    assert "concluida" in r.message


def test_http_json_match_fail_when_command_not_captured():
    r = registry["evidence.shell.http_json_match"](
        {"_peso": 5, "extract": "health", "equals": {"status": "ok"}},
        _shell_cmds({}),
    )
    assert r.passed is False
    assert "health" in r.message


def test_http_json_match_fail_when_present_field_missing():
    r = registry["evidence.shell.http_json_match"](
        {"_peso": 25, "extract": "post_tarefa", "present": ["id"]},
        _shell_cmds({"post_tarefa": {"json": {"titulo": "x"}}}),
    )
    assert r.passed is False
    assert "id" in r.message


def test_http_json_match_nested_paths():
    r = registry["evidence.shell.http_json_match"](
        {
            "_peso": 30,
            "extract": "mcp_test",
            "equals": {
                "criar_resultado.titulo": "tarefa via mcp",
                "criar_resultado.concluida": False,
            },
            "present": ["criar_resultado.id"],
        },
        _shell_cmds(
            {
                "mcp_test": {
                    "json": {
                        "criar_resultado": {
                            "id": 1,
                            "titulo": "tarefa via mcp",
                            "concluida": False,
                        }
                    }
                }
            }
        ),
    )
    assert r.passed is True
    assert r.points_earned == 30


def test_json_list_includes_pass():
    r = registry["evidence.shell.json_list_includes"](
        {
            "_peso": 20,
            "extract": "mcp_test",
            "field": "tools",
            "includes": ["criar_tarefa", "listar_tarefas"],
        },
        _shell_cmds({"mcp_test": {"json": {"tools": ["criar_tarefa", "listar_tarefas"]}}}),
    )
    assert r.passed is True
    assert r.points_earned == 20


def test_json_list_includes_fail_when_missing_one():
    r = registry["evidence.shell.json_list_includes"](
        {
            "_peso": 20,
            "extract": "mcp_test",
            "field": "tools",
            "includes": ["criar_tarefa", "listar_tarefas"],
        },
        _shell_cmds({"mcp_test": {"json": {"tools": ["criar_tarefa"]}}}),
    )
    assert r.passed is False
    assert "listar_tarefas" in r.message


def test_json_list_includes_fail_when_not_a_list():
    r = registry["evidence.shell.json_list_includes"](
        {"_peso": 20, "extract": "mcp_test", "field": "tools", "includes": ["x"]},
        _shell_cmds({"mcp_test": {"json": {"tools": "criar_tarefa"}}}),
    )
    assert r.passed is False
    assert "lista" in r.message


def test_json_list_includes_fail_when_json_none():
    r = registry["evidence.shell.json_list_includes"](
        {"_peso": 20, "extract": "mcp_test", "field": "tools", "includes": ["x"]},
        _shell_cmds({"mcp_test": {"json": None}}),
    )
    assert r.passed is False


def test_json_list_min_len_pass():
    r = registry["evidence.shell.json_list_min_len"](
        {"_peso": 15, "extract": "mcp_test", "field": "listar_resultado", "min": 1},
        _shell_cmds({"mcp_test": {"json": {"listar_resultado": [{"id": 1}]}}}),
    )
    assert r.passed is True
    assert r.points_earned == 15


def test_json_list_min_len_fail_when_empty():
    r = registry["evidence.shell.json_list_min_len"](
        {"_peso": 15, "extract": "mcp_test", "field": "listar_resultado", "min": 1},
        _shell_cmds({"mcp_test": {"json": {"listar_resultado": []}}}),
    )
    assert r.passed is False
    assert "0" in r.message


def test_json_list_min_len_fail_when_not_a_list():
    r = registry["evidence.shell.json_list_min_len"](
        {"_peso": 15, "extract": "mcp_test", "field": "listar_resultado", "min": 1},
        _shell_cmds({"mcp_test": {"json": {"listar_resultado": {}}}}),
    )
    assert r.passed is False
