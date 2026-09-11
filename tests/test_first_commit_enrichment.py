"""Enriquecimento sob demanda de `evidence['file_first_commit']` (ia-3.2).

Só os paths citados por um critério `github.file.first_commit_before` viram
chamada extra à API — cada path custa uma requisição, então o YAML é quem
decide se o custo vale.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import endpoints
from app.curriculum import Criterio, Exercise
from app.github_client import GitHubAPIError

NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _ex(*criterios: Criterio) -> Exercise:
    return Exercise(
        id="ia-3.2",
        titulo="Grill-me",
        turmas=("IA-2026-01",),
        disponivel_a_partir_de=NOW - timedelta(days=7),
        prazo={"recomendado_ate": NOW + timedelta(days=7)},
        criterios=criterios,
    )


def _ordem(path_a: str, path_b: str) -> Criterio:
    return Criterio(
        id="ordem",
        peso=6,
        check="github.file.first_commit_before",
        args={"path_a": path_a, "path_b": path_b},
    )


class _FakeClient:
    def __init__(self, mapa: dict[str, str | None], erro: str | None = None):
        self.mapa = mapa
        self.erro = erro
        self.chamadas: list[str] = []

    def first_commit_at(self, repo_url: str, path: str) -> str | None:
        self.chamadas.append(path)
        if self.erro == path:
            raise GitHubAPIError(502, "boom")
        return self.mapa.get(path)


def test_paths_needing_first_commit_deduplica_e_ordena():
    ex = _ex(_ordem("b.md", "a.md"), _ordem("b.md", "c.md"))
    assert endpoints._paths_needing_first_commit(ex) == ["a.md", "b.md", "c.md"]


def test_paths_needing_first_commit_ignora_outros_checks():
    ex = _ex(Criterio(id="x", peso=10, check="github.repo.exists", args={}))
    assert endpoints._paths_needing_first_commit(ex) == []


def test_collect_first_commits_nao_chama_api_sem_criterio(monkeypatch: pytest.MonkeyPatch):
    def explode():  # pragma: no cover - só falha se for chamado
        raise AssertionError("não deveria instanciar cliente sem critério de ordem")

    monkeypatch.setattr(endpoints, "get_github_client", explode)
    ex = _ex(Criterio(id="x", peso=10, check="github.repo.exists", args={}))
    assert endpoints._collect_first_commits(ex, "https://github.com/a/b") == {}


def test_collect_first_commits_coleta_so_os_paths_citados(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeClient({"a.md": "2026-05-10T00:00:00+00:00", "b.md": "2026-05-11T00:00:00+00:00"})
    monkeypatch.setattr(endpoints, "get_github_client", lambda: fake)
    ex = _ex(_ordem("a.md", "b.md"))
    out = endpoints._collect_first_commits(ex, "https://github.com/a/b")
    assert out == {
        "a.md": "2026-05-10T00:00:00+00:00",
        "b.md": "2026-05-11T00:00:00+00:00",
    }
    assert fake.chamadas == ["a.md", "b.md"]


def test_collect_first_commits_falha_de_api_vira_none(monkeypatch: pytest.MonkeyPatch):
    """Erro de API não derruba a correção inteira: o critério de ordem reprova sozinho."""
    fake = _FakeClient({"b.md": "2026-05-11T00:00:00+00:00"}, erro="a.md")
    monkeypatch.setattr(endpoints, "get_github_client", lambda: fake)
    out = endpoints._collect_first_commits(_ex(_ordem("a.md", "b.md")), "https://github.com/a/b")
    assert out["a.md"] is None
    assert out["b.md"] == "2026-05-11T00:00:00+00:00"
