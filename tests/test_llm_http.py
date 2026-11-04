"""Concorrência real entre threads e retries simulados sem chamadas cobradas."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock

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


@pytest.mark.parametrize("retry_first_call", [False, True])
def test_limit_shared_by_all_requests_and_separate_for_each_provider(monkeypatch, retry_first_call):
    monkeypatch.setenv("LLM_MAX_CONCURRENT_OPENAI", "2")
    started, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = maximum = calls = 0
    per_thread = threading.local()

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
        per_thread.calls = getattr(per_thread, "calls", 0) + 1
        return Response(503 if retry_first_call and per_thread.calls == 1 else 200)

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
    assert calls == (16 if retry_first_call else 8)


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


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("failures", [[], [503], [requests.Timeout("offline"), 429]])
def test_success_on_first_or_last_attempt_preserves_request(provider, failures, monkeypatch):
    success = requests.Response()
    success.status_code = 200
    outcomes = []
    for failure in failures:
        if isinstance(failure, Exception):
            outcomes.append(failure)
        else:
            response = requests.Response()
            response.status_code = failure
            response._content = b'{}'
            outcomes.append(response)
    post = Mock(side_effect=[*outcomes, success])
    sleep = Mock()
    monkeypatch.setattr(llm_http.time, "sleep", sleep)
    payload, headers = {"input": "answer"}, {"Authorization": "Bearer test-only"}

    assert llm_http.post_with_retries(
        provider, "https://example.test/grade", timeout=7,
        http_post=post, json=payload, headers=headers,
    ) is success
    assert post.call_count == len(failures) + 1
    assert sleep.call_count == len(failures)
    for call in post.call_args_list:
        assert call.args == ("https://example.test/grade",)
        assert call.kwargs == {"timeout": 7, "json": payload, "headers": headers}


@pytest.mark.parametrize("status,body", [
    (401, {}), (403, {}),
    (429, {"error": {"code": "insufficient_quota"}}),
    (429, {"error": {"details": [{"violations": [{"quotaId": "requests_per_day"}]}]}}),
])
def test_permanent_failure_after_transient_stops_immediately(status, body, monkeypatch):
    final = Response(status, body)
    post = Mock(side_effect=[Response(503), final])
    sleep = Mock()
    monkeypatch.setattr(llm_http.time, "sleep", sleep)
    assert llm_http.post_with_retries("gemini", "url", timeout=1, http_post=post) is final
    assert post.call_count == 2
    sleep.assert_called_once_with(1.25)


@pytest.mark.parametrize("header,expected", [
    ("0", 0.25), ("-5", 0.25), ("0.5", 0.75), ("30", 30.25),
    ("30.01", None), ("invalid", 1.25), ("nan", 1.25), ("inf", 1.25),
])
def test_retry_after_boundaries_control_whether_another_call_is_made(header, expected, monkeypatch):
    first, success = Response(503, headers={"retry-after": header}), Response()
    post = Mock(side_effect=[first, success])
    sleep = Mock()
    monkeypatch.setattr(llm_http.time, "sleep", sleep)
    result = llm_http.post_with_retries("openai", "url", timeout=1, http_post=post)
    if expected is None:
        assert result is first
        post.assert_called_once()
        sleep.assert_not_called()
    else:
        assert result is success
        assert post.call_count == 2
        sleep.assert_called_once_with(expected)


@pytest.mark.parametrize("error", [None, "unstructured", {"details": None},
    {"details": [None, "unknown"]},
    {"details": [{"violations": [{"quotaId": "RequestsPerMinute"}]}]},
])
def test_unknown_or_minute_quota_error_can_recover(error):
    success = Response()
    post = Mock(side_effect=[Response(429, {"error": error}), success])
    assert llm_http.post_with_retries("gemini", "url", timeout=1, http_post=post) is success
    assert post.call_count == 2


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("setting,limit", [
    (None, 4), ("invalid", 4), ("0", 1), ("-2", 1), ("2", 2),
])
def test_configured_capacity_and_queue_timeout(provider, setting, limit, monkeypatch):
    name = f"LLM_MAX_CONCURRENT_{provider.upper()}"
    if setting is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, setting)
    gate = llm_http._gate(provider)
    for _ in range(limit):
        assert gate.acquire(blocking=False)
    try:
        with pytest.raises(requests.Timeout, match="ocupado"):
            llm_http.post_with_retries(provider, "url", timeout=0, http_post=Mock())
        assert not gate.acquire(blocking=False)
    finally:
        for _ in range(limit):
            gate.release()
    post = Mock(return_value=Response())
    assert llm_http.post_with_retries(provider, "url", timeout=0, http_post=post).status_code == 200
    post.assert_called_once()


@pytest.mark.parametrize("failure", [
    requests.Timeout("offline"), requests.ConnectionError("offline"),
    requests.exceptions.SSLError("certificate"), ValueError("unexpected"),
    Response(503), Response(403),
])
def test_capacity_restored_after_terminal_failure(failure, monkeypatch):
    monkeypatch.setenv("LLM_MAX_CONCURRENT_OPENAI", "1")
    post = (Mock(side_effect=failure) if isinstance(failure, Exception)
            else Mock(return_value=failure))
    if isinstance(failure, Exception):
        with pytest.raises(type(failure)):
            llm_http.post_with_retries("openai", "url", timeout=0, http_post=post)
    else:
        assert llm_http.post_with_retries("openai", "url", timeout=0, http_post=post) is failure
    success = Response()
    assert llm_http.post_with_retries(
        "openai", "url", timeout=0, http_post=Mock(return_value=success),
    ) is success


@pytest.mark.parametrize("kind", ["short", "artifact", "sql"])
@pytest.mark.parametrize("status", [200, 503])
def test_callers_report_failure_for_invalid_output_or_exhausted_retries(kind, status):
    post = Mock(return_value=Response(status, {"unexpected": "invalid model output"}))
    if kind == "short":
        result = grade_resposta("q", "c", "r", 10, api_key="k", http_post=post)
    elif kind == "artifact":
        result = grade_artifact("r", "texto", "texto", [], 1, 0, api_key="k", http_post=post)
    else:
        result = generate_sql("q", "schema", api_key="k", http_post=post)
    assert not result.ok
    assert post.call_count == (1 if status == 200 else 3)
