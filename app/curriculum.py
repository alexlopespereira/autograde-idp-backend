from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import yaml

REQUIRED_TOP_KEYS = (
    "exercicio",
    "titulo",
    "turmas",
    "disponivel_a_partir_de",
    "prazo",
    "criterios",
)
REQUIRED_CRITERIO_KEYS = ("id", "peso", "check")
# Perguntas têm validação dependente do `tipo`:
#   reflexao (default) → exige texto, criterios_avaliacao, peso (grading Gemini subjetivo)
#   sql               → exige texto, query_referencia, peso (grading por execução SQLite)
PERGUNTA_TIPOS = ("reflexao", "sql")


class CurriculumValidationError(Exception):
    """Raised when an exercise YAML violates schema."""


@dataclass(frozen=True)
class Criterio:
    id: str
    peso: int
    check: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Pergunta:
    texto: str
    peso: int
    tipo: str = "reflexao"
    # reflexao: rubrica textual pro judge Gemini. sql: vazio (não usado).
    criterios_avaliacao: str = ""
    # sql: query gold do professor; o gabarito é o resultado dela na base.
    query_referencia: str = ""
    # sql: True quando a tarefa testa ORDER BY (comparação sensível à ordem).
    ordenado: bool = False


@dataclass(frozen=True)
class DatasetSql:
    """Base SQLite compartilhada pelas perguntas ``tipo: sql`` de um exercício."""

    schema: str  # DDL (CREATE TABLE ...)
    seed: str  # INSERTs (pode ser vazio)


@dataclass(frozen=True)
class Artefato:
    """Arquivo que o aluno entrega, declarado no YAML em ``artefatos:``.

    ``role`` é a chave que os primitives ``evidence.artifacts.*`` e
    ``judge.artifacts.*`` usam em ``criterios.args.role``; ``path`` é o caminho
    relativo à raiz do repo do aluno que o CLI lê.
    """

    role: str
    path: str
    required: bool = True


@dataclass(frozen=True)
class ComandoShell:
    """Comando que o CLI executa na máquina do aluno, declarado no YAML.

    ``cmd`` é a lista argv (sem shell). O placeholder ``{owner_repo}`` é
    substituído pelo CLI a partir do ``repo_url`` — e pelo backend, do mesmo
    jeito, na hora de montar a whitelist que valida a evidência recebida.
    ``extract`` rotula o resultado para os primitives ``evidence.shell.*``.
    """

    cmd: tuple[str, ...]
    extract: str = ""


@dataclass(frozen=True)
class Exercise:
    id: str
    titulo: str
    turmas: tuple[str, ...]
    disponivel_a_partir_de: datetime
    prazo: dict[str, Any]
    criterios: tuple[Criterio, ...]
    perguntas: tuple[Pergunta, ...] = ()
    dataset_sql: DatasetSql | None = None
    artefatos: tuple[Artefato, ...] = ()
    comandos_shell: tuple[ComandoShell, ...] = ()
    # `false` desliga a exigência de que o trabalho esteja versionado num repo
    # do GitHub: o CLI não lê `remote.origin.url`, não manda `repo_url`, e o
    # backend não chama a API do GitHub. A identidade continua vindo do login
    # Google + roster; quem quiser a amarra com a conta GitHub declara
    # `gh auth status` em `comandos_shell:` e um critério
    # `evidence.shell.gh_auth_ok` — que já confere o usuário contra o roster,
    # sem precisar de repositório.
    # Default `true` por compatibilidade: os exercícios de git (aula 1) e todos
    # os YAMLs já no ar seguem exigindo repo sem precisar declarar nada.
    requer_repositorio: bool = True


