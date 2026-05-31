"""Tests for judge.artifacts.* primitives.

Estratégia: monkeypatch ``app.primitives.judge_llm.grade_artifact`` para
retornar ``JudgeResult`` controlado. Não bate em Gemini real. Cobre:
  * happy path (passed conforme threshold)
  * score abaixo de PASS_THRESHOLD → reprovado mas points_earned proporcional
  * fallback (ok=False → nota máxima por convenção)
  * artefato ausente → falha imediata sem chamar judge
  * actor_map_quality concatena map + transcript corretamente
"""
from __future__ import annotations

from typing import Any

import pytest

from app.gemini import JudgeResult
from app.primitives import judge_llm, registry


def _entry(role: str, **fields: Any) -> dict[str, Any]:
    base = {
        "tool": "artifacts",
        "role": role,
        "path": f"{role}.md",
        "required": True,
        "exists": True,
        "size_bytes": 1000,
        "word_count": 200,
        "sha256": "hash",
        "headings": [],
        "links": [],
        "content": "",
        "captured_at": "2026-05-16T12:00:00+00:00",
    }
    base.update(fields)
    return base


def _ev(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"artifacts": list(entries)}


def _stub_judge(monkeypatch, result: JudgeResult, capture: dict[str, Any] | None = None):
    """Substitui grade_artifact no módulo judge_llm por stub que retorna result."""

    def fake(
        rubrica_text: str,
        role: str,
        content: str,
        headings: list[str],
        word_count: int,
        n_links: int,
        **_kw: Any,
    ) -> JudgeResult:
        if capture is not None:
            capture["rubrica"] = rubrica_text
            capture["role"] = role
            capture["content"] = content
            capture["headings"] = headings
            capture["word_count"] = word_count
            capture["n_links"] = n_links
        return result

    monkeypatch.setattr(judge_llm, "grade_artifact", fake)


# ---------- meta_prompt_quality --------------------------------------------


def test_meta_prompt_quality_passes_with_high_score(monkeypatch: pytest.MonkeyPatch):
    _stub_judge(
        monkeypatch,
        JudgeResult(score=1.0, evidence_quote="atende tudo", missing="", ok=True),
    )
    r = registry["judge.artifacts.meta_prompt_quality"](
        {"_peso": 20, "role": "meta_prompt", "sub_criterios": ["A1", "A2"]},
        _ev(_entry("meta_prompt", content="meu meta-prompt completo")),
    )
    assert r.passed is True
    assert r.points_earned == 20
    assert "1.00" in r.message
    assert "atende tudo" in r.message


def test_meta_prompt_quality_partial_score_proportional(monkeypatch: pytest.MonkeyPatch):
    _stub_judge(
        monkeypatch,
        JudgeResult(score=0.4, evidence_quote="parcial", missing="faltou A3 A4 A5", ok=True),
    )
    r = registry["judge.artifacts.meta_prompt_quality"](
        {"_peso": 20, "role": "meta_prompt"},
        _ev(_entry("meta_prompt", content="meta-prompt incompleto")),
    )
    # score=0.4 < PASS_THRESHOLD=0.5 → passed=False mas pontos proporcionais
    assert r.passed is False
    assert r.points_earned == 8  # round(0.4 * 20) = 8
    assert "faltou A3 A4 A5" in r.message


def test_meta_prompt_quality_passes_at_threshold(monkeypatch: pytest.MonkeyPatch):
    _stub_judge(monkeypatch, JudgeResult(score=0.5, evidence_quote="metade", missing="", ok=True))
    r = registry["judge.artifacts.meta_prompt_quality"](
        {"_peso": 20, "role": "meta_prompt"},
        _ev(_entry("meta_prompt", content="x")),
    )
    assert r.passed is True
    assert r.points_earned == 10


def test_meta_prompt_quality_missing_artifact_does_not_call_judge(
    monkeypatch: pytest.MonkeyPatch,
):
    def must_not_call(*a: Any, **k: Any) -> JudgeResult:
        raise AssertionError("grade_artifact não deve ser chamado quando artefato ausente")

    monkeypatch.setattr(judge_llm, "grade_artifact", must_not_call)
    r = registry["judge.artifacts.meta_prompt_quality"](
        {"_peso": 20, "role": "meta_prompt"},
        _ev(),  # payload vazio
    )
    assert r.passed is False
    assert r.points_earned == 0
    assert "ausente" in r.message


