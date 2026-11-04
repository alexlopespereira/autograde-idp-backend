"""Integração dos dois avaliadores com respostas REST reais simuladas."""

import json

import pytest
import requests

from app.gemini import grade_artifact, grade_resposta
from app.openai_grader import OPENAI_API_URL


class Response:
    status_code = 200
    text = "sensitive provider body"

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def completed(value):
    return {"status": "completed", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(value)},
        ]},
    ]}


def test_artifact_request_and_score(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-use")

    def post(url, *, headers, json, timeout):
        assert url == OPENAI_API_URL
        assert headers == {"Authorization": "Bearer test-secret"}
        assert json["model"] == "gpt-6-luna"
        assert json["reasoning"] == {"effort": "low"}
        assert json["store"] is False
        assert "temperature" not in json
        schema = json["text"]["format"]["schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {"score", "evidence_quote", "missing"}
        assert "rubrica teste" in json["input"]
        assert "conteudo teste" in json["input"]
        assert timeout == 30
        return Response(completed({
            "score": 0.5, "evidence_quote": "conteudo teste", "missing": "exemplo",
        }))

    result = grade_artifact("rubrica teste", "texto", "conteudo teste", [], 2, 0,
                            http_post=post)
    assert result.ok
    assert result.score == 0.5
    assert result.evidence_quote == "conteudo teste"


@pytest.mark.parametrize("body", [
    [],
    {"status": "completed", "output": None},
    {"status": "completed", "output": [None]},
    {"status": "completed", "output": [{"type": "message", "content": None}]},
    {"status": "incomplete", "output": []},
    {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "refusal", "refusal": "cannot grade"},
    ]}]},
    {"status": "completed", "output": [{"type": "reasoning"}]},
    completed({"score": float("nan"), "evidence_quote": "", "missing": ""}),
    completed({"score": "bad", "evidence_quote": "", "missing": ""}),
])
def test_bad_provider_output_does_not_award_points(body):
    post = lambda *a, **kw: Response(body)  # noqa: E731
    artifact = grade_artifact("r", "texto", "c", [], 1, 0, api_key="k", http_post=post)
    assert not artifact.ok
    assert artifact.score == 0.0
    short = grade_resposta("q", "c", "r", 10, api_key="k", http_post=post)
    assert not short.ok
    assert short.nota == 0


def test_missing_openai_key_does_not_use_gemini(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "legacy-key")
    result = grade_artifact("r", "texto", "c", [], 1, 0)
    assert not result.ok
    assert "OPENAI_API_KEY" in result.missing


def test_artifact_network_error_does_not_award_points():
    def post(*a, **kw):
        raise requests.Timeout("timeout")

    result = grade_artifact("r", "texto", "c", [], 1, 0, api_key="k", http_post=post)
    assert not result.ok
    assert result.score == 0.0


def test_http_error_does_not_expose_provider_body(caplog):
    response = Response({})
    response.status_code = 401
    post = lambda *a, **kw: response  # noqa: E731
    for result in [
        grade_resposta("q", "c", "r", 10, api_key="k", http_post=post),
        grade_artifact("r", "texto", "c", [], 1, 0, api_key="k", http_post=post),
    ]:
        assert not result.ok
    assert response.text not in caplog.text


def test_network_error_does_not_expose_api_key(caplog):
    secret = "test-private-api-key"

    def post(*a, **kw):
        raise requests.ConnectionError(f"request failed: https://api.example/?key={secret}")

    result = grade_resposta("q", "c", "r", 10, api_key=secret, http_post=post)
    assert not result.ok
    assert result.nota == 0
    assert secret not in result.feedback
    assert secret not in caplog.text
