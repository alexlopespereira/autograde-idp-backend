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
REQUIRED_PERGUNTA_KEYS = ("texto", "criterios_avaliacao", "peso")


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
    criterios_avaliacao: str
    peso: int


@dataclass(frozen=True)
class Exercise:
    id: str
    titulo: str
    turmas: tuple[str, ...]
    disponivel_a_partir_de: datetime
    prazo: dict[str, Any]
    criterios: tuple[Criterio, ...]
    perguntas: tuple[Pergunta, ...] = ()


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

    return Exercise(
        id=str(data["exercicio"]),
        titulo=str(data["titulo"]),
        turmas=tuple(turmas_raw),
        disponivel_a_partir_de=disponivel,
        prazo=dict(prazo),
        criterios=tuple(criterios),
        perguntas=perguntas,
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
        for key in REQUIRED_PERGUNTA_KEYS:
            if key not in p:
                raise CurriculumValidationError(
                    f"perguntas[{idx}]: campo '{key}' faltante"
                )
        texto = str(p["texto"]).strip()
        criterios_avaliacao = str(p["criterios_avaliacao"]).strip()
        if not texto:
            raise CurriculumValidationError(f"perguntas[{idx}]: texto vazio")
        if not criterios_avaliacao:
            raise CurriculumValidationError(
                f"perguntas[{idx}]: criterios_avaliacao vazio"
            )
        try:
            peso = int(p["peso"])
        except (TypeError, ValueError) as exc:
            raise CurriculumValidationError(
                f"perguntas[{idx}]: peso precisa ser inteiro ({exc})"
            ) from exc
        if peso <= 0:
            raise CurriculumValidationError(
                f"perguntas[{idx}]: peso precisa ser > 0"
            )
        out.append(Pergunta(texto=texto, criterios_avaliacao=criterios_avaliacao, peso=peso))
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