def test_meta_prompt_quality_fallback_when_ok_false(monkeypatch: pytest.MonkeyPatch):
    _stub_judge(
        monkeypatch,
        JudgeResult(score=1.0, evidence_quote="", missing="HTTP 500", ok=False),
    )
    r = registry["judge.artifacts.meta_prompt_quality"](
        {"_peso": 20, "role": "meta_prompt"},
        _ev(_entry("meta_prompt", content="x")),
    )
    # Fallback: nota máxima, passed=True, mensagem indica fallback
    assert r.passed is True
    assert r.points_earned == 20
    assert "fallback" in r.message.lower()
    # Auditoria: fallback marca degraded=True (nota provisória, re-correção)
    assert r.degraded is True


def test_meta_prompt_quality_not_degraded_when_ok_true(monkeypatch: pytest.MonkeyPatch):
    _stub_judge(
        monkeypatch,
        JudgeResult(score=0.8, evidence_quote="ok", missing="", ok=True),
    )
    r = registry["judge.artifacts.meta_prompt_quality"](
        {"_peso": 20, "role": "meta_prompt"},
        _ev(_entry("meta_prompt", content="x")),
    )
    # judge respondeu normalmente → nota é definitiva, não degradada
    assert r.degraded is False


def test_meta_prompt_quality_passes_metadata_to_judge(monkeypatch: pytest.MonkeyPatch):
    capture: dict[str, Any] = {}
    _stub_judge(monkeypatch, JudgeResult(1.0, "ok", "", True), capture)
    registry["judge.artifacts.meta_prompt_quality"](
        {"_peso": 20, "role": "meta_prompt", "sub_criterios": ["Escopo", "Fontes"]},
        _ev(
            _entry(
                "meta_prompt",
                content="prompt aqui",
                word_count=250,
                links=["https://a", "https://b"],
                headings=["# H1"],
            )
        ),
    )
    assert capture["content"] == "prompt aqui"
    assert capture["word_count"] == 250
    assert capture["n_links"] == 2
    assert "Escopo" in capture["rubrica"]
    assert "Fontes" in capture["rubrica"]


# ---------- divergence_real ------------------------------------------------


def test_divergence_real_passes(monkeypatch: pytest.MonkeyPatch):
    _stub_judge(
        monkeypatch,
        JudgeResult(score=0.9, evidence_quote="A1 diz X; A2 diz Y", missing="", ok=True),
    )
    r = registry["judge.artifacts.divergence_real"](
        {"_peso": 6, "role": "synthesis"},
        _ev(_entry("synthesis", content="...")),
    )
    assert r.passed is True
    assert r.points_earned == 5  # round(0.9 * 6) = 5


def test_divergence_real_fails_when_cosmetic(monkeypatch: pytest.MonkeyPatch):
    _stub_judge(
        monkeypatch,
        JudgeResult(
            score=0.0,
            evidence_quote="só resumo",
            missing="nenhuma divergência real",
            ok=True,
        ),
    )
    r = registry["judge.artifacts.divergence_real"](
        {"_peso": 6, "role": "synthesis"},
        _ev(_entry("synthesis", content="resumo dos dois")),
    )
    assert r.passed is False
    assert r.points_earned == 0


# ---------- evolution_substantive ------------------------------------------


def test_evolution_substantive_full(monkeypatch: pytest.MonkeyPatch):
    _stub_judge(
        monkeypatch,
        JudgeResult(score=1.0, evidence_quote="cada v cita gatilho", missing="", ok=True),
    )
    r = registry["judge.artifacts.evolution_substantive"](
        {"_peso": 4, "role": "synthesis", "min_iterations": 2},
        _ev(
            _entry(
                "synthesis",
                content="## v1\n...\n## v2\n### Mudanças nesta versão\n- Gatilho: pergunta 3",
            )
        ),
    )
    assert r.passed is True
    assert r.points_earned == 4


def test_evolution_substantive_rejects_cosmetic(monkeypatch: pytest.MonkeyPatch):
    _stub_judge(
        monkeypatch,
        JudgeResult(score=0.0, evidence_quote="", missing="apenas reescrita cosmética", ok=True),
    )
    r = registry["judge.artifacts.evolution_substantive"](
        {"_peso": 4, "role": "synthesis"},
        _ev(_entry("synthesis", content="## v1\n## v2\n(igual a v1)")),
    )
    assert r.passed is False
    assert r.points_earned == 0


def test_evolution_substantive_passes_min_iterations_to_rubric(
    monkeypatch: pytest.MonkeyPatch,
):
    capture: dict[str, Any] = {}
    _stub_judge(monkeypatch, JudgeResult(1.0, "ok", "", True), capture)
    registry["judge.artifacts.evolution_substantive"](
        {"_peso": 4, "role": "synthesis", "min_iterations": 3},
        _ev(_entry("synthesis", content="x")),
    )
    assert "≥3 iterações" in capture["rubrica"] or "3 iterações" in capture["rubrica"]


