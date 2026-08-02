"""Testes do módulo de curso — split/qualify do id e resolução de base URL."""

from __future__ import annotations

import pytest

from app.curso import (
    CURSO_DEFAULT,
    CursoError,
    curso_of,
    exercises_base_url,
    qualify_exercise_id,
    split_exercise_id,
)


def test_id_sem_prefixo_e_curso_default() -> None:
    # Compatibilidade: todo id histórico da Sheet e do idp_governodigital
    # continua sendo Transformação Digital.
    assert split_exercise_id("1.1") == ("td", "1.1")
    assert split_exercise_id("5.1") == ("td", "5.1")
    assert CURSO_DEFAULT == "td"


def test_id_com_prefixo_separa_curso_e_base() -> None:
    assert split_exercise_id("ia-1.1") == ("ia", "1.1")
    assert split_exercise_id("ia-1.4") == ("ia", "1.4")
    assert curso_of("ia-2.1") == "ia"


def test_split_ignora_espacos_ao_redor() -> None:
    assert split_exercise_id("  ia-1.2  ") == ("ia", "1.2")


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "ia-", "-1.1", "IA-1.1", "ia_1.1", "abcdefghi-1.1", "x-1.1", "foo"],
)
def test_id_malformado_levanta(bad: str) -> None:
    # Falhar explícito em vez de montar URL com lixo e devolver 404 confuso.
    with pytest.raises(CursoError):
        split_exercise_id(bad)


def test_qualify_e_inverso_de_split() -> None:
    assert qualify_exercise_id("td", "1.2") == "1.2"  # default sai sem prefixo
    assert qualify_exercise_id("ia", "1.2") == "ia-1.2"
    for eid in ("1.1", "ia-1.1", "4.2"):
        curso, base = split_exercise_id(eid)
        assert qualify_exercise_id(curso, base) == eid


def test_base_url_especifica_do_curso_tem_precedencia(monkeypatch) -> None:
    monkeypatch.setenv("EXERCISES_BASE_URL", "https://exemplo/td/")
    monkeypatch.setenv("EXERCISES_BASE_URL_IA", "https://exemplo/ia/")
    assert exercises_base_url("ia") == "https://exemplo/ia"
    assert exercises_base_url("td") == "https://exemplo/td"


def test_base_url_generica_e_fallback_de_qualquer_curso(monkeypatch) -> None:
    # Deployment atual (só EXERCISES_BASE_URL setada) segue funcionando.
    monkeypatch.delenv("EXERCISES_BASE_URL_IA", raising=False)
    monkeypatch.setenv("EXERCISES_BASE_URL", "https://exemplo/todos")
    assert exercises_base_url("td") == "https://exemplo/todos"
    assert exercises_base_url("ia") == "https://exemplo/todos"


def test_base_url_sem_env_alguma_levanta(monkeypatch) -> None:
    monkeypatch.delenv("EXERCISES_BASE_URL", raising=False)
    monkeypatch.delenv("EXERCISES_BASE_URL_IA", raising=False)
    with pytest.raises(CursoError):
        exercises_base_url("ia")


# --- roteamento de load_exercise por curso -----------------------------------

_YAML_TEMPLATE = """
exercicio: "{eid}"
titulo: "Seu Primeiro Repositorio"
turmas: ["IA-2026-02"]
disponivel_a_partir_de: "2026-03-10T08:00:00-03:00"
prazo:
  recomendado_ate: "2026-03-17T23:59:59-03:00"
criterios:
  - id: repo_publico
    peso: 10
    check: github.repo.public
"""


def _capture_fetcher(monkeypatch, eid: str) -> list[str]:
    from app import endpoints as endpoints_module

    urls: list[str] = []

    def fake_fetch(url: str) -> str:
        urls.append(url)
        return _YAML_TEMPLATE.format(eid=eid)

    monkeypatch.setattr(endpoints_module, "_http_fetcher", fake_fetch)
    return urls


def test_load_exercise_roteia_para_o_repo_do_curso(monkeypatch) -> None:
    from app.endpoints import load_exercise

    monkeypatch.setenv("EXERCISES_BASE_URL", "https://exemplo/td")
    monkeypatch.setenv("EXERCISES_BASE_URL_IA", "https://exemplo/ia")
    urls = _capture_fetcher(monkeypatch, "ia-1.1")

    exercise, _ = load_exercise("ia-1.1")

    # Repo do curso de agentes, e o arquivo mantém o id COMPLETO no nome.
    assert urls == ["https://exemplo/ia/ia-1.1.yaml"]
    assert exercise.id == "ia-1.1"


def test_load_exercise_curso_legado_usa_base_generica(monkeypatch) -> None:
    from app.endpoints import load_exercise

    monkeypatch.setenv("EXERCISES_BASE_URL", "https://exemplo/td")
    monkeypatch.setenv("EXERCISES_BASE_URL_IA", "https://exemplo/ia")
    urls = _capture_fetcher(monkeypatch, "1.1")

    load_exercise("1.1")

    assert urls == ["https://exemplo/td/1.1.yaml"]


def test_whitelist_shell_cobre_os_dois_cursos() -> None:
    # ia-1.2..1.4 reaproveitam a evidência `gh` do módulo de fundamentos;
    # ids não declarados (ex.: ia-4.1) continuam sem whitelist.
    from app.evidence.shell import _WHITELIST

    for base in ("1.2", "1.3", "1.4"):
        assert base in _WHITELIST
        assert f"ia-{base}" in _WHITELIST
        assert _WHITELIST[f"ia-{base}"] == _WHITELIST[base]
    assert "ia-4.1" not in _WHITELIST
