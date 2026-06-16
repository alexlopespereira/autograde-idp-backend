"""Tests pro parsing de perguntas tipo:sql + bloco dataset_sql."""

from __future__ import annotations

import pytest

from app.curriculum import CurriculumValidationError, parse_exercise_yaml

BASE = """
exercicio: "5.1"
titulo: "SQL via prompt"
turmas: ["TD-2026-01"]
disponivel_a_partir_de: "2026-05-01T00:00:00-03:00"
prazo:
  recomendado_ate: "2026-06-28T23:59:59-03:00"
criterios: []
"""


def _yaml(extra: str) -> str:
    return BASE + extra


def test_parses_sql_pergunta_with_dataset():
    ex = parse_exercise_yaml(
        _yaml(
            """
dataset_sql:
  schema: "CREATE TABLE t (v REAL);"
  seed: "INSERT INTO t VALUES (1),(2);"
perguntas:
  - tipo: sql
    texto: "Some os valores"
    query_referencia: "SELECT SUM(v) FROM t"
    peso: 20
"""
        )
    )
    assert ex.dataset_sql is not None
    assert "CREATE TABLE" in ex.dataset_sql.schema
    p = ex.perguntas[0]
    assert p.tipo == "sql"
    assert p.query_referencia == "SELECT SUM(v) FROM t"
    assert p.ordenado is False
    assert p.criterios_avaliacao == ""


def test_sql_pergunta_ordenado_flag():
    ex = parse_exercise_yaml(
        _yaml(
            """
dataset_sql:
  schema: "CREATE TABLE t (v REAL);"
perguntas:
  - tipo: sql
    texto: "Ordene"
    query_referencia: "SELECT v FROM t ORDER BY v"
    ordenado: true
    peso: 10
"""
        )
    )
    assert ex.perguntas[0].ordenado is True


def test_sql_pergunta_without_dataset_fails():
    with pytest.raises(CurriculumValidationError, match="dataset_sql"):
        parse_exercise_yaml(
            _yaml(
                """
perguntas:
  - tipo: sql
    texto: "Some"
    query_referencia: "SELECT SUM(v) FROM t"
    peso: 10
"""
            )
        )


def test_sql_pergunta_missing_query_referencia_fails():
    with pytest.raises(CurriculumValidationError, match="query_referencia"):
        parse_exercise_yaml(
            _yaml(
                """
dataset_sql:
  schema: "CREATE TABLE t (v REAL);"
perguntas:
  - tipo: sql
    texto: "Some"
    peso: 10
"""
            )
        )


def test_invalid_tipo_fails():
    with pytest.raises(CurriculumValidationError, match="tipo"):
        parse_exercise_yaml(
            _yaml(
                """
perguntas:
  - tipo: banana
    texto: "x"
    peso: 10
"""
            )
        )


def test_reflexao_still_requires_criterios():
    with pytest.raises(CurriculumValidationError, match="criterios_avaliacao"):
        parse_exercise_yaml(
            _yaml(
                """
perguntas:
  - texto: "reflita"
    peso: 10
"""
            )
        )


def test_reflexao_default_tipo_backward_compatible():
    ex = parse_exercise_yaml(
        _yaml(
            """
perguntas:
  - texto: "reflita"
    criterios_avaliacao: "cite X"
    peso: 10
"""
        )
    )
    assert ex.perguntas[0].tipo == "reflexao"
    assert ex.dataset_sql is None
