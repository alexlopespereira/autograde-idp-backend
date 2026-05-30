import threading
import time
from datetime import datetime

from app.curriculum import Criterio, Exercise
from app.grader import Bulletin, grade
from app.primitives import CriterioResult, register


@register("test.always_pass")
def _always_pass(args: dict, evidence: dict) -> CriterioResult:
    peso = int(args.get("_peso", 0))
    return CriterioResult(passed=True, points_earned=peso, points_max=peso, message="ok")


@register("test.always_fail")
def _always_fail(args: dict, evidence: dict) -> CriterioResult:
    peso = int(args.get("_peso", 0))
    return CriterioResult(passed=False, points_earned=0, points_max=peso, message="falhou")


@register("test.raises")
def _raises(args: dict, evidence: dict) -> CriterioResult:
    raise RuntimeError("boom")


def _make_exercise(criterios: tuple[Criterio, ...]) -> Exercise:
    return Exercise(
        id="test",
        titulo="t",
        turmas=("X",),
        disponivel_a_partir_de=datetime(2026, 1, 1),
        prazo={},
        criterios=criterios,
    )


def test_grade_consolidates_pass_fail_exception():
    ex = _make_exercise(
        (
            Criterio(id="a", peso=10, check="test.always_pass", args={}),
            Criterio(id="b", peso=20, check="test.always_fail", args={}),
            Criterio(id="c", peso=30, check="test.raises", args={}),
        )
    )

    bulletin = grade(ex, evidence={})

    assert isinstance(bulletin, Bulletin)
    assert len(bulletin.criterios) == 3

    pass_result, fail_result, raise_result = bulletin.criterios
    assert pass_result.passed is True
    assert pass_result.points_earned == 10
    assert pass_result.points_max == 10

    assert fail_result.passed is False
    assert fail_result.points_earned == 0
    assert fail_result.points_max == 20

    assert raise_result.passed is False
    assert raise_result.points_earned == 0
    assert raise_result.points_max == 30
    assert "boom" in raise_result.message
    assert "test.raises" in raise_result.message

    assert bulletin.total == 10
    assert bulletin.max_total == 60


def test_grade_unknown_primitive_does_not_raise():
    ex = _make_exercise(
        (Criterio(id="x", peso=10, check="nope.does_not_exist", args={}),)
    )

    bulletin = grade(ex, evidence={})

    assert bulletin.criterios[0].passed is False
    assert bulletin.criterios[0].points_earned == 0
    assert bulletin.criterios[0].points_max == 10
    assert "primitive desconhecido" in bulletin.criterios[0].message
    assert "nope.does_not_exist" in bulletin.criterios[0].message
    assert bulletin.total == 0
    assert bulletin.max_total == 10


def test_grade_passes_args_and_evidence_to_primitive():
    captured = {}

    @register("test.capture")
    def _capture(args: dict, evidence: dict) -> CriterioResult:
        captured["args"] = args
        captured["evidence"] = evidence
        return CriterioResult(passed=True, points_earned=5, points_max=5, message="capt")

    ex = _make_exercise(
        (Criterio(id="cap", peso=5, check="test.capture", args={"path": "README.md"}),)
    )

    grade(ex, evidence={"repo_url": "https://github.com/x/y"})

    assert captured["args"]["path"] == "README.md"
    assert captured["args"]["_peso"] == 5
    assert captured["evidence"] == {"repo_url": "https://github.com/x/y"}


def test_grade_empty_criterios():
    ex = _make_exercise(())
    bulletin = grade(ex, evidence={})
    assert bulletin.criterios == ()
    assert bulletin.total == 0
    assert bulletin.max_total == 0


def test_github_stub_primitives_registered():
    from app.primitives import registry

    expected = {
        "github.repo.exists",
        "github.repo.public",
        "github.repo.has_file",
        "github.repo.file_not_empty",
        "github.repo.name_matches",
        "github.commits.count_at_least",
        "github.commits.last_within",
    }
    assert expected.issubset(set(registry.keys()))


def test_github_primitives_handle_empty_evidence():
    from app.primitives import registry

    result = registry["github.repo.public"]({"_peso": 10}, {})
    assert result.passed is False
    assert result.points_earned == 0
    assert result.points_max == 10


def test_grade_runs_primitives_concurrently():
    """Judges I/O-bound rodam em paralelo: 3 primitives que esperam um Barrier
    de 3 só liberam se executados simultaneamente. Sequencial → BrokenBarrier.
    """
    n = 3
    barrier = threading.Barrier(n, timeout=5)
    reached: list[int] = []
    lock = threading.Lock()

    @register("test.barrier")
    def _barrier(args: dict, evidence: dict) -> CriterioResult:
        peso = int(args.get("_peso", 0))
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            return CriterioResult(False, 0, peso, "barrier timeout (rodou sequencial)")
        with lock:
            reached.append(peso)
        return CriterioResult(True, peso, peso, "concorrente ok")

    ex = _make_exercise(
        tuple(Criterio(id=f"c{i}", peso=10, check="test.barrier", args={}) for i in range(n))
    )

    bulletin = grade(ex, evidence={})

    assert len(reached) == n, "primitives não rodaram em paralelo (barrier não fechou)"
    assert all(c.passed for c in bulletin.criterios)
    assert bulletin.total == 30


def test_grade_preserves_order_despite_completion_race():
    """Ordem do boletim segue a ordem dos criterios, não a ordem de término.
    O de menor peso dorme mais → termina por último, mas deve sair primeiro.
    """

    @register("test.sleep_inverse")
    def _sleep_inverse(args: dict, evidence: dict) -> CriterioResult:
        peso = int(args.get("_peso", 0))
        time.sleep((40 - peso) / 1000.0)  # peso menor dorme mais
        return CriterioResult(True, peso, peso, f"p{peso}")

    ex = _make_exercise(
        (
            Criterio(id="a", peso=10, check="test.sleep_inverse", args={}),
            Criterio(id="b", peso=20, check="test.sleep_inverse", args={}),
            Criterio(id="c", peso=30, check="test.sleep_inverse", args={}),
        )
    )

    bulletin = grade(ex, evidence={})

    assert [c.points_max for c in bulletin.criterios] == [10, 20, 30]
    assert [c.message for c in bulletin.criterios] == ["p10", "p20", "p30"]
    assert bulletin.total == 60
