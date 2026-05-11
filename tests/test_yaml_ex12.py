"""Validates that exercicios/1.2.yaml parses, has 6 criteria summing 100pts,
is multi-turma, and references registered primitives (US-12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.curriculum import parse_exercise_yaml
from app.primitives import registry

AUTOGRADE_ROOT = Path(__file__).resolve().parents[2]
EX12_PATH = AUTOGRADE_ROOT / "exercicios" / "1.2.yaml"


@pytest.fixture(scope="module")
def exercise():
    text = EX12_PATH.read_text(encoding="utf-8")
    return parse_exercise_yaml(text)


def test_yaml_file_exists():
    assert EX12_PATH.is_file(), f"esperado em {EX12_PATH}"


def test_basic_metadata(exercise):
    assert exercise.id == "1.2"
    assert exercise.titulo == "GitHub CLI"


def test_is_multi_turma(exercise):
    assert exercise.turmas == ("TD-2026-01", "MBA-IDP-2026")


def test_pesos_sum_to_100(exercise):
    total = sum(c.peso for c in exercise.criterios)
    assert total == 100, f"esperado 100, somou {total}"


def test_has_six_criterios(exercise):
    assert len(exercise.criterios) == 6


def test_criterio_ids_and_checks(exercise):
    by_id = {c.id: c for c in exercise.criterios}

    assert by_id["repo_publico"].check == "github.repo.public"
    assert by_id["repo_publico"].peso == 20

    pr_count = by_id["pelo_menos_1_pr"]
    assert pr_count.check == "github.pr.count"
    assert pr_count.peso == 20
    assert pr_count.args.get("state") == "all"
    assert int(pr_count.args.get("min")) == 1

    assert by_id["pr_titulo_descritivo"].check == "github.pr.has_descriptive_title"
    assert by_id["pr_titulo_descritivo"].peso == 20

    assert by_id["gh_authenticated"].check == "evidence.shell.gh_auth_ok"
    assert by_id["gh_authenticated"].peso == 15

    assert by_id["gh_version_capturado"].check == "evidence.shell.gh_version_present"
    assert by_id["gh_version_capturado"].peso == 10

    assert by_id["gh_repo_view_ok"].check == "evidence.shell.gh_repo_view_ok"
    assert by_id["gh_repo_view_ok"].peso == 15


def test_all_primitives_in_yaml_are_registered(exercise):
    for c in exercise.criterios:
        assert c.check in registry, f"primitive '{c.check}' nao registrada"
