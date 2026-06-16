"""Unit tests pro generate_sql — mocka http_post, não bate na API real."""

from __future__ import annotations

import json
from typing import Any

from app.gemini import (
    GEMINI_API_URL,
    SqlGenResult,
    _gemini_response_stub,
    generate_sql,
)


class FakeResp:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text or json.dumps(body or {})

    def json(self) -> dict[str, Any]:
        if self._body is None:
            raise ValueError("no json body")
        return self._body


def _ok(sql: str) -> FakeResp:
    return FakeResp(200, body=_gemini_response_stub(json.dumps({"sql": sql})))


def test_generate_sql_happy_path():
    captured: dict[str, Any] = {}

    def fake_post(url: str, json: dict[str, Any], timeout: int) -> FakeResp:
        captured["url"] = url
        captured["body"] = json
        return _ok("SELECT SUM(valor) FROM contratos")

    result = generate_sql(
        "qual o total dos contratos",
        "CREATE TABLE contratos (valor REAL);",
        api_key="fake-key",
        http_post=fake_post,
    )
    assert result == SqlGenResult(sql="SELECT SUM(valor) FROM contratos", ok=True, error="")
    assert GEMINI_API_URL in captured["url"]
    assert captured["body"]["generationConfig"]["temperature"] == 0.0
    # o esquema e o pedido vão no prompt
    sent = captured["body"]["contents"][0]["parts"][0]["text"]
    assert "contratos" in sent
    assert "qual o total dos contratos" in sent


def test_generate_sql_missing_key_is_infra_failure():
    result = generate_sql("x", "schema", api_key="")
    assert result.ok is False
    assert result.sql == ""


def test_generate_sql_http_error_is_infra_failure():
    def fake_post(*_a: Any, **_k: Any) -> FakeResp:
        return FakeResp(500, text="boom")

    result = generate_sql("x", "schema", api_key="k", http_post=fake_post)
    assert result.ok is False
    assert "HTTP 500" in result.error


def test_generate_sql_empty_is_infra_failure():
    def fake_post(*_a: Any, **_k: Any) -> FakeResp:
        return _ok("")

    result = generate_sql("x", "schema", api_key="k", http_post=fake_post)
    assert result.ok is False