def parse_exercise_yaml(yaml_text: str) -> Exercise:
    if not yaml_text or not yaml_text.strip():
        raise CurriculumValidationError("YAML vazio")

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise CurriculumValidationError(f"YAML malformado: {exc}") from exc

    if not isinstance(data, dict):
        raise CurriculumValidationError("YAML root precisa ser mapping")

    missing = [k for k in REQUIRED_TOP_KEYS if k not in data]
    if missing:
        raise CurriculumValidationError(f"campos obrigatorios faltantes: {missing}")

    try:
        disponivel = _parse_datetime(data["disponivel_a_partir_de"])
    except (TypeError, ValueError) as exc:
        raise CurriculumValidationError(f"disponivel_a_partir_de invalido: {exc}") from exc

    turmas_raw = data["turmas"]
    if not isinstance(turmas_raw, list) or not all(isinstance(t, str) for t in turmas_raw):
        raise CurriculumValidationError("turmas precisa ser lista de strings")

    prazo = data["prazo"]
    if not isinstance(prazo, dict):
        raise CurriculumValidationError("prazo precisa ser mapping")

    criterios_raw = data["criterios"]
    if not isinstance(criterios_raw, list):
        raise CurriculumValidationError("criterios precisa ser lista")

    criterios: list[Criterio] = []
    for idx, c in enumerate(criterios_raw):
        if not isinstance(c, dict):
            raise CurriculumValidationError(f"criterio[{idx}] precisa ser mapping")
        for key in REQUIRED_CRITERIO_KEYS:
            if key not in c:
                raise CurriculumValidationError(f"criterio[{idx}]: campo '{key}' faltante")
        args_raw = c.get("args")
        if args_raw is not None and not isinstance(args_raw, dict):
            raise CurriculumValidationError(
                f"criterio[{idx}]: campo 'args' precisa ser mapping, "
                f"recebi {type(args_raw).__name__}"
            )
        criterios.append(
            Criterio(
                id=str(c["id"]),
                peso=int(c["peso"]),
                check=str(c["check"]),
                args=dict(args_raw) if args_raw else {},
            )
        )

    perguntas = _parse_perguntas(data.get("perguntas"))
    dataset_sql = _parse_dataset_sql(data.get("dataset_sql"))
    artefatos = _parse_artefatos(data.get("artefatos"))
    comandos_shell = _parse_comandos_shell(data.get("comandos_shell"))
    requer_repositorio = _parse_requer_repositorio(data.get("requer_repositorio"))

    # Cross-check: pergunta sql precisa de uma base pra rodar a query gold.
    if any(p.tipo == "sql" for p in perguntas) and dataset_sql is None:
        raise CurriculumValidationError(
            "há pergunta tipo 'sql' mas falta o bloco 'dataset_sql' (schema + seed)"
        )

    # Cross-check: declarar `requer_repositorio: false` e ainda depender do repo
    # é contradição que, sem isto, viraria nota zero silenciosa em produção —
    # o critério `github.*` não teria evidência e o `{owner_repo}` não teria
    # com que ser substituído.
    if not requer_repositorio:
        _reject_repo_dependencies(criterios, comandos_shell)

    return Exercise(
        id=str(data["exercicio"]),
        titulo=str(data["titulo"]),
        turmas=tuple(turmas_raw),
        disponivel_a_partir_de=disponivel,
        prazo=dict(prazo),
        criterios=tuple(criterios),
        perguntas=perguntas,
        dataset_sql=dataset_sql,
        artefatos=artefatos,
        comandos_shell=comandos_shell,
        requer_repositorio=requer_repositorio,
    )


def _parse_requer_repositorio(raw: Any) -> bool:
    """Parseia ``requer_repositorio:`` — booleano estrito, default ``True``.

    Não usa ``bool(raw)`` de propósito: no YAML, ``requer_repositorio: "false"``
    é a string ``"false"``, que é truthy, e o exercício voltaria a exigir repo
    sem ninguém perceber. Aqui isso é erro de validação.
    """
    if raw is None:
        return True
    if not isinstance(raw, bool):
        raise CurriculumValidationError(
            f"requer_repositorio precisa ser booleano (true/false), "
            f"recebi {type(raw).__name__}"
        )
    return raw


def _reject_repo_dependencies(
    criterios: list[Criterio], comandos_shell: tuple[ComandoShell, ...]
) -> None:
    github_checks = sorted({c.id for c in criterios if c.check.startswith("github.")})
    if github_checks:
        raise CurriculumValidationError(
            f"requer_repositorio: false, mas há criterios com check 'github.*' "
            f"(sem repo o backend não lê a API do GitHub): {github_checks}"
        )
    com_placeholder = [
        " ".join(c.cmd)
        for c in comandos_shell
        if any("{owner_repo}" in tok for tok in c.cmd)
    ]
    if com_placeholder:
        raise CurriculumValidationError(
            f"requer_repositorio: false, mas há comandos_shell com o "
            f"placeholder '{{owner_repo}}' (nada para substituir): {com_placeholder}"
        )


