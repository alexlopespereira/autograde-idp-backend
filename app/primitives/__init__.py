from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class CriterioResult:
    passed: bool
    points_earned: int
    points_max: int
    message: str


@runtime_checkable
class Primitive(Protocol):
    def __call__(self, args: dict, evidence: dict) -> CriterioResult: ...


PrimitiveFunc = Callable[[dict, dict], CriterioResult]

registry: dict[str, PrimitiveFunc] = {}


def register(name: str) -> Callable[[PrimitiveFunc], PrimitiveFunc]:
    def decorator(func: PrimitiveFunc) -> PrimitiveFunc:
        registry[name] = func
        return func

    return decorator


from . import (  # noqa: E402, F401  -- trigger primitive registrations
    evidence_artifacts,
    evidence_shell,
    github,
    judge_llm,
)
