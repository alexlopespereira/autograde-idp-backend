"""Concorrência real entre threads e retries simulados sem chamadas cobradas."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

from app import llm_http
from app.gemini import generate_sql, grade_artifact, grade_resposta


class Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.body = body or {}
        self.headers = headers or {}
        self.text = json.dumps(self.body)

    def json(self):
        return self.body


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(llm_http.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm_http.random, "uniform", lambda a, b: 0.25)
    # Cada teste começa com vagas novas, sem alterar configuração de produção.
    monkeypatch.setattr(llm_http, "_gates", {})


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_http_retries_at_most_twice(status, monkeypatch):
    attempts, delays = [], []
    response = Response(status)
    monkeypatch.setattr(llm_http.time, "sleep", delays.append)

    def post(*args, **kwargs):
        attempts.append(kwargs)
        return response

    assert llm_http.post_with_retries("openai", "url", timeout=30, http_post=post) is response
    assert len(attempts) == 3
    assert delays == [1.25, 2.25]
    assert all(a["timeout"] == 30 for a in attempts)


@pytest.mark.parametrize("status", [200, 400, 401, 402, 403, 404, 422, 501])
def test_non_transient_http_not_retried(status):
    attempts = []

    def post(*args, **kwargs):
        attempts.append(1)
        return Response(status)

    llm_http.post_with_retries("openai", "url", timeout=30, http_post=post)
    assert len(attempts) == 1


@pytest.mark.parametrize("error", [
    {"code": "insufficient_quota"},
    {"code": "billing_hard_limit_reached"},
    {"code": "quota_exceeded"},
    {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProject"}]}]},
])
def test_exhausted_quota_not_retried(error):
    attempts = []

    def post(*args, **kwargs):
        attempts.append(1)
        return Response(429, {"error": error})

    llm_http.post_with_retries("gemini", "url", timeout=30, http_post=post)
    assert len(attempts) == 1


@pytest.mark.parametrize("exception", [requests.Timeout, requests.ConnectionError])
def test_transient_network_error_retried_and_reraised(exception):
    attempts = []

    def post(*args, **kwargs):
        attempts.append(1)
        raise exception("offline")

    with pytest.raises(exception):
        llm_http.post_with_retries("openai", "url", timeout=30, http_post=post)
    assert len(attempts) == 3


def test_ssl_error_not_retried():
    attempts = []

    def post(*args, **kwargs):
        attempts.append(1)
        raise requests.exceptions.SSLError("invalid certificate")

    with pytest.raises(requests.exceptions.SSLError):
        llm_http.post_with_retries("openai", "url", timeout=30, http_post=post)
    assert len(attempts) == 1


def test_retry_after_respected_and_success_stops_retries(monkeypatch):
    responses = iter([Response(429, headers={"Retry-After": "7"}), Response()])
    delays = []
    monkeypatch.setattr(llm_http.time, "sleep", delays.append)
    result = llm_http.post_with_retries(
        "openai", "url", timeout=30, http_post=lambda *a, **k: next(responses)
    )
    assert result.status_code == 200
    assert delays == [7.25]


def test_long_retry_after_returns_error_without_waiting():
    response = Response(503, headers={"Retry-After": "120"})
    assert llm_http.post_with_retries(
        "gemini", "url", timeout=30, http_post=lambda *a, **k: response
    ) is response


def test_http_date_retry_after():
    date = datetime.now(timezone.utc) + timedelta(seconds=15)
    response = Response(503, headers={"Retry-After": format_datetime(date)})
    assert 13 <= llm_http._retry_delay(response, 0) <= 16


def test_limit_shared_by_all_requests_and_separate_for_each_provider(monkeypatch):
    monkeypatch.setenv("LLM_MAX_CONCURRENT_OPENAI", "2")
    started, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = maximum = calls = 0

    def post(*args, **kwargs):
        nonlocal active, maximum, calls
        with lock:
            active += 1
            maximum = max(maximum, active)
            calls += 1
            if active == 2:
                started.set()
        assert release.wait(3)
        with lock:
            active -= 1
        return Response()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(llm_http.post_with_retries, "openai", "url",
                               timeout=3, http_post=post) for _ in range(8)]
        try:
            assert started.wait(2)
            assert calls == 2
            # Gemini não deve ficar bloqueado pelas vagas ocupadas da OpenAI.
            assert llm_http.post_with_retries(
                "gemini", "url", timeout=0.1, http_post=lambda *a, **k: Response()
            ).status_code == 200
        finally:
            release.set()
        assert all(f.result().status_code == 200 for f in futures)
    assert maximum == 2
    assert calls == 8


def test_permit_released_before_retry_wait_and_after_exceptions(monkeypatch):
    monkeypatch.setenv("LLM_MAX_CONCURRENT_OPENAI", "1")
    attempts = []

    def post(*a, **k):
        attempts.append(1)
        if len(attempts) == 1:
            raise requests.Timeout()
        return Response()

    def sleep(seconds):
        assert llm_http.post_with_retries(
            "openai", "url", timeout=0.1, http_post=lambda *a, **k: Response()
        ).status_code == 200

    monkeypatch.setattr(llm_http.time, "sleep", sleep)
    assert llm_http.post_with_retries("openai", "url", timeout=1, http_post=post).status_code == 200


def test_queue_timeout_does_not_call_provider(monkeypatch):
    gate = threading.BoundedSemaphore(1)
    monkeypatch.setitem(llm_http._gates, "openai", gate)
    with gate:
        with pytest.raises(requests.Timeout, match="ocupado"):
            llm_http.post_with_retries(
                "openai", "url", timeout=0,
                http_post=lambda *a, **k: pytest.fail("provider must not be called"),
            )


@pytest.mark.parametrize("kind", ["short", "artifact", "sql"])
def test_all_llm_callers_recover_from_503(kind):
    attempts = []

    def post(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            return Response(503)
        if kind == "sql":
            return Response(body={"candidates": [{"content": {"parts": [
                {"text": json.dumps({"sql": "SELECT 1"})},
            ]}}]})
        value = {"nota": 5, "feedback": "ok"} if kind == "short" else {
            "score": 0.5, "evidence_quote": "texto", "missing": "exemplo",
        }
        return Response(body={"status": "completed", "output": [{
            "type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}],
        }]})

    if kind == "short":
        result = grade_resposta("q", "c", "r", 10, api_key="k", http_post=post)
    elif kind == "artifact":
        result = grade_artifact("r", "texto", "texto", [], 1, 0, api_key="k", http_post=post)
    else:
        result = generate_sql("q", "schema", api_key="k", http_post=post)
    assert result.ok
    assert len(attempts) == 2
