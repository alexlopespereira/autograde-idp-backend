"""Gemini API client para gradear respostas subjetivas de exercícios.

Chama `generateContent` no `gemini-2.5-flash` com structured output (JSON
schema) pra retornar `{nota: int, feedback: str}`. Fallback em caso de
qualquer falha: nota máxima + flag `ok=False` pra log. Decisão: não punir
aluno por bug nosso (princípio: falha do grader → benefício da dúvida).

Princípio 11 CLAUDE.md: chamada cobrada (Gemini flash é barato mas tem
custo financeiro). Caller controla rate-limit antes de chamar.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)
GEMINI_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class GeminiResult:
    """Resultado de grading. `ok=False` indica fallback (nota máxima por convenção)."""

    nota: int
    feedback: str
    ok: bool


def _build_prompt(
    texto_pergunta: str, criterios_avaliacao: str, resposta_aluno: str, peso: int
) -> str:
    return (
        "Você está avaliando a resposta de um aluno a uma pergunta de reflexão num "
        "exercício de programação. Atribua uma nota inteira de 0 a "
        f"{peso} com base APENAS nos critérios de avaliação fornecidos.\n\n"
        f"Pergunta: {texto_pergunta}\n\n"
        f"Critérios de avaliação: {criterios_avaliacao}\n\n"
        f"Resposta do aluno: {resposta_aluno}\n\n"
        "Responda APENAS com um JSON no formato "
        '{"nota": <int>, "feedback": "<feedback curto em português, max 200 chars>"}.'
    )


def grade_resposta(
    texto_pergunta: str,
    criterios_avaliacao: str,
    resposta_aluno: str,
    peso: int,
    *,
    api_key: str | None = None,
    http_post: Callable[..., requests.Response] = requests.post,
) -> GeminiResult:
    """Avalia uma resposta via Gemini. Retorna nota inteira [0, peso] + feedback.

    Em qualquer falha (sem API key, HTTP error, JSON inválido, nota fora de range)
    retorna fallback: nota=peso (máxima), feedback descrevendo o erro, ok=False.
    """
    key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")
    if not key:
        log.error("gemini_unavailable reason=missing_api_key")
        return GeminiResult(
            nota=peso, feedback="gemini_unavailable: GEMINI_API_KEY ausente", ok=False
        )

    prompt = _build_prompt(texto_pergunta, criterios_avaliacao, resposta_aluno, peso)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "object",
                "properties": {
                    "nota": {"type": "integer"},
                    "feedback": {"type": "string"},
                },
                "required": ["nota", "feedback"],
            },
            "temperature": 0.2,
        },
    }
    try:
        resp = http_post(
            f"{GEMINI_API_URL}?key={key}",
            json=payload,
            timeout=GEMINI_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.error("gemini_unavailable reason=network err=%s", exc)
        return GeminiResult(nota=peso, feedback=f"gemini_unavailable: rede ({exc})", ok=False)

    if resp.status_code != 200:
        log.error(
            "gemini_unavailable reason=http status=%d body=%s", resp.status_code, resp.text[:200]
        )
        return GeminiResult(
            nota=peso, feedback=f"gemini_unavailable: HTTP {resp.status_code}", ok=False
        )

    try:
        body = resp.json()
        text = body["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        nota_raw = int(parsed["nota"])
        feedback = str(parsed.get("feedback", "")).strip()[:300]
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        log.error("gemini_unavailable reason=parse err=%s body=%s", exc, resp.text[:200])
        return GeminiResult(nota=peso, feedback=f"gemini_unavailable: parse ({exc})", ok=False)

    if nota_raw < 0:
        nota = 0
    elif nota_raw > peso:
        nota = peso
    else:
        nota = nota_raw

    log.info("gemini_ok peso=%d nota=%d feedback_len=%d", peso, nota, len(feedback))
    return GeminiResult(nota=nota, feedback=feedback or "(sem feedback)", ok=True)


def grade_respostas(
    perguntas_e_respostas: list[tuple[str, str, str, int]],
    *,
    api_key: str | None = None,
    grader: Callable[..., GeminiResult] = grade_resposta,
) -> list[GeminiResult]:
    """Grades múltiplas perguntas. Lista de tuplas (texto, criterios, resposta, peso).

    Sequencial — paralelizar agora é otimização prematura (poucas perguntas por
    exercício na prática).
    """
    return [
        grader(texto, criterios, resposta, peso, api_key=api_key)
        for texto, criterios, resposta, peso in perguntas_e_respostas
    ]


def _gemini_response_stub(text: str) -> dict[str, Any]:
    """Helper exposto pros tests construírem payload no formato real do Gemini."""
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}
