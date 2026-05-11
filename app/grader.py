from __future__ import annotations

from dataclasses import dataclass

from .curriculum import Exercise
from .primitives import CriterioResult, registry


@dataclass(frozen=True)
class Bulletin:
    criterios: tuple[CriterioResult, ...]
    total: int
    max_total: int


def grade(exercise: Exercise, evidence: dict) -> Bulletin:
    results: list[CriterioResult] = []
    total = 0
    max_total = 0
    for criterio in exercise.criterios:
        max_total += criterio.peso
        primitive = registry.get(criterio.check)
        if primitive is None:
            results.append(
                CriterioResult(
                    passed=False,
                    points_earned=0,
                    points_max=criterio.peso,
                    message=f"primitive desconhecido: {criterio.check}",
                )
            )
            continue
        try:
            args_with_peso = {**criterio.args, "_peso": criterio.peso}
            result = primitive(args_with_peso, evidence)
        except Exception as exc:  # noqa: BLE001 - intencional: isolar falha do primitive
            results.append(
                CriterioResult(
                    passed=False,
                    points_earned=0,
                    points_max=criterio.peso,
                    message=f"erro ao executar primitive '{criterio.check}': {exc}",
                )
            )
            continue
        results.append(result)
        if result.passed:
            total += result.points_earned

    return Bulletin(criterios=tuple(results), total=total, max_total=max_total)