# ---------- actor_map_quality ----------------------------------------------


def test_actor_map_quality_concatenates_map_and_transcript(
    monkeypatch: pytest.MonkeyPatch,
):
    capture: dict[str, Any] = {}
    _stub_judge(monkeypatch, JudgeResult(0.8, "bom", "", True), capture)
    registry["judge.artifacts.actor_map_quality"](
        {
            "_peso": 24,
            "role_map": "actor_map",
            "role_transcript": "grill_transcript",
            "min_actors": 7,
            "min_humans": 2,
            "min_ai": 2,
        },
        _ev(
            _entry("actor_map", content="MAPA-CONTEUDO"),
            _entry("grill_transcript", content="TRANSCRIPT-CONTEUDO"),
        ),
    )
    # Conteúdo concatenado com delimitadores
    assert "MAPA-CONTEUDO" in capture["content"]
    assert "TRANSCRIPT-CONTEUDO" in capture["content"]
    assert "=== MAPA" in capture["content"]
    assert "=== TRANSCRIPT" in capture["content"]
    # Rubrica menciona contagens mínimas
    assert "7" in capture["rubrica"]
    assert "2 humanos" in capture["rubrica"]
    assert "2 IA" in capture["rubrica"]


def test_actor_map_quality_omits_typing_when_min_humans_and_min_ai_zero(
    monkeypatch: pytest.MonkeyPatch,
):
    """Quando o YAML não exige humanos/IA (min=0), a rubric NÃO menciona
    tipagem humanos/IA — exercício genérico de mapa de atores."""
    capture: dict[str, Any] = {}
    _stub_judge(monkeypatch, JudgeResult(0.8, "ok", "", True), capture)
    registry["judge.artifacts.actor_map_quality"](
        {
            "_peso": 24,
            "role_map": "actor_map",
            "role_transcript": "grill_transcript",
            "min_actors": 7,
            "min_humans": 0,
            "min_ai": 0,
        },
        _ev(
            _entry("actor_map", content="MAPA"),
            _entry("grill_transcript", content="TRANSCRIPT"),
        ),
    )
    rub = capture["rubrica"]
    # contagem mínima de atores ainda aparece, mas SEM cláusula de tipagem
    assert "(≥7 atores):" in rub  # nada anexado tipo ", ≥X humanos, ≥X IA"
    assert "humanos" not in rub
    assert "tipagem" not in rub
    # ainda cobra contagem coerente + consistência + decisões
    assert "categorias coerentes" in rub
    assert "CONSISTÊNCIA" in rub
    assert "DECISÕES CITADAS" in rub


def test_actor_map_quality_misses_when_transcript_absent(
    monkeypatch: pytest.MonkeyPatch,
):
    def must_not_call(*a: Any, **k: Any) -> JudgeResult:
        raise AssertionError("não deve chamar judge sem ambos artefatos")

    monkeypatch.setattr(judge_llm, "grade_artifact", must_not_call)
    r = registry["judge.artifacts.actor_map_quality"](
        {
            "_peso": 24,
            "role_map": "actor_map",
            "role_transcript": "grill_transcript",
        },
        _ev(_entry("actor_map", content="x")),  # transcript ausente
    )
    assert r.passed is False
    assert "grill_transcript" in r.message


# ---------- grill_rounds & relations_explicit ------------------------------


def test_grill_rounds_passes(monkeypatch: pytest.MonkeyPatch):
    _stub_judge(monkeypatch, JudgeResult(1.0, "10 rodadas", "", True))
    r = registry["judge.artifacts.grill_rounds"](
        {"_peso": 8, "role": "grill_transcript", "min_rounds": 8},
        _ev(_entry("grill_transcript", content="...")),
    )
    assert r.passed is True
    assert r.points_earned == 8


def test_relations_explicit_partial_score(monkeypatch: pytest.MonkeyPatch):
    _stub_judge(
        monkeypatch,
        JudgeResult(
            score=0.5,
            evidence_quote="RACI parcial",
            missing="metade dos atores sem A/C/I",
            ok=True,
        ),
    )
    r = registry["judge.artifacts.relations_explicit"](
        {"_peso": 8, "role": "actor_map"},
        _ev(_entry("actor_map", content="| Cidadão | R | | | |")),
    )
    assert r.passed is True
    assert r.points_earned == 4  # round(0.5 * 8) = 4


