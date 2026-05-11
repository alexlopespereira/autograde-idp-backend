"""Validates that exercicios/1.1.yaml parses, has expected metadata, and
distributes 100pts across exactly the seven Ex 1.1 primitives (US-11)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.curriculum import parse_exercise_yaml

AUTOGRADE_ROOT = Path(__file__).resolve().parents[2]
EX11_PATH = AUTOGRADE_ROOT / "exercicios" / "1.1.yaml"


@pytest.fixture(scope="module")
def exercise():
    text = EX11_PATH.read_text(encoding="utf-8")
    return parse_exercise_yaml(text)


def test_yaml_file_exists():
    assert EX11_PATH.is_file(), f"esperado em {EX11_PATH}"


def test_basic_metadata(exercise):
    assert exercise.id == "1.1"
    assert exercise.titulo == "Seu Primeiro Repositorio"
    assert exercise.turmas == ("TD-2026-01",)


def test_disponivel_a_partir_de(exercise):
    expected = datetime(2026, 3, 10, 8, 0, 0, tzinfo=timezone(timedelta(hours=-3)))
    assert exercise.disponivel_a_partir_de == expected


def test_prazo_recomendado_ate(exercise):
    raw = exercise.prazo.get("recomendado_ate")
    if isinstance(raw, str):
        raw = datetime.fromisoformat(raw)
    expected = datetime(2026, 3, 17, 23, 59, 59, tzinfo=timezone(timedelta(hours=-3)))
    assert raw == expected


def test_pesos_sum_to_100(exercise):
    total = sum(c.peso for c in exercise.criterios)
    assert total == 100, f"esperado 100, somou {total}"


def test_uses_only_seven_known_primitives(exercise):
    expected = {
        "github.repo.exists",
        "github.repo.public",
        "github.repo.has_file",
        "github.repo.file_not_empty",
        "github.repo.name_matches",
        "github.commits.count_at_least",
        "github.commits.last_within",
    }
    used = {c.check for c in exercise.criterios}
    assert used == expected


def test_each_primitive_referenced_at_least_once(exercise):
    used = [c.check for c in exercise.criterios]
    for name in (
        "github.repo.exists",
        "github.repo.public",
        "github.repo.has_file",
        "github.repo.file_not_empty",
        "github.repo.name_matches",
        "github.commits.count_at_least",
        "github.commits.last_within",
    ):
        assert name in used, f"primitive {name} nao referenciada"


def test_specific_args_match_acceptance_criteria(exercise):
    by_id = {c.id: c for c in exercise.criterios}

    readme_existe = by_id["readme_existe"]
    assert readme_existe.check == "github.repo.has_file"
    assert readme_existe.args.get("path") == "README.md"
    assert readme_existe.peso == 10

    readme_nao_vazio = by_id["readme_nao_vazio"]
    assert readme_nao_vazio.check == "github.repo.file_not_empty"
    assert readme_nao_vazio.args.get("path") == "README.md"
    assert readme_nao_vazio.peso == 10

    dois_commits = by_id["dois_commits"]
    assert dois_commits.check == "github.commits.count_at_least"
    assert int(dois_commits.args.get("n")) == 2
    assert dois_commits.peso == 15

    ultimo = by_id["ultimo_commit_recente"]
    assert ultimo.check == "github.commits.last_within"
    assert ultimo.args.get("duration") == "24h"
    assert ultimo.peso == 10

    nome = by_id["nome_repo"]
    assert nome.check == "github.repo.name_matches"
    assert nome.args.get("pattern") == "meu-primeiro-repo"
    assert nome.peso == 10


def test_all_primitives_in_yaml_are_registered(exercise):
    from app.primitives import registry

    for c in exercise.criterios:
        assert c.check in registry, f"primitive '{c.check}' nao registrada"
