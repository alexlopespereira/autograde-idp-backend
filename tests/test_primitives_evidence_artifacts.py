"""Tests for evidence.artifacts.* primitives."""
from __future__ import annotations

from typing import Any

from app.primitives import registry


def _make_entry(
    role: str,
    *,
    exists: bool = True,
    word_count: int = 100,
    links: list[str] | None = None,
    headings: list[str] | None = None,
    content: str = "",
    sha256: str = "deadbeef",
    path: str | None = None,
) -> dict[str, Any]:
    return {
        "tool": "artifacts",
        "role": role,
        "path": path or f"{role}.md",
        "required": True,
        "exists": exists,
        "size_bytes": len(content.encode("utf-8")) if content else word_count * 6,
        "word_count": word_count,
        "sha256": sha256,
        "headings": headings or [],
        "links": links or [],
        "content": content,
        "captured_at": "2026-05-16T12:00:00+00:00",
    }


def _evidence(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"artifacts": list(entries)}


# ---------- evidence.artifacts.exists ---------------------------------------


def test_exists_passes_when_artifact_present_and_exists_true():
    r = registry["evidence.artifacts.exists"](
        {"_peso": 5, "role": "meta_prompt"},
        _evidence(_make_entry("meta_prompt")),
    )
    assert r.passed is True
    assert r.points_earned == 5
    assert "meta_prompt.md" in r.message


def test_exists_fails_when_artifact_missing_from_payload():
    r = registry["evidence.artifacts.exists"](
        {"_peso": 5, "role": "meta_prompt"},
        _evidence(),  # payload vazio
    )
    assert r.passed is False
    assert r.points_earned == 0
    assert "ausente" in r.message.lower()


def test_exists_fails_when_artifact_present_but_exists_false():
    r = registry["evidence.artifacts.exists"](
        {"_peso": 5, "role": "meta_prompt"},
        _evidence(_make_entry("meta_prompt", exists=False)),
    )
    assert r.passed is False
    assert "não existe" in r.message


# ---------- evidence.artifacts.word_count_min -------------------------------


def test_word_count_passes_when_above_minimum():
    r = registry["evidence.artifacts.word_count_min"](
        {"_peso": 8, "role": "report_ai_1", "min": 800},
        _evidence(_make_entry("report_ai_1", word_count=900)),
    )
    assert r.passed is True
    assert r.points_earned == 8
    assert "900" in r.message


def test_word_count_fails_when_below_minimum():
    r = registry["evidence.artifacts.word_count_min"](
        {"_peso": 8, "role": "report_ai_1", "min": 800},
        _evidence(_make_entry("report_ai_1", word_count=400)),
    )
    assert r.passed is False
    assert "400" in r.message and "800" in r.message


def test_word_count_fails_when_artifact_absent():
    r = registry["evidence.artifacts.word_count_min"](
        {"_peso": 8, "role": "report_ai_1", "min": 800},
        _evidence(),
    )
    assert r.passed is False


# ---------- evidence.artifacts.links_min ------------------------------------


def test_links_min_passes_when_enough_links():
    r = registry["evidence.artifacts.links_min"](
        {"_peso": 4, "role": "report_ai_1", "min": 3},
        _evidence(_make_entry("report_ai_1", links=["a", "b", "c", "d"])),
    )
    assert r.passed is True


def test_links_min_fails_when_insufficient():
    r = registry["evidence.artifacts.links_min"](
        {"_peso": 4, "role": "report_ai_1", "min": 3},
        _evidence(_make_entry("report_ai_1", links=["a", "b"])),
    )
    assert r.passed is False
    assert "2" in r.message and "3" in r.message


# ---------- evidence.artifacts.distinct_reports -----------------------------


def test_distinct_reports_passes_for_two_genuinely_different():
    r = registry["evidence.artifacts.distinct_reports"](
        {"_peso": 6, "roles": ["report_ai_1", "report_ai_2"]},
        _evidence(
            _make_entry("report_ai_1", sha256="aaa", content="Gemini analisa..."),
            _make_entry("report_ai_2", sha256="bbb", content="ChatGPT considera..."),
        ),
    )
    assert r.passed is True


def test_distinct_reports_fails_when_sha256_identical():
    r = registry["evidence.artifacts.distinct_reports"](
        {"_peso": 6, "roles": ["report_ai_1", "report_ai_2"]},
        _evidence(
            _make_entry("report_ai_1", sha256="aaa", content="X" * 100),
            _make_entry("report_ai_2", sha256="aaa", content="X" * 100),
        ),
    )
    assert r.passed is False
    assert "idênticos" in r.message


def test_distinct_reports_fails_when_first_500_chars_identical():
    same_prefix = "Y" * 500
    r = registry["evidence.artifacts.distinct_reports"](
        {"_peso": 6, "roles": ["report_ai_1", "report_ai_2"]},
        _evidence(
            _make_entry("report_ai_1", sha256="aaa", content=same_prefix + "A"),
            _make_entry("report_ai_2", sha256="bbb", content=same_prefix + "B"),
        ),
    )
    assert r.passed is False
    assert "500 chars" in r.message