# ---------- audit_finds_real_issues (B10 — cadeia de auditoria) ------------


def test_audit_finds_real_issues_passes_with_high_score(monkeypatch: pytest.MonkeyPatch):
    _stub_judge(
        monkeypatch,
        JudgeResult(score=1.0, evidence_quote="lacuna de evidência em §3", missing="", ok=True),
    )
    r = registry["judge.artifacts.audit_finds_real_issues"](
        {"_peso": 8, "role_audit": "auditoria_v1", "role_audited": "assistente_v1"},
        _ev(
            _entry("auditoria_v1", content="AUDITORIA-CONTENT"),
            _entry("assistente_v1", content="PESQUISA-CONTENT"),
        ),
    )
    assert r.passed is True
    assert r.points_earned == 8
    assert r.degraded is False


def test_audit_finds_real_issues_concatenates_audit_and_audited(
    monkeypatch: pytest.MonkeyPatch,
):
    capture: dict[str, Any] = {}
    _stub_judge(monkeypatch, JudgeResult(0.9, "ok", "", True), capture)
    registry["judge.artifacts.audit_finds_real_issues"](
        {"_peso": 8, "role_audit": "auditoria_v1", "role_audited": "assistente_v1"},
        _ev(
            _entry("auditoria_v1", content="AUDIT-X"),
            _entry("assistente_v1", content="PESQUISA-Y"),
        ),
    )
    assert "AUDIT-X" in capture["content"]
    assert "PESQUISA-Y" in capture["content"]
    assert "=== AUDITORIA" in capture["content"]
    assert "=== PESQUISA AUDITADA" in capture["content"]
    assert "falha REAL" in capture["rubrica"]


def test_audit_finds_real_issues_misses_when_audited_absent(
    monkeypatch: pytest.MonkeyPatch,
):
    def must_not_call(*a: Any, **k: Any) -> JudgeResult:
        raise AssertionError("não deve chamar judge sem ambos artefatos")

    monkeypatch.setattr(judge_llm, "grade_artifact", must_not_call)
    r = registry["judge.artifacts.audit_finds_real_issues"](
        {"_peso": 8, "role_audit": "auditoria_v1", "role_audited": "assistente_v1"},
        _ev(_entry("auditoria_v1", content="x")),  # assistente_v1 ausente
    )
    assert r.passed is False
    assert "assistente_v1" in r.message


# ---------- iteration_addresses_audit (B11 — cadeia de auditoria) ----------


def test_iteration_addresses_audit_passes(monkeypatch: pytest.MonkeyPatch):
    _stub_judge(
        monkeypatch,
        JudgeResult(score=0.8, evidence_quote="v2 §2 corrige fonte fraca", missing="", ok=True),
    )
    r = registry["judge.artifacts.iteration_addresses_audit"](
        {"_peso": 6, "role_iteration": "assistente_v2", "role_audit": "auditoria_v1"},
        _ev(
            _entry("assistente_v2", content="V2-CONTENT"),
            _entry("auditoria_v1", content="AUDIT1-CONTENT"),
        ),
    )
    assert r.passed is True
    assert r.points_earned == 5  # round(0.8 * 6)


def test_iteration_addresses_audit_misses_when_audit_absent(
    monkeypatch: pytest.MonkeyPatch,
):
    def must_not_call(*a: Any, **k: Any) -> JudgeResult:
        raise AssertionError("não deve chamar judge sem ambos artefatos")

    monkeypatch.setattr(judge_llm, "grade_artifact", must_not_call)
    r = registry["judge.artifacts.iteration_addresses_audit"](
        {"_peso": 6, "role_iteration": "assistente_v2", "role_audit": "auditoria_v1"},
        _ev(_entry("assistente_v2", content="x")),  # auditoria_v1 ausente
    )
    assert r.passed is False
    assert "auditoria_v1" in r.message


def test_iteration_addresses_audit_concatenates(monkeypatch: pytest.MonkeyPatch):
    capture: dict[str, Any] = {}
    _stub_judge(monkeypatch, JudgeResult(1.0, "ok", "", True), capture)
    registry["judge.artifacts.iteration_addresses_audit"](
        {"_peso": 6, "role_iteration": "assistente_v2", "role_audit": "auditoria_v1"},
        _ev(
            _entry("assistente_v2", content="V2-X"),
            _entry("auditoria_v1", content="A1-Y"),
        ),
    )
    assert "V2-X" in capture["content"]
    assert "A1-Y" in capture["content"]
    assert "=== ITERAÇÃO" in capture["content"]
    assert "=== AUDITORIA A SER ABORDADA" in capture["content"]
