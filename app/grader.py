from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .curriculum import Criterio, Exercise
from .primitives import CriterioResult, registry

# Os criterios são avaliados concorrentemente: os judges LLM (judge.artifacts.*)
# são I/O-bound (HTTP Gemini, cada um já capado em GEMINI_TIMEOUT_SECONDS) e
# liberam o GIL durante a espera. Sequencial, a latência era a SOMA das chamadas
# (ex: 2.1 com ~7 judges → 40–90s, estourando o read-timeout do CLI). Paralelo,
# vira ~o judge mais lento. Override do teto de threads: GRADER_MAX_WORKERS.
_DEFAULT_MAX_WORKERS = 8


@dataclass(frozen=True)
class Bulletin:
    criterios: tuple[CriterioResult, ...]
    total: int
    max_total: int


def _max_workers(n_criterios: int) -> int:
    try:
        configured = int(os.environ.get("GRADER_MAX_WORKERS", str(_DEFAULT_MAX_WORKERS)))
    except (TypeError, ValueError):
        configured = _DEFAULT_MAX_WORKERS
    return max(1, min(configured, n_criterios))


def _grade_one(criterio: Criterio, evidence: dict) -> CriterioResult:
    """Avalia um único criterio. Isola falha do primitive (nunca propaga)."""
    primitive = registry.get(criterio.check)
    if primitive is None:
        return CriterioResult(
            passed=False,
            points_earned=0,
            points_max=criterio.peso,
            message=f"primitive desconhecido: {criterio.check}",
        )
    try:
        args_with_peso = {**criterio.args, "_peso": criterio.peso}
        return primitive(args_with_peso, evidence)
    except Exception as exc:  # noqa: BLE001 - intencional: isolar falha do primitive
        return CriterioResult(
            passed=False,
            points_earned=0,
            points_max=criterio.peso,
            message=f"erro ao executar primitive '{criterio.check}': {exc}",
        )


def grade(exercise: Exercise, evidence: dict) -> Bulletin:
    criterios = exercise.criterios
    if not criterios:
        return Bulletin(criterios=(), total=0, max_total=0)

    # pool.map preserva a ordem de entrada na saída — o boletim continua na
    # ordem dos criterios do YAML, independente de quem termina primeiro.
    with ThreadPoolExecutor(max_workers=_max_workers(len(criterios))) as pool:
        results = list(pool.map(lambda c: _grade_one(c, evidence), criterios))

    max_total = sum(c.peso for c in criterios)
    total = sum(r.points_earned for r in results if r.passed)
    return Bulletin(criterios=tuple(results), total=total, max_total=max_total)
