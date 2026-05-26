"""judge.artifacts.* primitives — LLM-as-judge sobre artefatos textuais.

Cada primitive recebe rubrica explícita em ``args``, lookup do artefato pelo
``role`` no ``evidence['artifacts']``, chama ``app.gemini.grade_artifact`` com
``temperature=0`` e converte ``score * peso → points_earned``.

Convenções:
* ``ok=False`` do Gemini → score=1.0 (fallback "benefício da dúvida", mesma
  política do ``grade_resposta``). Mensagem indica fallback pro log do prof.
* Threshold de pass: ``score >= 0.5`` é "passed" (≥metade dos pontos). Crítica
  pra UI de boletim que mostra ✅/❌ — abaixo de 0.5 marca como falha mesmo
  com points_earned > 0, sinalizando "passou raspando, revise".
"""
from __future__ import annotations

from typing import Any

from app.gemini import JudgeResult, grade_artifact

from . import CriterioResult, register

PASS_THRESHOLD = 0.5


def _peso(args: dict) -> int:
    try:
        return int(args.get("_peso", 0))
    except (TypeError, ValueError):
        return 0


def _artifacts_list(evidence: dict) -> list[dict[str, Any]]:
    raw = evidence.get("artifacts") if isinstance(evidence, dict) else None
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    return []


def _artifact_by_role(evidence: dict, role: str) -> dict[str, Any] | None:
    for entry in _artifacts_list(evidence):
        if entry.get("role") == role:
            return entry
    return None


def _str_arg(args: dict, key: str, default: str = "") -> str:
    val = args.get(key, default)
    return str(val) if val is not None else default


def _int_arg(args: dict, key: str, default: int = 0) -> int:
    try:
        return int(args.get(key, default))
    except (TypeError, ValueError):
        return default


def _list_arg(args: dict, key: str) -> list[str]:
    raw = args.get(key) or []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw]


def _materialize(
    peso: int, role: str, result: JudgeResult
) -> CriterioResult:
    """Converte JudgeResult em CriterioResult conforme peso."""
    points_earned = int(round(result.score * peso))
    if points_earned < 0:
        points_earned = 0
    if points_earned > peso:
        points_earned = peso
    passed = result.score >= PASS_THRESHOLD
    if not result.ok:
        message = (
            f"[fallback judge] {role}: {result.missing or 'erro Gemini'}. "
            f"Nota máxima PROVISÓRIA por convenção (não pune aluno por bug "
            f"nosso) — sujeita a re-correção quando o judge voltar."
        )
        return CriterioResult(True, peso, peso, message, degraded=True)
    quote_preview = result.evidence_quote.replace("\n", " ")[:160]
    if passed:
        msg = f"score={result.score:.2f}. evidência: {quote_preview!r}"
        if result.missing:
            msg += f". para 1.0 faltaria: {result.missing}"
    else:
        msg = (
            f"score={result.score:.2f} (<{PASS_THRESHOLD}). "
            f"faltou: {result.missing or '(não especificado)'}. "
            f"evidência citada: {quote_preview!r}"
        )
    return CriterioResult(passed, points_earned, peso, msg)


def _call_judge(rubrica_text: str, role: str, entry: dict[str, Any]) -> JudgeResult:
    return grade_artifact(
        rubrica_text=rubrica_text,
        role=role,
        content=str(entry.get("content", "")),
        headings=list(entry.get("headings") or []),
        word_count=_int_arg(entry, "word_count"),
        n_links=len(entry.get("links") or []),
    )


def _miss_artifact(peso: int, role: str) -> CriterioResult:
    return CriterioResult(False, 0, peso, f"artefato {role!r} ausente para judge")


# ---------------------------------------------------------------------------
# Primitive: meta_prompt_quality (A1–A5 consolidado)
# ---------------------------------------------------------------------------

_META_PROMPT_RUBRIC_HEADER = (
    "Avalie a qualidade de um META-PROMPT que o aluno escreveu para iniciar "
    "uma sessão de deep research em um assistente de IA. O meta-prompt deve "
    "ATENDER A TODOS os sub-critérios abaixo; score é proporcional a quantos "
    "ele atende substantivamente (não cosmeticamente).\n\n"
    "Sub-critérios:"
)


