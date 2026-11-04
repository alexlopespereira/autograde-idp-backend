"""Limite por provedor e retries transitórios para as chamadas REST dos LLMs."""

from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import requests

log = logging.getLogger(__name__)
MAX_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 30
TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}
_gates: dict[str, threading.BoundedSemaphore] = {}
_gates_lock = threading.Lock()


def _gate(provider: str) -> threading.BoundedSemaphore:
    if provider not in {"openai", "gemini"}:
        raise ValueError("provedor LLM desconhecido")
    with _gates_lock:
        if provider not in _gates:
            try:
                limit = max(1, int(os.environ.get(f"LLM_MAX_CONCURRENT_{provider.upper()}", "4")))
            except ValueError:
                limit = 4
                log.warning("llm_invalid_concurrency provider=%s default=4", provider)
            _gates[provider] = threading.BoundedSemaphore(limit)
        return _gates[provider]


def _quota_exhausted(response: requests.Response) -> bool:
    """Quota diária/esgotamento de saldo não se resolve com retry imediato."""
    try:
        error = response.json().get("error", {})
        if not isinstance(error, dict):
            return False
        if error.get("code") in {
            "insufficient_quota", "billing_hard_limit_reached", "quota_exceeded",
        }:
            return True
        for detail in error.get("details", []):
            if not isinstance(detail, dict):
                continue
            for violation in detail.get("violations", []):
                quota_id = str(violation.get("quotaId", "")).lower()
                if "perday" in quota_id or "per_day" in quota_id:
                    return True
    except (ValueError, TypeError, AttributeError):
        pass
    return False


def _retry_delay(response: requests.Response | None, retry: int) -> float | None:
    """None: a espera solicitada é longa demais; devolva o erro ao aluno."""
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    delay = None
    if raw is not None:
        try:
            delay = float(raw)
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(str(raw))
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                delay = (date - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
    if delay is not None and math.isfinite(delay):
        delay = max(0.0, delay)
        if delay > MAX_RETRY_DELAY_SECONDS:
            return None
        return delay + random.uniform(0.0, 0.5)
    return 2 ** retry + random.uniform(0.0, 0.5)


def post_with_retries(
    provider: str,
    url: str,
    *,
    timeout: float,
    http_post: Callable[..., requests.Response] = requests.post,
    **kwargs,
) -> requests.Response:
    """No máximo três chamadas; libera a vaga antes de esperar pelo retry.

    O timeout HTTP por tentativa é preservado. A espera por uma vaga também
    é limitada a esse timeout; fila cheia retorna erro sem chamar o provedor.
    As vagas são compartilhadas por processo, entre todos os alunos. O deploy
    atual tem um worker e uma instância; escalar exige um limite distribuído.
    """
    gate = _gate(provider)
    for retry in range(MAX_RETRIES + 1):
        if not gate.acquire(timeout=timeout):
            raise requests.Timeout(f"avaliador {provider} ocupado: espera por vaga excedida")
        response = None
        try:
            try:
                response = http_post(url, timeout=timeout, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if retry == MAX_RETRIES or isinstance(exc, requests.exceptions.SSLError):
                    raise
        finally:
            gate.release()

        if response is not None:
            if response.status_code not in TRANSIENT_STATUSES or _quota_exhausted(response):
                return response
            if retry == MAX_RETRIES:
                return response
        delay = _retry_delay(response, retry)
        if delay is None:
            assert response is not None  # só existe com Retry-After HTTP
            return response
        log.warning(
            "llm_retry provider=%s attempt=%d status=%s delay=%.2f",
            provider, retry + 2,
            response.status_code if response is not None else "network", delay,
        )
        time.sleep(delay)
    raise AssertionError("retry loop terminou sem resultado")