def _parse_perguntas(raw: Any) -> tuple[Pergunta, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CurriculumValidationError("perguntas precisa ser lista")
    out: list[Pergunta] = []
    for idx, p in enumerate(raw):
        if not isinstance(p, dict):
            raise CurriculumValidationError(f"perguntas[{idx}] precisa ser mapping")

        tipo = str(p.get("tipo", "reflexao")).strip() or "reflexao"
        if tipo not in PERGUNTA_TIPOS:
            raise CurriculumValidationError(
                f"perguntas[{idx}]: tipo '{tipo}' inválido (use {PERGUNTA_TIPOS})"
            )

        if "texto" not in p:
            raise CurriculumValidationError(f"perguntas[{idx}]: campo 'texto' faltante")
        if "peso" not in p:
            raise CurriculumValidationError(f"perguntas[{idx}]: campo 'peso' faltante")
        texto = str(p["texto"]).strip()
        if not texto:
            raise CurriculumValidationError(f"perguntas[{idx}]: texto vazio")
        try:
            peso = int(p["peso"])
        except (TypeError, ValueError) as exc:
            raise CurriculumValidationError(
                f"perguntas[{idx}]: peso precisa ser inteiro ({exc})"
            ) from exc
        if peso <= 0:
            raise CurriculumValidationError(f"perguntas[{idx}]: peso precisa ser > 0")

        criterios_avaliacao = ""
        query_referencia = ""
        ordenado = False
        if tipo == "reflexao":
            if "criterios_avaliacao" not in p:
                raise CurriculumValidationError(
                    f"perguntas[{idx}]: campo 'criterios_avaliacao' faltante"
                )
            criterios_avaliacao = str(p["criterios_avaliacao"]).strip()
            if not criterios_avaliacao:
                raise CurriculumValidationError(
                    f"perguntas[{idx}]: criterios_avaliacao vazio"
                )
        else:  # sql
            if "query_referencia" not in p:
                raise CurriculumValidationError(
                    f"perguntas[{idx}]: campo 'query_referencia' faltante (tipo sql)"
                )
            query_referencia = str(p["query_referencia"]).strip()
            if not query_referencia:
                raise CurriculumValidationError(
                    f"perguntas[{idx}]: query_referencia vazio"
                )
            ordenado = bool(p.get("ordenado", False))

        out.append(
            Pergunta(
                texto=texto,
                peso=peso,
                tipo=tipo,
                criterios_avaliacao=criterios_avaliacao,
                query_referencia=query_referencia,
                ordenado=ordenado,
            )
        )
    return tuple(out)


def _parse_dataset_sql(raw: Any) -> DatasetSql | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CurriculumValidationError("dataset_sql precisa ser mapping")
    if "schema" not in raw:
        raise CurriculumValidationError("dataset_sql: campo 'schema' faltante")
    schema = str(raw["schema"]).strip()
    if not schema:
        raise CurriculumValidationError("dataset_sql: schema vazio")
    seed = str(raw.get("seed", "")).strip()
    return DatasetSql(schema=schema, seed=seed)


def _parse_artefatos(raw: Any) -> tuple[Artefato, ...]:
    """Parseia ``artefatos:`` — a lista de arquivos que o aluno entrega.

    Ausente devolve tupla vazia: exercícios sem artefato textual (os de `gh`
    puro, por exemplo) continuam válidos.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CurriculumValidationError("artefatos precisa ser lista")
    out: list[Artefato] = []
    roles_vistos: set[str] = set()
    for idx, a in enumerate(raw):
        if not isinstance(a, dict):
            raise CurriculumValidationError(f"artefatos[{idx}] precisa ser mapping")
        for key in ("role", "path"):
            if key not in a:
                raise CurriculumValidationError(
                    f"artefatos[{idx}]: campo '{key}' faltante"
                )
        role = str(a["role"]).strip()
        path = str(a["path"]).strip()
        if not role:
            raise CurriculumValidationError(f"artefatos[{idx}]: role vazio")
        if not path:
            raise CurriculumValidationError(f"artefatos[{idx}]: path vazio")
        if role in roles_vistos:
            raise CurriculumValidationError(
                f"artefatos[{idx}]: role '{role}' duplicado"
            )
        roles_vistos.add(role)
        out.append(
            Artefato(role=role, path=path, required=bool(a.get("required", True)))
        )
    return tuple(out)


def _parse_comandos_shell(raw: Any) -> tuple[ComandoShell, ...]:
    """Parseia ``comandos_shell:`` — o que o CLI roda na máquina do aluno.

    Aceita duas formas por entrada:
      * lista argv pura — ``["gh", "--version"]``
      * mapping — ``{cmd: [...], extract: "pytest"}``
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CurriculumValidationError("comandos_shell precisa ser lista")
    out: list[ComandoShell] = []
    for idx, c in enumerate(raw):
        if isinstance(c, dict):
            cmd_raw = c.get("cmd")
            extract = str(c.get("extract", "") or "").strip()
        else:
            cmd_raw = c
            extract = ""
        if not isinstance(cmd_raw, list) or not cmd_raw:
            raise CurriculumValidationError(
                f"comandos_shell[{idx}]: cmd precisa ser lista nao vazia de strings"
            )
        cmd = tuple(str(tok) for tok in cmd_raw)
        if any(not tok for tok in cmd):
            raise CurriculumValidationError(
                f"comandos_shell[{idx}]: token vazio no argv"
            )
        out.append(ComandoShell(cmd=cmd, extract=extract))
    return tuple(out)


def _parse_datetime(val: Any) -> datetime:
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        return datetime.fromisoformat(val)
    raise ValueError(f"esperado datetime ou string ISO, recebi {type(val).__name__}")


def fetch_exercise(
    url: str,
    exercise_id: str,
    fetcher: Callable[[str], str],
) -> Exercise:
    yaml_text = fetcher(url)
    exercise = parse_exercise_yaml(yaml_text)
    if exercise.id != exercise_id:
        raise CurriculumValidationError(
            f"YAML.exercicio ({exercise.id!r}) != exercise_id solicitado ({exercise_id!r})"
        )
    return exercise