@register("judge.artifacts.meta_prompt_quality")
def meta_prompt_quality(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    role = _str_arg(args, "role", "meta_prompt")
    sub_criterios = _list_arg(args, "sub_criterios")
    entry = _artifact_by_role(evidence, role)
    if entry is None or not entry.get("exists"):
        return _miss_artifact(peso, role)
    bullets = "\n".join(f"- {c}" for c in sub_criterios) or "- (rubrica vazia)"
    rubrica = (
        f"{_META_PROMPT_RUBRIC_HEADER}\n{bullets}\n\n"
        "Atribua score:\n"
        "- 1.0: TODOS os sub-critérios atendidos substantivamente\n"
        "- 0.0: meta-prompt genérico, omite ≥3 sub-critérios\n"
        "- Interpole proporcionalmente para casos intermediários"
    )
    return _materialize(peso, role, _call_judge(rubrica, role, entry))


# ---------------------------------------------------------------------------
# Primitive: divergence_real (B4)
# ---------------------------------------------------------------------------


@register("judge.artifacts.divergence_real")
def divergence_real(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    role = _str_arg(args, "role", "synthesis")
    entry = _artifact_by_role(evidence, role)
    if entry is None or not entry.get("exists"):
        return _miss_artifact(peso, role)
    rubrica = (
        "Este artefato é a síntese adversarial entre relatórios de dois "
        "assistentes de IA. Avalie se a síntese identifica pelo menos UMA "
        "divergência REAL — não cosmética — entre os relatórios.\n\n"
        "Divergência real: A1 e A2 chegam a conclusões diferentes sobre o "
        "mesmo fato verificável (ex: existência de um ator, taxa de uso, "
        "natureza humana vs IA de um componente).\n\n"
        "Divergência cosmética (rejeitar): A1 usa bullet, A2 usa tabela; A1 "
        "lista 5 atores, A2 lista 6 mas são os mesmos; ordem de apresentação.\n\n"
        "Score:\n"
        "- 1.0: ≥1 divergência real, claramente articulada\n"
        "- 0.5: divergência identificada mas vagamente\n"
        "- 0.0: apenas resumo/sobreposição; nenhuma divergência real"
    )
    return _materialize(peso, role, _call_judge(rubrica, role, entry))


# ---------------------------------------------------------------------------
# Primitive: resolution_offered (B5)
# ---------------------------------------------------------------------------


@register("judge.artifacts.resolution_offered")
def resolution_offered(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    role = _str_arg(args, "role", "synthesis")
    entry = _artifact_by_role(evidence, role)
    if entry is None or not entry.get("exists"):
        return _miss_artifact(peso, role)
    rubrica = (
        "Avalie se a síntese, ao identificar divergências, propõe RESOLUÇÃO: "
        "qual posição é mais defensável e por quê, OU declara explicitamente "
        "como pergunta em aberto que será investigada adiante.\n\n"
        "Score:\n"
        "- 1.0: cada divergência tem resolução fundamentada OU é marcada "
        "como aberta com plano concreto\n"
        "- 0.5: parte das divergências resolvida, parte ignorada\n"
        "- 0.0: divergências listadas mas síntese não toma posição"
    )
    return _materialize(peso, role, _call_judge(rubrica, role, entry))


# ---------------------------------------------------------------------------
# Primitive: evolution_substantive (B7)
# ---------------------------------------------------------------------------


@register("judge.artifacts.evolution_substantive")
def evolution_substantive(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    role = _str_arg(args, "role", "synthesis")
    min_iterations = _int_arg(args, "min_iterations", 2)
    entry = _artifact_by_role(evidence, role)
    if entry is None or not entry.get("exists"):
        return _miss_artifact(peso, role)
    rubrica = (
        "A síntese tem versões iterativas (## v1, ## v2, ...). Avalie se "
        f"existem ≥{min_iterations} iterações E se cada bloco "
        "'### Mudanças nesta versão' (a partir de v2) atende:\n"
        "1. Cita ≥1 delta CONCRETO: mudou posição, descobriu ator novo, "
        "   resolveu divergência, abriu pergunta nova.\n"
        "2. Cita o GATILHO concreto: pergunta N do grill-me, nova "
        "   evidência X, resposta de A1/A2 reaberto.\n\n"
        "Rejeitar (score 0):\n"
        "- Reescrita cosmética sem novo conteúdo\n"
        "- 'Refleti melhor' / 'pensando bem' (sem gatilho)\n"
        "- Iterações duplicadas (v2 == v1)\n\n"
        "Score:\n"
        "- 1.0: cada iteração (≥2) tem delta + gatilho concretos\n"
        "- 0.5: tem iterações mas gatilhos são vagos OU 1 iteração só\n"
        "- 0.0: cosmético OU ausente"
    )
    return _materialize(peso, role, _call_judge(rubrica, role, entry))


# ---------------------------------------------------------------------------
# Primitive: actor_map_quality (C2+C3+C5 consolidado)
# ---------------------------------------------------------------------------


@register("judge.artifacts.actor_map_quality")
def actor_map_quality(args: dict, evidence: dict) -> CriterioResult:
    """Avalia mapa de atores: consistência com transcript, contagem, decisões.

    Recebe MAPA + TRANSCRIPT concatenados no prompt (separados por marcadores).
    """
    peso = _peso(args)
    role_map = _str_arg(args, "role_map", "actor_map")
    role_transcript = _str_arg(args, "role_transcript", "grill_transcript")
    min_actors = _int_arg(args, "min_actors", 7)
    min_humans = _int_arg(args, "min_humans", 2)
    min_ai = _int_arg(args, "min_ai", 2)
    entry_map = _artifact_by_role(evidence, role_map)
    entry_tx = _artifact_by_role(evidence, role_transcript)
    if entry_map is None or not entry_map.get("exists"):
        return _miss_artifact(peso, role_map)
    if entry_tx is None or not entry_tx.get("exists"):
        return _miss_artifact(peso, role_transcript)

    rubrica = (
        f"Avalie 3 aspectos do mapa de atores (≥{min_actors} atores, "
        f"≥{min_humans} humanos, ≥{min_ai} IA):\n"
        "1. CONTAGEM: número e tipagem corretos.\n"
        "2. CONSISTÊNCIA: todo ator no mapa aparece nominalmente no "
        "transcript do /grill-me (cross-reference).\n"
        "3. DECISÕES CITADAS: o mapa cita explicitamente ≥2 decisões "
        "tomadas durante o grill que motivaram categorização "
        "(ex: 'classifiquei IVR como IA após pergunta 3').\n\n"
        "Score = média dos 3 aspectos (cada um 0..1):\n"
        "- Aspecto 1: 1.0 se contagem bate exata; 0.5 se quase (off-by-one); "
        "0.0 se falha categoria mínima\n"
        "- Aspecto 2: 1.0 se 100% dos atores do mapa estão no transcript; "
        "proporcional caso contrário\n"
        "- Aspecto 3: 1.0 se ≥2 decisões citadas; 0.5 se 1; 0.0 se nenhuma"
    )

    # Conteúdo concatenado: mapa primeiro, transcript depois (delimitado).
    combined = (
        f"=== MAPA ({role_map}) ===\n"
        f"{entry_map.get('content', '')}\n\n"
        f"=== TRANSCRIPT ({role_transcript}) ===\n"
        f"{entry_tx.get('content', '')}\n"
    )
    combined_entry: dict[str, Any] = {
        "content": combined,
        "headings": (entry_map.get("headings") or []) + (entry_tx.get("headings") or []),
        "word_count": _int_arg(entry_map, "word_count") + _int_arg(entry_tx, "word_count"),
        "links": (entry_map.get("links") or []) + (entry_tx.get("links") or []),
    }
    return _materialize(
        peso,
        f"{role_map}+{role_transcript}",
        _call_judge(rubrica, f"{role_map}+{role_transcript}", combined_entry),
    )


# ---------------------------------------------------------------------------
# Primitive: grill_rounds (C1)
# ---------------------------------------------------------------------------


@register("judge.artifacts.grill_rounds")
def grill_rounds(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    role = _str_arg(args, "role", "grill_transcript")
    min_rounds = _int_arg(args, "min_rounds", 8)
    entry = _artifact_by_role(evidence, role)
    if entry is None or not entry.get("exists"):
        return _miss_artifact(peso, role)
    rubrica = (
        f"Conte rodadas REAIS de pergunta-resposta neste transcript do "
        f"/grill-me. Mínimo aceitável: {min_rounds}.\n\n"
        "Uma rodada = uma pergunta nova do Claude + uma resposta substantiva "
        "do aluno. Não contar:\n"
        "- Continuações ('continue', 'explique mais')\n"
        "- Confirmações curtas ('sim', 'ok')\n"
        "- Múltiplas perguntas embutidas como UMA rodada\n\n"
        f"Score:\n"
        f"- 1.0: ≥{min_rounds} rodadas reais\n"
        f"- 0.5: 50–99% do mínimo\n"
        f"- 0.0: <50% OU respostas evasivas ('tanto faz', 'você decide')"
    )
    return _materialize(peso, role, _call_judge(rubrica, role, entry))


# ---------------------------------------------------------------------------
# Primitive: relations_explicit (C4)
# ---------------------------------------------------------------------------


@register("judge.artifacts.relations_explicit")
def relations_explicit(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    role = _str_arg(args, "role", "actor_map")
    entry = _artifact_by_role(evidence, role)
    if entry is None or not entry.get("exists"):
        return _miss_artifact(peso, role)
    rubrica = (
        "Avalie se o mapa de atores tem RELAÇÕES EXPLÍCITAS entre atores — "
        "não lista solta. Aceitar:\n"
        "- Tabela RACI completa (R/A/C/I preenchidos para cada ator)\n"
        "- Diagrama mermaid com setas (-->) entre atores\n"
        "- Prosa estruturada que descreve handoffs entre atores\n\n"
        "Rejeitar:\n"
        "- Lista de bullets sem indicar quem se conecta com quem\n"
        "- Tabela só com colunas 'nome' e 'tipo'\n\n"
        "Score:\n"
        "- 1.0: relações explícitas e completas\n"
        "- 0.5: parcial (algumas relações, outras faltando)\n"
        "- 0.0: lista solta"
    )
    return _materialize(peso, role, _call_judge(rubrica, role, entry))
