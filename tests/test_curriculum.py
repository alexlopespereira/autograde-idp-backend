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


# ---------- artefatos: e comandos_shell: (Aula 3) ---------------------------

_BASE_YAML = """
exercicio: "ia-3.1"
titulo: "T"
turmas: ["X"]
disponivel_a_partir_de: "2026-03-10T08:00:00-03:00"
prazo: {recomendado_ate: "2026-03-17T23:59:59-03:00"}
criterios:
  - id: c1
    peso: 10
    check: github.repo.exists
"""


def test_parse_exercise_sem_artefatos_nem_comandos():
    ex = parse_exercise_yaml(_BASE_YAML)
    assert ex.artefatos == ()
    assert ex.comandos_shell == ()


def test_parse_artefatos_le_role_path_e_required():
    ex = parse_exercise_yaml(
        _BASE_YAML
        + """
artefatos:
  - role: reflexao
    path: RALPH.md
  - role: extra
    path: notas.md
    required: false
"""
    )
    assert [(a.role, a.path, a.required) for a in ex.artefatos] == [
        ("reflexao", "RALPH.md", True),
        ("extra", "notas.md", False),
    ]


def test_parse_artefatos_role_duplicado_falha():
    yaml_text = _BASE_YAML + """
artefatos:
  - role: reflexao
    path: A.md
  - role: reflexao
    path: B.md
"""
    with pytest.raises(CurriculumValidationError, match="duplicado"):
        parse_exercise_yaml(yaml_text)


@pytest.mark.parametrize(
    "bloco,erro",
    [
        ("artefatos: {role: a}", "artefatos precisa ser lista"),
        ("artefatos:\n  - path: A.md", "campo 'role' faltante"),
        ("artefatos:\n  - role: a", "campo 'path' faltante"),
        ("artefatos:\n  - {role: ' ', path: A.md}", "role vazio"),
        ("artefatos:\n  - {role: a, path: ' '}", "path vazio"),
    ],
)
def test_parse_artefatos_invalidos(bloco: str, erro: str):
    with pytest.raises(CurriculumValidationError, match=erro):
        parse_exercise_yaml(_BASE_YAML + "\n" + bloco + "\n")


def test_parse_comandos_shell_aceita_argv_puro_e_mapping():
    ex = parse_exercise_yaml(
        _BASE_YAML
        + """
comandos_shell:
  - ["gh", "--version"]
  - cmd: ["python", "-m", "pytest", "-q"]
    extract: pytest
"""
    )
    assert ex.comandos_shell[0].cmd == ("gh", "--version")
    assert ex.comandos_shell[0].extract == ""
    assert ex.comandos_shell[1].cmd == ("python", "-m", "pytest", "-q")
    assert ex.comandos_shell[1].extract == "pytest"


@pytest.mark.parametrize(
    "bloco,erro",
    [
        ("comandos_shell: gh --version", "comandos_shell precisa ser lista"),
        ("comandos_shell:\n  - cmd: []", "lista nao vazia"),
        ("comandos_shell:\n  - cmd: 'gh --version'", "lista nao vazia"),
        ("comandos_shell:\n  - ['gh', '']", "token vazio"),
    ],
)
def test_parse_comandos_shell_invalidos(bloco: str, erro: str):
    with pytest.raises(CurriculumValidationError, match=erro):
        parse_exercise_yaml(_BASE_YAML + "\n" + bloco + "\n")


# --- requer_repositorio ---------------------------------------------------
# O default é `true` e vale para todo YAML já no ar: exercício de git (aula 1)
# não declara nada e continua exigindo repo. `false` desliga a exigência, e o
# parser recusa o YAML que declara `false` mas ainda depende do repo — essa
# contradição, sem a checagem, vira nota zero silenciosa em produção.

SEM_REPO_YAML = """
exercicio: "ia-9.9"
titulo: "Exercicio sem repositorio"
turmas: ["IA-2026-01"]
disponivel_a_partir_de: "2026-03-10T08:00:00-03:00"
requer_repositorio: false
prazo:
  recomendado_ate: "2026-03-17T23:59:59-03:00"
artefatos:
  - role: ensaio
    path: ensaio.md
comandos_shell:
  - cmd: ["gh", "auth", "status"]
    extract: gh_auth
criterios:
  - id: gh_autenticado
    peso: 10
    check: evidence.shell.gh_auth_ok
  - id: ensaio_existe
    peso: 10
    check: evidence.artifacts.exists
    args:
      role: ensaio
"""


def test_requer_repositorio_default_true():
    assert parse_exercise_yaml(HAPPY_YAML).requer_repositorio is True


def test_requer_repositorio_false_e_aceito():
    ex = parse_exercise_yaml(SEM_REPO_YAML)
    assert ex.requer_repositorio is False
    # `gh auth status` sobrevive: é a amarra de identidade, e não precisa de repo.
    assert ex.comandos_shell[0].cmd == ("gh", "auth", "status")


def test_requer_repositorio_true_explicito():
    yaml_text = HAPPY_YAML.replace(
        'titulo: "Seu Primeiro Repositorio"',
        'titulo: "Seu Primeiro Repositorio"\nrequer_repositorio: true',
    )
    assert parse_exercise_yaml(yaml_text).requer_repositorio is True


def test_requer_repositorio_nao_booleano_raises():
    # `"false"` é string truthy: sem validação estrita o exercício voltaria a
    # exigir repo sem ninguém perceber.
    yaml_text = SEM_REPO_YAML.replace(
        "requer_repositorio: false", 'requer_repositorio: "false"'
    )
    with pytest.raises(CurriculumValidationError, match="requer_repositorio.*boolean"):
        parse_exercise_yaml(yaml_text)


def test_requer_repositorio_false_com_criterio_github_raises():
    yaml_text = SEM_REPO_YAML.replace(
        "  - id: ensaio_existe\n    peso: 10\n    check: evidence.artifacts.exists\n"
        "    args:\n      role: ensaio\n",
        "  - id: repo_publico\n    peso: 10\n    check: github.repo.public\n",
    )
    with pytest.raises(CurriculumValidationError, match="github"):
        parse_exercise_yaml(yaml_text)


def test_requer_repositorio_false_com_owner_repo_raises():
    yaml_text = SEM_REPO_YAML.replace(
        '  - cmd: ["gh", "auth", "status"]\n    extract: gh_auth\n',
        '  - cmd: ["gh", "repo", "view", "{owner_repo}"]\n    extract: gh_repo_view\n',
    )
    with pytest.raises(CurriculumValidationError, match="owner_repo"):
        parse_exercise_yaml(yaml_text)


def test_requer_repositorio_true_mantem_github_e_owner_repo():
    # A checagem só vale quando `false`: exercício com repo segue usando os dois.
    yaml_text = SEM_REPO_YAML.replace(
        "requer_repositorio: false", "requer_repositorio: true"
    ).replace(
        '  - cmd: ["gh", "auth", "status"]\n    extract: gh_auth\n',
        '  - cmd: ["gh", "repo", "view", "{owner_repo}"]\n    extract: gh_repo_view\n',
    ).replace(
        "check: evidence.shell.gh_auth_ok", "check: github.repo.public"
    )
    ex = parse_exercise_yaml(yaml_text)
    assert ex.requer_repositorio is True
