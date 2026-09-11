"""Tests das primitives genéricas criadas para a Aula 3 (ia-3.1 e ia-3.2).

Todas são genéricas de propósito: o conteúdo do exercício mora no YAML. Os
testes aqui cobrem o contrato de cada uma (args, caminho feliz, caminho de
falha e args inválidos), não os valores de nenhum exercício específico.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.gemini import JudgeResult
from app.primitives import judge_llm, registry


def _entry(role: str, content: str = "", **fields: Any) -> dict[str, Any]:
    base = {
        "tool": "artifacts",
        "role": role,
        "path": f"{role}.md",
        "required": True,
        "exists": True,
        "size_bytes": len(content.encode("utf-8")),
        "word_count": len(content.split()),
        "sha256": "hash",
        "headings": [],
        "links": [],
        "content": content,
        "captured_at": "2026-05-16T12:00:00+00:00",
    }
    base.update(fields)
    return base


def _ev(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"artifacts": list(entries)}


def _run(nome: str, args: dict[str, Any], evidence: dict[str, Any]):
    return registry[nome]({"_peso": 10, **args}, evidence)


# ---------- evidence.artifacts.content_matches ------------------------------


def test_content_matches_conta_ocorrencias_e_usa_descricao():
    r = _run(
        "evidence.artifacts.content_matches",
        {"role": "sh", "pattern": r"^\s*for\s", "flags": "m", "descricao": "laço"},
        _ev(_entry("sh", "#!/bin/sh\nfor i in 1 2 3; do\n  echo $i\ndone\n")),
    )
    assert r.passed is True
    assert r.points_earned == 10
    assert "laço" in r.message


def test_content_matches_falha_quando_abaixo_do_min():
    r = _run(
        "evidence.artifacts.content_matches",
        {"role": "t", "pattern": r"Q\d{2}", "min": 16, "descricao": "perguntas"},
        _ev(_entry("t", "Q01 Q02 Q03")),
    )
    assert r.passed is False
    assert r.points_earned == 0
    assert "3 ocorrência(s)" in r.message
    assert ">= 16" in r.message


def test_content_matches_flag_i_e_ignorada_sem_flags():
    args = {"role": "p", "pattern": "grill-me"}
    ev = _ev(_entry("p", "Grill-Me@marketplace"))
    assert _run("evidence.artifacts.content_matches", args, ev).passed is False
    assert _run(
        "evidence.artifacts.content_matches", {**args, "flags": "i"}, ev
    ).passed is True


def test_content_matches_regex_invalida_vira_mensagem():
    r = _run(
        "evidence.artifacts.content_matches",
        {"role": "p", "pattern": "(nao fecha"},
        _ev(_entry("p", "x")),
    )
    assert r.passed is False
    assert "regex" in r.message.lower()


def test_content_matches_artefato_ausente():
    r = _run(
        "evidence.artifacts.content_matches",
        {"role": "sumido", "pattern": "x"},
        _ev(_entry("outro", "x")),
    )
    assert r.passed is False
    assert "sumido" in r.message


# ---------- evidence.artifacts.content_absent -------------------------------


def test_content_absent_passa_quando_padrao_nao_aparece():
    r = _run(
        "evidence.artifacts.content_absent",
        {"role": "p", "pattern": r"<PREENCHA[^>]*>", "descricao": "placeholder"},
        _ev(_entry("p", "protocolo preenchido de verdade")),
    )
    assert r.passed is True
    assert "ausente" in r.message


def test_content_absent_reprova_quando_padrao_aparece():
    r = _run(
        "evidence.artifacts.content_absent",
        {"role": "p", "pattern": r"TODO", "descricao": "TODO pendente"},
        _ev(_entry("p", "TODO: escrever\nTODO: revisar")),
    )
    assert r.passed is False
    assert "2 ocorrência(s)" in r.message


# ---------- evidence.artifacts.line_count_min -------------------------------


def test_line_count_min_ignora_linhas_vazias():
    r = _run(
        "evidence.artifacts.line_count_min",
        {"role": "r", "min": 5},
        _ev(_entry("r", "a\n\nb\n\n\nc\nd\n   \ne\n")),
    )
    assert r.passed is True
    assert "5 linha(s)" in r.message


def test_line_count_min_reprova_abaixo_do_piso():
    r = _run(
        "evidence.artifacts.line_count_min",
        {"role": "r", "min": 5},
        _ev(_entry("r", "a\nb\n")),
    )
    assert r.passed is False
    assert ">= 5" in r.message


def test_line_count_min_teto_opcional_reprova_acima():
    ev = _ev(_entry("r", "\n".join(str(i) for i in range(50))))
    assert _run(
        "evidence.artifacts.line_count_min", {"role": "r", "min": 5}, ev
    ).passed is True  # max=0 desliga o teto
    r = _run(
        "evidence.artifacts.line_count_min", {"role": "r", "min": 5, "max": 40}, ev
    )
    assert r.passed is False
    assert "<= 40" in r.message


# ---------- evidence.artifacts.csv_columns ----------------------------------

_PIVOT = "regiao,2026-01,2026-02\nSul,1.5,2.5\nSudeste,3.0,4.0\n"


def test_csv_columns_exact_confere_ordem():
    r = _run(
        "evidence.artifacts.csv_columns",
        {"role": "c", "exact": True, "columns": ["regiao", "2026-01", "2026-02"]},
        _ev(_entry("c", _PIVOT)),
    )
    assert r.passed is True


def test_csv_columns_exact_reprova_ordem_trocada():
    r = _run(
        "evidence.artifacts.csv_columns",
        {"role": "c", "exact": True, "columns": ["2026-01", "regiao", "2026-02"]},
        _ev(_entry("c", _PIVOT)),
    )
    assert r.passed is False
    assert "ordem" in r.message


def test_csv_columns_sem_exact_permite_colunas_extras():
    r = _run(
        "evidence.artifacts.csv_columns",
        {"role": "c", "columns": ["regiao", "2026-01"]},
        _ev(_entry("c", _PIVOT)),
    )
    assert r.passed is True


def test_csv_columns_lista_o_que_faltou():
    r = _run(
        "evidence.artifacts.csv_columns",
        {"role": "c", "columns": ["regiao", "2026-12"]},
        _ev(_entry("c", _PIVOT)),
    )
    assert r.passed is False
    assert "2026-12" in r.message


def test_csv_columns_delimiter_ponto_e_virgula():
    r = _run(
        "evidence.artifacts.csv_columns",
        {"role": "m", "delimiter": ";", "exact": True, "columns": ["autor", "ano"]},
        _ev(_entry("m", "autor;ano\nSilva;2020\n")),
    )
    assert r.passed is True


def test_csv_columns_tolera_bom_e_espacos_no_cabecalho():
    r = _run(
        "evidence.artifacts.csv_columns",
        {"role": "c", "exact": True, "columns": ["regiao", "2026-01"]},
        _ev(_entry("c", "﻿ regiao , 2026-01 \nSul,1\n")),
    )
    assert r.passed is True


def test_csv_columns_sem_columns_e_erro_de_args():
    r = _run("evidence.artifacts.csv_columns", {"role": "c"}, _ev(_entry("c", _PIVOT)))
    assert r.passed is False
    assert "columns" in r.message


# ---------- evidence.artifacts.csv_rows_min / csv_shape ---------------------


def test_csv_rows_min_nao_conta_o_cabecalho():
    r = _run(
        "evidence.artifacts.csv_rows_min",
        {"role": "c", "min": 2},
        _ev(_entry("c", _PIVOT)),
    )
    assert r.passed is True
    assert "2 linha(s) de dados" in r.message


def test_csv_rows_min_reprova_com_poucas_linhas():
    r = _run(
        "evidence.artifacts.csv_rows_min",
        {"role": "c", "min": 15},
        _ev(_entry("c", _PIVOT)),
    )
    assert r.passed is False


def test_csv_shape_confere_linhas_e_colunas():
    r = _run(
        "evidence.artifacts.csv_shape",
        {"role": "c", "rows": 2, "cols": 3},
        _ev(_entry("c", _PIVOT)),
    )
    assert r.passed is True
    assert "2x3" in r.message


def test_csv_shape_reporta_as_duas_dimensoes_erradas():
    r = _run(
        "evidence.artifacts.csv_shape",
        {"role": "c", "rows": 4, "cols": 7},
        _ev(_entry("c", _PIVOT)),
    )
    assert r.passed is False
    assert "2 linhas de dados" in r.message
    assert "3 colunas" in r.message


def test_csv_shape_csv_vazio():
    r = _run(
        "evidence.artifacts.csv_shape",
        {"role": "c", "rows": 4, "cols": 7},
        _ev(_entry("c", "\n\n")),
    )
    assert r.passed is False
    assert "vazio" in r.message


# ---------- evidence.artifacts.csv_sum_equals -------------------------------


def test_csv_sum_equals_soma_tudo_menos_a_primeira_coluna():
    r = _run(
        "evidence.artifacts.csv_sum_equals",
        {"role": "c", "expected": 11.0, "skip_first_col": True},
        _ev(_entry("c", _PIVOT)),
    )
    assert r.passed is True
    assert "11.00" in r.message


def test_csv_sum_equals_respeita_tolerancia():
    ev = _ev(_entry("c", _PIVOT))
    args = {"role": "c", "expected": 11.3, "skip_first_col": True}
    assert _run("evidence.artifacts.csv_sum_equals", args, ev).passed is False
    assert _run(
        "evidence.artifacts.csv_sum_equals", {**args, "tolerance": 0.5}, ev
    ).passed is True


def test_csv_sum_equals_aceita_formato_brasileiro():
    r = _run(
        "evidence.artifacts.csv_sum_equals",
        {"role": "c", "expected": 2469.12, "skip_first_col": True},
        _ev(_entry("c", "regiao,valor\nSul,\"1.234,56\"\nSudeste,\"1.234,56\"\n")),
    )
    assert r.passed is True


def test_csv_sum_equals_soma_apenas_colunas_pedidas():
    r = _run(
        "evidence.artifacts.csv_sum_equals",
        {"role": "c", "expected": 4.5, "columns": ["2026-01"]},
        _ev(_entry("c", _PIVOT)),
    )
    assert r.passed is True


def test_csv_sum_equals_coluna_inexistente():
    r = _run(
        "evidence.artifacts.csv_sum_equals",
        {"role": "c", "expected": 1, "columns": ["2026-99"]},
        _ev(_entry("c", _PIVOT)),
    )
    assert r.passed is False
    assert "nenhuma das colunas" in r.message


def test_csv_sum_equals_sem_celula_numerica():
    r = _run(
        "evidence.artifacts.csv_sum_equals",
        {"role": "c", "expected": 1},
        _ev(_entry("c", "a,b\nx,y\n")),
    )
    assert r.passed is False
    assert "numérica" in r.message


def test_csv_sum_equals_expected_obrigatorio():
    r = _run("evidence.artifacts.csv_sum_equals", {"role": "c"}, _ev(_entry("c", _PIVOT)))
    assert r.passed is False
    assert "expected" in r.message


# ---------- evidence.shell.pytest_passed ------------------------------------


def _shell(commands: dict[str, Any]) -> dict[str, Any]:
    return {
        "shell": {
            "gh_version": None,
            "gh_auth_ok": False,
            "gh_auth_user": None,
            "gh_repo_view": None,
            "commands_seen": [],
            "commands": commands,
        }
    }


def test_pytest_passed_conta_testes_verdes():
    r = _run(
        "evidence.shell.pytest_passed",
        {"extract": "pytest", "min_tests": 4},
        _shell({"pytest": {"stdout": "....  [100%]\n4 passed in 0.1s", "exit_code": 0}}),
    )
    assert r.passed is True
    assert "4 teste(s)" in r.message


def test_pytest_passed_reprova_abaixo_do_minimo():
    r = _run(
        "evidence.shell.pytest_passed",
        {"extract": "pytest", "min_tests": 4},
        _shell({"pytest": {"stdout": "2 passed in 0.1s", "exit_code": 0}}),
    )
    assert r.passed is False


def test_pytest_passed_reprova_com_falhas():
    r = _run(
        "evidence.shell.pytest_passed",
        {"extract": "pytest", "min_tests": 1},
        _shell({"pytest": {"stdout": "3 passed, 1 failed in 0.2s", "exit_code": 1}}),
    )
    assert r.passed is False
    assert "1" in r.message


def test_pytest_passed_extract_lista_usa_o_que_produziu_saida():
    """`python` e `python3` são declarados os dois no YAML; só um existe."""
    r = _run(
        "evidence.shell.pytest_passed",
        {"extract": ["pytest", "pytest3"], "min_tests": 4},
        _shell(
            {
                "pytest": {"stdout": "python not found in PATH", "exit_code": -1},
                "pytest3": {"stdout": "4 passed in 0.1s", "exit_code": 0},
            }
        ),
    )
    assert r.passed is True


def test_pytest_passed_interpretador_ausente_da_mensagem_util():
    r = _run(
        "evidence.shell.pytest_passed",
        {"extract": ["pytest", "pytest3"], "min_tests": 4},
        _shell(
            {
                "pytest": {"stdout": "python not found in PATH", "exit_code": -1},
                "pytest3": {"stdout": "python3 not found in PATH", "exit_code": -1},
            }
        ),
    )
    assert r.passed is False
    assert "PATH" in r.message


def test_pytest_passed_sem_comando_na_evidencia():
    r = _run("evidence.shell.pytest_passed", {"extract": "pytest"}, _shell({}))
    assert r.passed is False


# ---------- github.repo.files_matching_min ----------------------------------


def _gh(**fields: Any) -> dict[str, Any]:
    base = {"files_list": [], "commits": []}
    base.update(fields)
    return base


def test_files_matching_min_conta_glob():
    r = _run(
        "github.repo.files_matching_min",
        {"pattern": "tests/test_*.py", "min": 2, "descricao": "testes"},
        _gh(files_list=["tests/test_a.py", "tests/test_b.py", "src/main.py"]),
    )
    assert r.passed is True
    assert "testes: 2" in r.message


def test_files_matching_min_reprova_sem_match():
    r = _run(
        "github.repo.files_matching_min",
        {"pattern": "src/*", "min": 1},
        _gh(files_list=["README.md"]),
    )
    assert r.passed is False
    assert ">= 1" in r.message


def test_files_matching_min_pattern_obrigatorio():
    r = _run("github.repo.files_matching_min", {}, _gh())
    assert r.passed is False
    assert "pattern" in r.message


# ---------- github.commits.message_pattern_count ----------------------------


def test_message_pattern_count_conta_commits_do_loop():
    commits = [{"message": f"ralph: iter {i}"} for i in range(1, 6)]
    commits.append({"message": "docs: README"})
    r = _run(
        "github.commits.message_pattern_count",
        {"pattern": r"^ralph: iter \d+", "min": 5, "descricao": "iterações"},
        _gh(commits=commits),
    )
    assert r.passed is True
    assert "iterações: 5" in r.message


def test_message_pattern_count_reporta_quantos_inspecionou():
    r = _run(
        "github.commits.message_pattern_count",
        {"pattern": r"^ralph: iter \d+", "min": 5},
        _gh(commits=[{"message": "wip"}, {"message": "wip 2"}]),
    )
    assert r.passed is False
    assert "2 commits inspecionados" in r.message


def test_message_pattern_count_regex_invalida():
    r = _run(
        "github.commits.message_pattern_count", {"pattern": "(["}, _gh(commits=[])
    )
    assert r.passed is False
    assert "invalida" in r.message


# ---------- github.repo.no_secret_files -------------------------------------


@pytest.mark.parametrize(
    "path",
    [".env", "config/.env", "chave.pem", "app/credentials.json", "id_rsa", ".npmrc"],
)
def test_no_secret_files_pega_arquivo_suspeito(path: str):
    r = _run("github.repo.no_secret_files", {}, _gh(files_list=["README.md", path]))
    assert r.passed is False
    assert path in r.message


@pytest.mark.parametrize(
    "path", [".env.example", ".env.sample", "credentials.json.template"]
)
def test_no_secret_files_ignora_exemplos(path: str):
    r = _run("github.repo.no_secret_files", {}, _gh(files_list=["README.md", path]))
    assert r.passed is True


def test_no_secret_files_repo_limpo():
    r = _run(
        "github.repo.no_secret_files",
        {},
        _gh(files_list=["README.md", "src/main.py", "tests/test_main.py"]),
    )
    assert r.passed is True
    assert "3 do repo" in r.message


def test_no_secret_files_aceita_globs_extras_do_yaml():
    r = _run(
        "github.repo.no_secret_files",
        {"extra": ["*.sqlite"]},
        _gh(files_list=["dados.sqlite"]),
    )
    assert r.passed is False
    assert "dados.sqlite" in r.message


# ---------- github.file.first_commit_before ---------------------------------

_ARGS_ORDEM = {
    "path_a": "revisao/transcript.md",
    "path_b": "revisao/protocolo.md",
    "descricao": "transcript antes do protocolo",
}


def test_first_commit_before_ordem_correta():
    r = _run(
        "github.file.first_commit_before",
        _ARGS_ORDEM,
        {
            "file_first_commit": {
                "revisao/transcript.md": "2026-05-10T10:00:00+00:00",
                "revisao/protocolo.md": "2026-05-12T10:00:00+00:00",
            }
        },
    )
    assert r.passed is True
    assert "transcript antes do protocolo" in r.message


def test_first_commit_before_fora_de_ordem():
    r = _run(
        "github.file.first_commit_before",
        _ARGS_ORDEM,
        {
            "file_first_commit": {
                "revisao/transcript.md": "2026-05-12T10:00:00+00:00",
                "revisao/protocolo.md": "2026-05-10T10:00:00+00:00",
            }
        },
    )
    assert r.passed is False
    assert "fora de ordem" in r.message


def test_first_commit_before_mesmo_commit_explica_o_erro():
    """Commitar tudo de uma vez não prova ordem de trabalho — e a mensagem diz isso."""
    mesma_data = "2026-05-12T10:00:00+00:00"
    r = _run(
        "github.file.first_commit_before",
        _ARGS_ORDEM,
        {
            "file_first_commit": {
                "revisao/transcript.md": mesma_data,
                "revisao/protocolo.md": mesma_data,
            }
        },
    )
    assert r.passed is False
    assert "MESMO commit" in r.message


def test_first_commit_before_arquivo_sem_historico():
    r = _run(
        "github.file.first_commit_before",
        _ARGS_ORDEM,
        {"file_first_commit": {"revisao/protocolo.md": "2026-05-12T10:00:00+00:00"}},
    )
    assert r.passed is False
    assert "revisao/transcript.md" in r.message


def test_first_commit_before_sem_coleta_no_evidence():
    r = _run("github.file.first_commit_before", _ARGS_ORDEM, {})
    assert r.passed is False
    assert "nao coletadas" in r.message


def test_first_commit_before_paths_obrigatorios():
    r = _run("github.file.first_commit_before", {"path_a": "a"}, {})
    assert r.passed is False
    assert "path_b" in r.message


# ---------- judge.artifacts.rubric ------------------------------------------


def _stub_judge(monkeypatch, result: JudgeResult, capture: dict[str, Any] | None = None):
    def fake(rubrica_text, role, content, headings, word_count, n_links, **_kw):
        if capture is not None:
            capture.update(
                {"rubrica": rubrica_text, "role": role, "content": content}
            )
        return result

    monkeypatch.setattr(judge_llm, "grade_artifact", fake)


def test_rubric_monta_prompt_com_rubrica_e_sub_criterios(monkeypatch):
    capture: dict[str, Any] = {}
    _stub_judge(monkeypatch, JudgeResult(1.0, "ok", "", True), capture)
    r = _run(
        "judge.artifacts.rubric",
        {"role": "reflexao", "rubrica": "Avalie a reflexão.", "sub_criterios": ["A", "B"]},
        _ev(_entry("reflexao", "texto da reflexão")),
    )
    assert r.passed is True
    assert r.points_earned == 10
    assert "Avalie a reflexão." in capture["rubrica"]
    assert "- A" in capture["rubrica"] and "- B" in capture["rubrica"]
    assert capture["content"] == "texto da reflexão"


def test_rubric_multi_role_concatena_artefatos(monkeypatch):
    capture: dict[str, Any] = {}
    _stub_judge(monkeypatch, JudgeResult(1.0, "ok", "", True), capture)
    r = _run(
        "judge.artifacts.rubric",
        {"roles": ["readme", "reflexao"], "rubrica": "Avalie o join."},
        _ev(_entry("readme", "usei inner join"), _entry("reflexao", "cinco iterações")),
    )
    assert r.passed is True
    assert capture["role"] == "readme+reflexao"
    assert "usei inner join" in capture["content"]
    assert "cinco iterações" in capture["content"]


def test_rubric_score_parcial_e_proporcional(monkeypatch):
    _stub_judge(monkeypatch, JudgeResult(0.4, "raso", "faltou justificar", True))
    r = _run(
        "judge.artifacts.rubric",
        {"role": "reflexao", "rubrica": "Avalie."},
        _ev(_entry("reflexao", "x")),
    )
    assert r.passed is False
    assert r.points_earned == 4
    assert "faltou justificar" in r.message


def test_rubric_artefato_ausente_nao_chama_o_judge(monkeypatch):
    def explode(*_a, **_k):  # pragma: no cover - só falha se for chamado
        raise AssertionError("judge não deveria ser chamado sem artefato")

    monkeypatch.setattr(judge_llm, "grade_artifact", explode)
    r = _run(
        "judge.artifacts.rubric",
        {"roles": ["readme", "reflexao"], "rubrica": "Avalie."},
        _ev(_entry("readme", "só o readme")),
    )
    assert r.passed is False
    assert "reflexao" in r.message


def test_rubric_fallback_da_nota_maxima_provisoria(monkeypatch):
    _stub_judge(monkeypatch, JudgeResult(0.0, "", "gemini fora do ar", False))
    r = _run(
        "judge.artifacts.rubric",
        {"role": "reflexao", "rubrica": "Avalie."},
        _ev(_entry("reflexao", "x")),
    )
    assert r.passed is True
    assert r.points_earned == 10
    assert "PROVISÓRIA" in r.message


def test_rubric_sem_rubrica_nem_sub_criterios_e_erro_de_args():
    r = _run("judge.artifacts.rubric", {"role": "reflexao"}, _ev(_entry("reflexao", "x")))
    assert r.passed is False
    assert "rubrica" in r.message


def test_rubric_sem_role_e_erro_de_args():
    r = _run("judge.artifacts.rubric", {"rubrica": "Avalie."}, _ev())
    assert r.passed is False
    assert "role" in r.message


# ---------- evidence.shell.stdout_matches / stdout_absent -------------------


def test_stdout_matches_acha_o_nome_do_repo_privado():
    """`gh repo view` do próprio dono funciona em repo privado — e é evidência local."""
    r = _run(
        "evidence.shell.stdout_matches",
        {
            "extract": "gh_repo_view",
            "pattern": r'"name"\s*:\s*"dissertacao-revisao"',
            "descricao": "repo dissertacao-revisao acessível",
        },
        _shell(
            {
                "gh_repo_view": {
                    "stdout": '{"name":"dissertacao-revisao","visibility":"PRIVATE"}',
                    "exit_code": 0,
                }
            }
        ),
    )
    assert r.passed is True


def test_stdout_matches_respeita_min():
    r = _run(
        "evidence.shell.stdout_matches",
        {"extract": "c", "pattern": "grill-me", "min": 2},
        _shell({"c": {"stdout": "grill-me instalada", "exit_code": 0}}),
    )
    assert r.passed is False
    assert ">= 2" in r.message


def test_stdout_matches_flags():
    args = {"extract": "c", "pattern": "GRILL-ME"}
    ev = _shell({"c": {"stdout": "grill-me", "exit_code": 0}})
    assert _run("evidence.shell.stdout_matches", args, ev).passed is False
    assert _run("evidence.shell.stdout_matches", {**args, "flags": "i"}, ev).passed is True


def test_stdout_matches_aceita_lista_de_extracts():
    """`skills list` (projeto) e `skills list -g` (usuário) são comandos distintos.

    A skill pode ter sido instalada em qualquer um dos dois escopos; achar em um
    basta. Sem isso, o critério viraria uma pegadinha de flag do instalador.
    """
    ev = _shell(
        {
            "skills_projeto": {"stdout": "No project skills found.", "exit_code": 0},
            "skills_global": {
                "stdout": "  grill-me   ~/.agents/skills/grill-me\n",
                "exit_code": 0,
            },
        }
    )
    r = _run(
        "evidence.shell.stdout_matches",
        {"extract": ["skills_projeto", "skills_global"], "pattern": r"skills[\\/]grill-me"},
        ev,
    )
    assert r.passed is True


def test_stdout_matches_lista_ignora_rotulo_que_nao_rodou():
    """Um rótulo ausente não reprova enquanto outro da lista tiver rodado."""
    ev = _shell({"skills_global": {"stdout": "~/.agents/skills/prd\n", "exit_code": 0}})
    r = _run(
        "evidence.shell.stdout_matches",
        {"extract": ["skills_projeto", "skills_global"], "pattern": r"skills[\\/]prd"},
        ev,
    )
    assert r.passed is True


def test_stdout_matches_lista_toda_ausente_reprova():
    r = _run(
        "evidence.shell.stdout_matches",
        {"extract": ["a", "b"], "pattern": "x"},
        _shell({}),
    )
    assert r.passed is False
    assert "ausente" in r.message


def test_stdout_matches_comando_ausente():
    r = _run("evidence.shell.stdout_matches", {"extract": "c", "pattern": "x"}, _shell({}))
    assert r.passed is False
    assert "ausente" in r.message


def test_stdout_matches_regex_invalida():
    r = _run("evidence.shell.stdout_matches", {"extract": "c", "pattern": "(["}, _shell({}))
    assert r.passed is False
    assert "invalida" in r.message


def test_stdout_absent_passa_quando_nao_ha_segredo():
    r = _run(
        "evidence.shell.stdout_absent",
        {
            "extract": "git_ls",
            "pattern": r"(^|/)\.env$",
            "flags": "m",
            "descricao": ".env versionado",
        },
        _shell({"git_ls": {"stdout": "README.md\nrevisao/protocolo.md\n", "exit_code": 0}}),
    )
    assert r.passed is True


def test_stdout_absent_reprova_quando_o_padrao_aparece():
    r = _run(
        "evidence.shell.stdout_absent",
        {"extract": "git_ls", "pattern": r"(^|/)\.env$", "flags": "m"},
        _shell({"git_ls": {"stdout": "README.md\nconfig/.env\n", "exit_code": 0}}),
    )
    assert r.passed is False
    assert "deveria estar ausente" in r.message
