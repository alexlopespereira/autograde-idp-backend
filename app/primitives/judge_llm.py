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
        "papel ou categoria de um componente).\n\n"
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

    # Tipagem (humanos/IA) é OPCIONAL — só entra no rubric se o YAML pedir.
    # Permite reuso da primitive em exercícios que não exigem tipologia
    # humano/IA. Default (min_humans=min_ai=0): mapa avaliado pelo total +
    # consistência + decisões, sem cobrar categoria.
    if min_humans > 0 or min_ai > 0:
        typing_clause = f", ≥{min_humans} humanos, ≥{min_ai} IA"
        contagem_note = "tipagem (humanos/IA) corretos"
    else:
        typing_clause = ""
        contagem_note = "categorias coerentes"
    rubrica = (
        f"Avalie 3 aspectos do mapa de atores (≥{min_actors} atores"
        f"{typing_clause}):\n"
        f"1. CONTAGEM: número e {contagem_note}.\n"
        "2. CONSISTÊNCIA: todo ator no mapa aparece nominalmente no "
        "transcript do /grill-me (cross-reference).\n"
        "3. DECISÕES CITADAS: o mapa cita explicitamente ≥2 decisões "
        "tomadas durante o grill que motivaram categorização "
        "(ex: 'classifiquei o fornecedor X como ator indireto após pergunta 3').\n\n"
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


# ---------------------------------------------------------------------------
# Primitive: audit_finds_real_issues (B10 — cadeia de auditoria)
# ---------------------------------------------------------------------------


@register("judge.artifacts.audit_finds_real_issues")
def audit_finds_real_issues(args: dict, evidence: dict) -> CriterioResult:
    """Avalia se a auditoria aponta ≥1 falha REAL (não cosmética).

    Recebe AUDITORIA + PESQUISA AUDITADA concatenadas pro judge poder
    confirmar que as falhas apontadas são reais em relação ao texto auditado.
    Substitui semanticamente ``divergence_real`` no fluxo de 5 arquivos do 2.1
    (síntese morreu; a divergência agora vem dentro da auditoria).
    """
    peso = _peso(args)
    role_audit = _str_arg(args, "role_audit", "auditoria_v1")
    role_audited = _str_arg(args, "role_audited", "assistente_v1")
    entry_audit = _artifact_by_role(evidence, role_audit)
    entry_audited = _artifact_by_role(evidence, role_audited)
    if entry_audit is None or not entry_audit.get("exists"):
        return _miss_artifact(peso, role_audit)
    if entry_audited is None or not entry_audited.get("exists"):
        return _miss_artifact(peso, role_audited)
    rubrica = (
        "Você vai avaliar uma AUDITORIA feita por um assistente de IA sobre "
        "uma PESQUISA de outro assistente. Confirme se a auditoria identifica "
        "pelo menos UMA falha REAL — não cosmética — na pesquisa.\n\n"
        "Falha real: erro factual, lacuna de evidência, inferência mal-"
        "suportada, fonte fraca/ausente, atribuição incorreta, ator omitido "
        "relevante.\n\n"
        "Falha cosmética (rejeitar): formatação, estilo de escrita, uso de "
        "bullet vs prosa, ordem de apresentação.\n\n"
        "Score:\n"
        "- 1.0: ≥1 falha real claramente articulada com referência ao trecho "
        "auditado\n"
        "- 0.5: falha identificada mas vagamente OU sem referência clara\n"
        "- 0.0: apenas reformulação/echo da pesquisa, sem crítica real"
    )
    combined = (
        f"=== AUDITORIA ({role_audit}) ===\n"
        f"{entry_audit.get('content', '')}\n\n"
        f"=== PESQUISA AUDITADA ({role_audited}) ===\n"
        f"{entry_audited.get('content', '')}\n"
    )
    combined_entry: dict[str, Any] = {
        "content": combined,
        "headings": (entry_audit.get("headings") or [])
        + (entry_audited.get("headings") or []),
        "word_count": _int_arg(entry_audit, "word_count")
        + _int_arg(entry_audited, "word_count"),
        "links": (entry_audit.get("links") or [])
        + (entry_audited.get("links") or []),
    }
    label = f"{role_audit}+{role_audited}"
    return _materialize(peso, label, _call_judge(rubrica, label, combined_entry))


# ---------------------------------------------------------------------------
# Primitive: iteration_addresses_audit (B11 — cadeia de auditoria)
# ---------------------------------------------------------------------------


@register("judge.artifacts.iteration_addresses_audit")
def iteration_addresses_audit(args: dict, evidence: dict) -> CriterioResult:
    """Avalia se a iteração v2 ABORDA as falhas da audit_v1.

    Cada falha da auditoria deve receber tratamento (a) correção textual
    substantiva, (b) defesa fundamentada, ou (c) "em aberto" explícito.
    Ignorar = falha. Substitui ``resolution_offered`` no 2.1 (síntese morreu).
    """
    peso = _peso(args)
    role_iteration = _str_arg(args, "role_iteration", "assistente_v2")
    role_audit = _str_arg(args, "role_audit", "auditoria_v1")
    entry_iter = _artifact_by_role(evidence, role_iteration)
    entry_audit = _artifact_by_role(evidence, role_audit)
    if entry_iter is None or not entry_iter.get("exists"):
        return _miss_artifact(peso, role_iteration)
    if entry_audit is None or not entry_audit.get("exists"):
        return _miss_artifact(peso, role_audit)
    rubrica = (
        "Você vai avaliar se uma ITERAÇÃO (v2) de uma pesquisa ABORDA as "
        "falhas apontadas na AUDITORIA da v1. Cada falha da auditoria deve "
        "receber UM dos tratamentos:\n"
        "(a) corrigida com texto substantivamente diferente\n"
        "(b) defendida com argumento contrário citando evidência\n"
        "(c) marcada explicitamente como em-aberto / pendente\n\n"
        "Ignorar a falha (continuar do mesmo jeito sem mencionar) NÃO conta "
        "como abordar.\n\n"
        "Score:\n"
        "- 1.0: cada falha da auditoria tem tratamento (a), (b) ou (c)\n"
        "- 0.5: parte das falhas abordada, parte ignorada\n"
        "- 0.0: iteração ignora a auditoria OU é idêntica em substância à v1"
    )
    combined = (
        f"=== ITERAÇÃO ({role_iteration}) ===\n"
        f"{entry_iter.get('content', '')}\n\n"
        f"=== AUDITORIA A SER ABORDADA ({role_audit}) ===\n"
        f"{entry_audit.get('content', '')}\n"
    )
    combined_entry: dict[str, Any] = {
        "content": combined,
        "headings": (entry_iter.get("headings") or [])
        + (entry_audit.get("headings") or []),
        "word_count": _int_arg(entry_iter, "word_count")
        + _int_arg(entry_audit, "word_count"),
        "links": (entry_iter.get("links") or [])
        + (entry_audit.get("links") or []),
    }
    label = f"{role_iteration}+{role_audit}"
    return _materialize(peso, label, _call_judge(rubrica, label, combined_entry))


def _concat_entry(
    *parts: tuple[str, dict[str, Any]],
) -> dict[str, Any]:
    """Concatena ≥2 artefatos num único entry pro judge (mapa + transcript etc.).

    Cada ``part`` é ``(label, entry)``; o conteúdo vira blocos delimitados por
    ``=== <label> (<role>) ===`` e os metadados (headings/word_count/links) são
    somados. Mesmo padrão usado por actor_map_quality / audit_finds_real_issues.
    """
    blocks: list[str] = []
    headings: list[str] = []
    links: list[str] = []
    word_count = 0
    for label, entry in parts:
        blocks.append(f"=== {label} ===\n{entry.get('content', '')}\n")
        headings += list(entry.get("headings") or [])
        links += list(entry.get("links") or [])
        word_count += _int_arg(entry, "word_count")
    return {
        "content": "\n".join(blocks),
        "headings": headings,
        "links": links,
        "word_count": word_count,
    }


# ---------------------------------------------------------------------------
# Primitive: blueprint_quality (3.1 Parte C — Service Blueprint As-Is)
# ---------------------------------------------------------------------------


@register("judge.artifacts.blueprint_quality")
def blueprint_quality(args: dict, evidence: dict) -> CriterioResult:
    """Avalia um Service Blueprint As-Is (camadas + linhas + fail points).

    Recebe BLUEPRINT + TRANSCRIPT do /grill-me concatenados, pra cruzar os
    elementos do blueprint com a sessão que os destilou (mesma lógica de
    consistência do actor_map_quality, retargetada pro framework de Shostack
    das 5 swim lanes apresentado na Aula 3).
    """
    peso = _peso(args)
    role_map = _str_arg(args, "role_map", "blueprint_asis")
    role_transcript = _str_arg(args, "role_transcript", "grill_transcript")
    min_steps = _int_arg(args, "min_steps", 5)
    entry_map = _artifact_by_role(evidence, role_map)
    entry_tx = _artifact_by_role(evidence, role_transcript)
    if entry_map is None or not entry_map.get("exists"):
        return _miss_artifact(peso, role_map)
    if entry_tx is None or not entry_tx.get("exists"):
        return _miss_artifact(peso, role_transcript)
    rubrica = (
        "Avalie a qualidade de um SERVICE BLUEPRINT (As-Is) de um serviço "
        "público, no modelo de G. Lynn Shostack apresentado na Aula 3. "
        "Avalie 3 aspectos:\n\n"
        "1. CAMADAS (swim lanes): o blueprint distingue, de forma identificável, "
        "as faixas horizontais — Evidências Físicas, Ações do Cidadão/Usuário, "
        "Frontstage (linha de frente visível: humanos e/ou tecnologia visível), "
        "Backstage (bastidores invisíveis) e Processos de Suporte/retaguarda. "
        "Aceitar tabela, matriz ou mermaid desde que as faixas sejam "
        "distinguíveis. Aceita-se ao menos as linhas divisórias críticas "
        "(interação, visibilidade) implícitas na separação Frontstage/Backstage.\n"
        f"2. JORNADA: há ≥{min_steps} etapas SEQUENCIAIS mapeadas sob a ótica "
        "de quem CONSOME o serviço (não de quem opera), do início ao fim.\n"
        "3. DIAGNÓSTICO E CONSISTÊNCIA: o blueprint aponta ≥1 fail point / "
        "gargalo / ponto de atrito concreto; E todo elemento central do "
        "blueprint (etapas e atores citados) aparece nominalmente no "
        "transcript do /grill-me fornecido (cross-reference).\n\n"
        "Score = média dos 3 aspectos (cada um 0..1):\n"
        "- Aspecto 1: 1.0 se ≥4 das 5 camadas presentes e distinguíveis; "
        "0.5 se só separa Frontstage/Backstage; 0.0 se é lista linear sem camadas\n"
        f"- Aspecto 2: 1.0 se ≥{min_steps} etapas na ótica do cidadão; "
        "0.5 se poucas OU ótica do operador; 0.0 se não há jornada\n"
        "- Aspecto 3: 1.0 se ≥1 fail point E consistência total com o "
        "transcript; 0.5 se um dos dois; 0.0 se nenhum"
    )
    combined_entry = _concat_entry(
        (f"BLUEPRINT ({role_map})", entry_map),
        (f"TRANSCRIPT ({role_transcript})", entry_tx),
    )
    label = f"{role_map}+{role_transcript}"
    return _materialize(peso, label, _call_judge(rubrica, label, combined_entry))


# ---------------------------------------------------------------------------
# Primitive: tobe_improvements (3.1 Parte C — Service Blueprint To-Be)
# ---------------------------------------------------------------------------


@register("judge.artifacts.tobe_improvements")
def tobe_improvements(args: dict, evidence: dict) -> CriterioResult:
    """Avalia o Blueprint To-Be como reengenharia CONCRETA do As-Is.

    Recebe TO-BE + AS-IS concatenados: o judge precisa dos dois pra confirmar
    que as melhorias são deltas reais (e não um To-Be genérico desacoplado do
    diagnóstico). Cobre os 3 eixos da Aula 3: plataformas gov.br, redução de
    redundância/atrito e Desenho Universal / Linguagem Simples.
    """
    peso = _peso(args)
    role_tobe = _str_arg(args, "role_tobe", "blueprint_tobe")
    role_asis = _str_arg(args, "role_asis", "blueprint_asis")
    min_plataformas = _int_arg(args, "min_plataformas", 2)
    entry_tobe = _artifact_by_role(evidence, role_tobe)
    entry_asis = _artifact_by_role(evidence, role_asis)
    if entry_tobe is None or not entry_tobe.get("exists"):
        return _miss_artifact(peso, role_tobe)
    if entry_asis is None or not entry_asis.get("exists"):
        return _miss_artifact(peso, role_asis)
    rubrica = (
        "Avalie um SERVICE BLUEPRINT To-Be (serviço público futuro otimizado) "
        "como reengenharia CONCRETA do blueprint As-Is fornecido junto. "
        "Avalie 3 aspectos:\n\n"
        f"1. PLATAFORMAS gov.br: o To-Be emprega ≥{min_plataformas} recursos/"
        "plataformas concretos do ecossistema federal, ligados a etapas "
        "específicas do blueprint — ex.: Login/Conta gov.br (e nível "
        "Bronze/Prata/Ouro), CIN, APIs do Conecta.gov.br, Notifica gov.br, "
        "assinatura digital de documentos, PagTesouro, Design System gov.br. "
        "Citar o nome sem ligar a uma etapa NÃO conta.\n"
        "2. REDUÇÃO DE REDUNDÂNCIA/ATRITO: o To-Be elimina ≥1 etapa onerosa "
        "presente no As-Is (ex.: 'anexar cópia de documento', verificação "
        "manual, repetir CPF, recontar a história) e o delta é rastreável "
        "contra o As-Is — fica claro O QUE saiu/mudou e POR QUÊ.\n"
        "3. DESENHO UNIVERSAL / LINGUAGEM SIMPLES: o To-Be aplica ≥1 diretriz "
        "de acessibilidade ou Linguagem Simples acima da linha de visibilidade "
        "(frontstage universal e acessível) — não transfere complexidade "
        "organizacional para o cidadão.\n\n"
        "Score = média dos 3 aspectos (cada um 0..1):\n"
        f"- Aspecto 1: 1.0 se ≥{min_plataformas} plataformas ligadas a etapas; "
        "0.5 se 1 ou citadas sem ligação; 0.0 se nenhuma\n"
        "- Aspecto 2: 1.0 se ≥1 redundância eliminada com delta rastreável; "
        "0.5 se melhoria vaga sem rastrear o As-Is; 0.0 se To-Be ≈ As-Is\n"
        "- Aspecto 3: 1.0 se diretriz concreta de acessibilidade/Linguagem "
        "Simples; 0.5 se mencionada genericamente; 0.0 se ausente"
    )
    combined_entry = _concat_entry(
        (f"TO-BE ({role_tobe})", entry_tobe),
        (f"AS-IS ({role_asis})", entry_asis),
    )
    label = f"{role_tobe}+{role_asis}"
    return _materialize(peso, label, _call_judge(rubrica, label, combined_entry))
