from datetime import datetime

import pytest

from app.curriculum import (
    CurriculumValidationError,
    Pergunta,
    fetch_exercise,
    parse_exercise_yaml,
)

HAPPY_YAML = """
exercicio: "1.1"
titulo: "Seu Primeiro Repositorio"
turmas: ["TD-2026-01"]
disponivel_a_partir_de: "2026-03-10T08:00:00-03:00"
prazo:
  recomendado_ate: "2026-03-17T23:59:59-03:00"
criterios:
  - id: repo_publico
    peso: 10
    check: github.repo.public
  - id: readme_existe
    peso: 10
    check: github.repo.has_file
    args:
      path: "README.md"
"""


def test_parse_exercise_happy_path():
    ex = parse_exercise_yaml(HAPPY_YAML)
    assert ex.id == "1.1"
    assert ex.titulo == "Seu Primeiro Repositorio"
    assert ex.turmas == ("TD-2026-01",)
    assert isinstance(ex.disponivel_a_partir_de, datetime)
    assert ex.disponivel_a_partir_de.year == 2026
    assert ex.disponivel_a_partir_de.month == 3
    assert ex.prazo == {"recomendado_ate": "2026-03-17T23:59:59-03:00"}
    assert len(ex.criterios) == 2
    assert ex.criterios[0].id == "repo_publico"
    assert ex.criterios[0].peso == 10
    assert ex.criterios[0].args == {}
    assert ex.criterios[1].args == {"path": "README.md"}


def test_parse_exercise_yaml_malformado_raises():
    with pytest.raises(CurriculumValidationError, match="malformado|root|vazio"):
        parse_exercise_yaml("exercicio: '1.1'\n  bad-indent: oops\n - x")


def test_parse_exercise_datetime_yaml_native():
    # YAML parses unquoted ISO timestamps to native datetime
    yaml_text = HAPPY_YAML.replace(
        '"2026-03-10T08:00:00-03:00"',
        "2026-03-10T08:00:00-03:00",
    )
    ex = parse_exercise_yaml(yaml_text)
    assert isinstance(ex.disponivel_a_partir_de, datetime)
    assert ex.disponivel_a_partir_de.year == 2026


def test_parse_exercise_missing_required_field_raises():
    yaml_text = HAPPY_YAML.replace("titulo:", "titulox:")
    with pytest.raises(CurriculumValidationError, match="titulo"):
        parse_exercise_yaml(yaml_text)


def test_fetch_exercise_no_cache():
    calls = {"n": 0}

    def fetcher(url: str) -> str:
        calls["n"] += 1
        return HAPPY_YAML

    url = "https://example.com/1.1.yaml"
    ex1 = fetch_exercise(url, "1.1", fetcher=fetcher)
    ex2 = fetch_exercise(url, "1.1", fetcher=fetcher)
    assert calls["n"] == 2
    assert ex1.id == ex2.id == "1.1"


def test_fetch_exercise_id_mismatch_raises():
    def fetcher(url: str) -> str:
        return HAPPY_YAML

    with pytest.raises(CurriculumValidationError, match="exercicio.*1\\.2"):
        fetch_exercise("https://example.com/1.2.yaml", "1.2", fetcher=fetcher)


def test_parse_exercise_criterio_args_scalar_raises():
    yaml_text = """
exercicio: "1.1"
titulo: "T"
turmas: ["X"]
disponivel_a_partir_de: "2026-03-10T08:00:00-03:00"
prazo: {recomendado_ate: "2026-03-17T23:59:59-03:00"}
criterios:
  - id: c1
    peso: 10
    check: foo
    args: "string-invalido"
"""
    with pytest.raises(CurriculumValidationError, match="args.*mapping.*str"):
        parse_exercise_yaml(yaml_text)


def test_parse_exercise_without_perguntas_is_backward_compatible():
    ex = parse_exercise_yaml(HAPPY_YAML)
    assert ex.perguntas == ()


def test_parse_exercise_with_perguntas():
    yaml_text = HAPPY_YAML + """
perguntas:
  - texto: "O que você entendeu dos comandos?"
    criterios_avaliacao: "Aluno deve citar git init, add, commit, push e explicar cada um."
    peso: 10
  - texto: "Por que git é útil?"
    criterios_avaliacao: "Resposta deve mencionar versionamento e colaboração."
    peso: 5
"""
    ex = parse_exercise_yaml(yaml_text)
    assert len(ex.perguntas) == 2
    assert isinstance(ex.perguntas[0], Pergunta)
    assert ex.perguntas[0].texto == "O que você entendeu dos comandos?"
    assert "git init" in ex.perguntas[0].criterios_avaliacao
    assert ex.perguntas[0].peso == 10
    assert ex.perguntas[1].peso == 5


def test_parse_exercise_pergunta_missing_required_field_raises():
    yaml_text = HAPPY_YAML + """
perguntas:
  - texto: "Q1"
    peso: 5
"""
    with pytest.raises(CurriculumValidationError, match="criterios_avaliacao"):
        parse_exercise_yaml(yaml_text)


def test_parse_exercise_pergunta_empty_texto_raises():
    yaml_text = HAPPY_YAML + """
perguntas:
  - texto: "   "
    criterios_avaliacao: "x"
    peso: 5
"""
    with pytest.raises(CurriculumValidationError, match="texto vazio"):
        parse_exercise_yaml(yaml_text)


def test_parse_exercise_pergunta_peso_zero_raises():
    yaml_text = HAPPY_YAML + """
perguntas:
  - texto: "Q"
    criterios_avaliacao: "c"
    peso: 0
"""
    with pytest.raises(CurriculumValidationError, match="peso.*> 0"):
        parse_exercise_yaml(yaml_text)


def test_parse_exercise_perguntas_not_list_raises():
    yaml_text = HAPPY_YAML + """
perguntas: "uma string em vez de lista"
"""
    with pytest.raises(CurriculumValidationError, match="perguntas precisa ser lista"):
        parse_exercise_yaml(yaml_text)


def test_parse_exercise_criterio_args_list_raises():
    yaml_text = """
exercicio: "1.1"
titulo: "T"
turmas: ["X"]
disponivel_a_partir_de: "2026-03-10T08:00:00-03:00"
prazo: {recomendado_ate: "2026-03-17T23:59:59-03:00"}
criterios:
  - id: c1
    peso: 10
    check: foo
    args: [1, 2, 3]
"""
    with pytest.raises(CurriculumValidationError, match="args.*mapping.*list"):
        parse_exercise_yaml(yaml_text)