def test_distinct_reports_fails_when_one_role_missing():
    r = registry["evidence.artifacts.distinct_reports"](
        {"_peso": 6, "roles": ["report_ai_1", "report_ai_2"]},
        _evidence(_make_entry("report_ai_1")),
    )
    assert r.passed is False
    assert "report_ai_2" in r.message


def test_distinct_reports_invalid_args():
    r = registry["evidence.artifacts.distinct_reports"](
        {"_peso": 6, "roles": ["only_one"]},
        _evidence(),
    )
    assert r.passed is False
    assert "≥2" in r.message


# ---------- evidence.artifacts.heading_pattern_min --------------------------


def test_heading_pattern_passes_when_versions_present():
    r = registry["evidence.artifacts.heading_pattern_min"](
        {"_peso": 4, "role": "synthesis", "pattern": r"^## v\d+", "min": 2},
        _evidence(
            _make_entry(
                "synthesis",
                headings=[
                    "# Síntese adversarial",
                    "## v1 — primeira leitura",
                    "### Divergências",
                    "## v2 — após grill-me",
                    "### Mudanças nesta versão",
                ],
            )
        ),
    )
    assert r.passed is True
    assert "2 headings" in r.message


def test_heading_pattern_fails_when_only_one_version():
    r = registry["evidence.artifacts.heading_pattern_min"](
        {"_peso": 4, "role": "synthesis", "pattern": r"^## v\d+", "min": 2},
        _evidence(
            _make_entry("synthesis", headings=["# H", "## v1", "## Divergências"]),
        ),
    )
    assert r.passed is False
    assert "1" in r.message


def test_heading_pattern_invalid_regex():
    r = registry["evidence.artifacts.heading_pattern_min"](
        {"_peso": 4, "role": "synthesis", "pattern": r"(unclosed", "min": 1},
        _evidence(_make_entry("synthesis")),
    )
    assert r.passed is False
    assert "regex inválida" in r.message


def test_heading_pattern_case_insensitive():
    r = registry["evidence.artifacts.heading_pattern_min"](
        {"_peso": 4, "role": "synthesis", "pattern": r"^## V\d+", "min": 1},
        _evidence(_make_entry("synthesis", headings=["## v1"])),
    )
    assert r.passed is True


# ---------- evidence.artifacts.cross_reference_required ---------------------


def test_cross_reference_passes_when_all_terms_in_b():
    actor_map_content = (
        "| Cidadão | Humano | ... |\n| IVR | IA | ... |\n| Atendente | Humano | ... |"
    )
    transcript_content = (
        "Pergunta 1: Você considera o Cidadão como ator inicial?\n"
        "Resposta: Sim, e também o IVR vem logo depois.\n"
        "Pergunta 2: E o Atendente?\n"
        "Resposta: O atendente entra quando o IVR escalona."
    )
    r = registry["evidence.artifacts.cross_reference_required"](
        {
            "_peso": 8,
            "role_a": "actor_map",
            "role_b": "grill_transcript",
            "pattern_in_a": r"\|\s*([A-Z][a-zà-ú]+)\s*\|",
        },
        _evidence(
            _make_entry("actor_map", content=actor_map_content),
            _make_entry("grill_transcript", content=transcript_content),
        ),
    )
    assert r.passed is True


def test_cross_reference_fails_when_term_missing_in_b():
    actor_map_content = "Atores: Cidadão, IVR, Auditor"
    transcript_content = "Falamos sobre Cidadão e IVR. Auditor nunca foi mencionado."
    # Auditor existe nos dois — pattern simples: palavras capitalizadas após "Atores:"
    r = registry["evidence.artifacts.cross_reference_required"](
        {
            "_peso": 8,
            "role_a": "actor_map",
            "role_b": "grill_transcript",
            "pattern_in_a": r"Supervisor",  # esse termo NÃO está no map, retorna sem matches
        },
        _evidence(
            _make_entry("actor_map", content=actor_map_content),
            _make_entry("grill_transcript", content=transcript_content),
        ),
    )
    # Sem matches em A → falha com mensagem específica.
    assert r.passed is False
    assert "nenhum match" in r.message.lower()


def test_cross_reference_fails_when_a_has_term_b_doesnt():
    r = registry["evidence.artifacts.cross_reference_required"](
        {
            "_peso": 8,
            "role_a": "actor_map",
            "role_b": "grill_transcript",
            "pattern_in_a": r"Auditor",
        },
        _evidence(
            _make_entry("actor_map", content="Atores: Cidadão, Auditor, IVR"),
            _make_entry("grill_transcript", content="Falamos sobre Cidadão. IVR também."),
        ),
    )
    assert r.passed is False
    assert "auditor" in r.message.lower()


# ---------- Defensive ------------------------------------------------------


def test_all_primitives_handle_missing_artifacts_key_gracefully():
    """Se evidence não tem 'artifacts', primitive não levanta."""
    for name in [
        "evidence.artifacts.exists",
        "evidence.artifacts.word_count_min",
        "evidence.artifacts.links_min",
        "evidence.artifacts.heading_pattern_min",
    ]:
        r = registry[name](
            {"_peso": 1, "role": "x", "min": 1, "pattern": ".*"},
            {},
        )
        assert r.passed is False
        assert r.points_earned == 0
