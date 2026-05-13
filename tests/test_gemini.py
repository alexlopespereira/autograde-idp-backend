"""Unit tests pro grader Gemini. Mocka o http_post — não bate na API real."""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from app.gemini import (
    GEMINI_API_URL,
    GeminiResult,
    _gemini_response_stub,
    grade_resposta,
    grade_respostas,
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


def _ok_response(nota: int, feedback: str = "ótimo") -> FakeResp:
    payload_text = json.dumps({"nota": nota, "feedback": feedback})
    return FakeResp(200, body=_gemini_response_stub(payload_text))


def test_grade_resposta_happy_path():
    captured: dict[str, Any] = {}

    def fake_post(url: str, json: dict[str, Any], timeout: int) -> FakeResp:
        captured["url"] = url
        captured["body"] = json
        return _ok_response(nota=8, feedback="boa explicação")

    result = grade_resposta(
        texto_pergunta="O que você entendeu?",
        criterios_avaliacao="Deve citar X e Y",
        resposta_aluno="entendi sobre X e Y",
        peso=10,
        api_key="fake-key",
        http_post=fake_post,
    )
    assert result == GeminiResult(nota=8, feedback="boa explicação", ok=True)
    assert GEMINI_API_URL in captured["url"]
    assert "fake-key" in captured["url"]
    assert captured["body"]["generationConfig"]["responseMimeType"] == "application/json"


def test_grade_resposta_clamps_above_peso():
    def fake_post(*_a: Any, **_k: Any) -> FakeResp:
        return _ok_response(nota=100)  # bem acima do peso

    result = grade_resposta("q", "c", "r", peso=10, api_key="k", http_post=fake_post)
    assert result.nota == 10
    assert result.ok is True


def test_grade_resposta_clamps_below_zero():
    def fake_post(*_a: Any, **_k: Any) -> FakeResp:
        return _ok_response(nota=-5)

    result = grade_resposta("q", "c", "r", peso=10, api_key="k", http_post=fake_post)
    assert result.nota == 0


def test_grade_resposta_fallback_no_api_key():
    result = grade_resposta("q", "c", "r", peso=10, api_key="", http_post=lambda *_a, **_k: None)  # type: ignore[arg-type]
    assert result.ok is False
    assert result.nota == 10  # peso máximo (não punir)
    assert "GEMINI_API_KEY" in result.feedback


def test_grade_resposta_fallback_http_error():
    def fake_post(*_a: Any, **_k: Any) -> FakeResp:
        return FakeResp(500, text="server exploded")

    result = grade_resposta("q", "c", "r", peso=15, api_key="k", http_post=fake_post)
    assert result.ok is False
    assert result.nota == 15
    assert "HTTP 500" in result.feedback


def test_grade_resposta_fallback_network_error():
    def fake_post(*_a: Any, **_k: Any) -> FakeResp:
        raise requests.ConnectionError("dns failed")

    result = grade_resposta("q", "c", "r", peso=7, api_key="k", http_post=fake_post)
    assert result.ok is False
    assert result.nota == 7
    assert "rede" in result.feedback


def test_grade_resposta_fallback_malformed_json():
    def fake_post(*_a: Any, **_k: Any) -> FakeResp:
        return FakeResp(200, body=_gemini_response_stub("isso nao eh json"))

    result = grade_resposta("q", "c", "r", peso=10, api_key="k", http_post=fake_post)
    assert result.ok is False
    assert result.nota == 10
    assert "parse" in result.feedback


def test_grade_resposta_fallback_missing_candidates():
    def fake_post(*_a: Any, **_k: Any) -> FakeResp:
        return FakeResp(200, body={"foo": "bar"})

    result = grade_resposta("q", "c", "r", peso=10, api_key="k", http_post=fake_post)
    assert result.ok is False
    assert result.nota == 10


def test_grade_resposta_uses_env_var_when_api_key_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    captured: dict[str, Any] = {}

    def fake_post(url: str, json: dict[str, Any], timeout: int) -> FakeResp:
        captured["url"] = url
        return _ok_response(nota=5)

    grade_resposta("q", "c", "r", peso=10, http_post=fake_post)
    assert "env-key" in captured["url"]


def test_grade_respostas_returns_one_per_input():
    seen: list[tuple[str, str, str, int]] = []

    def fake_grader(texto: str, criterios: str, resposta: str, peso: int, **_k: Any) -> GeminiResult:
        seen.append((texto, criterios, resposta, peso))
        return GeminiResult(nota=peso, feedback="ok", ok=True)

    results = grade_respostas(
        [
            ("q1", "c1", "r1", 10),
            ("q2", "c2", "r2", 5),
        ],
        api_key="k",
        grader=fake_grader,
    )
    assert len(results) == 2
    assert results[0].nota == 10
    assert results[1].nota == 5
    assert seen == [("q1", "c1", "r1", 10), ("q2", "c2", "r2", 5)]
