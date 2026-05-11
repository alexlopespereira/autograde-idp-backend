from __future__ import annotations

import threading

import httpx
import pytest
from fastapi import FastAPI
from jose import jwt
from starlette.requests import Request

from app import auth as auth_module
from app.auth import AuthMiddleware, RosterEntry

JWT_SECRET = "test-secret-not-google"
JWT_AUDIENCE = "test-audience.apps.googleusercontent.com"
EMAIL_IN_ROSTER = "aluno@dominio.edu"
EMAIL_NOT_IN_ROSTER = "estranho@dominio.edu"


def _make_token(email: str) -> str:
    return jwt.encode(
        {"email": email, "name": "Aluno Fulano", "sub": "1234567890", "aud": JWT_AUDIENCE},
        JWT_SECRET,
        algorithm="HS256",
    )


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/protected")
    async def protected(request: Request) -> dict[str, str]:
        user = request.state.user
        return {
            "email": user.email,
            "github": user.github_username,
            "correlation_id": request.state.correlation_id,
        }

    return app


@pytest.fixture
def roster_fixture() -> dict[str, RosterEntry]:
    return {
        EMAIL_IN_ROSTER: RosterEntry(
            email=EMAIL_IN_ROSTER,
            nome="Aluno Fulano",
            turma="TD-2026-01",
            github_username="fulano-gh",
        ),
    }


@pytest.fixture
def patch_auth(monkeypatch, roster_fixture):
    def fake_verify(token: str, request_obj, audience: str):
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience=audience)

    monkeypatch.setattr(auth_module.id_token, "verify_oauth2_token", fake_verify)
    monkeypatch.setattr(auth_module, "get_roster", lambda: roster_fixture)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", JWT_AUDIENCE)


async def _request(
    app: FastAPI,
    path: str,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


@pytest.mark.asyncio
async def test_healthz_is_public(patch_auth) -> None:
    response = await _request(_make_app(), "/healthz")
    assert response.status_code == 200
    assert "X-Correlation-Id" in response.headers


@pytest.mark.asyncio
async def test_valid_token_and_in_roster_returns_200(patch_auth) -> None:
    token = _make_token(EMAIL_IN_ROSTER)
    response = await _request(
        _make_app(), "/protected", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == EMAIL_IN_ROSTER
    assert body["github"] == "fulano-gh"
    correlation_header = response.headers["X-Correlation-Id"]
    assert body["correlation_id"] == correlation_header


@pytest.mark.asyncio
async def test_missing_header_returns_401(patch_auth) -> None:
    response = await _request(_make_app(), "/protected")
    assert response.status_code == 401
    assert response.json() == {"error": "missing_authorization"}


@pytest.mark.asyncio
async def test_malformed_header_returns_401(patch_auth) -> None:
    response = await _request(
        _make_app(), "/protected", headers={"Authorization": "Token abc"}
    )
    assert response.status_code == 401
    assert response.json() == {"error": "missing_authorization"}


@pytest.mark.asyncio
async def test_invalid_token_returns_401(monkeypatch, roster_fixture) -> None:
    def raise_value_error(token: str, request_obj, audience: str):
        raise ValueError("Token signature invalid")

    monkeypatch.setattr(auth_module.id_token, "verify_oauth2_token", raise_value_error)
    monkeypatch.setattr(auth_module, "get_roster", lambda: roster_fixture)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", JWT_AUDIENCE)

    response = await _request(
        _make_app(), "/protected", headers={"Authorization": "Bearer garbage"}
    )
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


@pytest.mark.asyncio
async def test_token_valid_but_not_in_roster_returns_403(patch_auth) -> None:
    token = _make_token(EMAIL_NOT_IN_ROSTER)
    response = await _request(
        _make_app(), "/protected", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    assert response.json() == {"error": "not_in_roster"}


@pytest.mark.asyncio
async def test_missing_audience_config_returns_500(monkeypatch, roster_fixture) -> None:
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setattr(auth_module, "get_roster", lambda: roster_fixture)

    token = _make_token(EMAIL_IN_ROSTER)
    response = await _request(
        _make_app(), "/protected", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 500
    assert response.json()["error"] == "missing_audience_config"


@pytest.mark.asyncio
async def test_correlation_id_is_unique_per_request(patch_auth) -> None:
    token = _make_token(EMAIL_IN_ROSTER)
    app = _make_app()
    r1 = await _request(app, "/protected", headers={"Authorization": f"Bearer {token}"})
    r2 = await _request(app, "/protected", headers={"Authorization": f"Bearer {token}"})
    assert r1.headers["X-Correlation-Id"] != r2.headers["X-Correlation-Id"]


@pytest.mark.asyncio
async def test_roster_fetch_failure_returns_502_without_leaking_url(monkeypatch) -> None:
    def fake_verify(token: str, request_obj, audience: str):
        return {"email": EMAIL_IN_ROSTER, "name": "Aluno", "sub": "1", "aud": audience}

    secret_url_in_exc = (
        "HTTPSConnectionPool(host='docs.google.com', port=443): Max retries exceeded "
        "with url: /spreadsheets/d/SECRET-SHEET-ID-12345/export?format=csv"
    )

    def raising_get_roster() -> dict:
        raise RuntimeError(secret_url_in_exc)

    monkeypatch.setattr(auth_module.id_token, "verify_oauth2_token", fake_verify)
    monkeypatch.setattr(auth_module, "get_roster", raising_get_roster)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", JWT_AUDIENCE)

    token = _make_token(EMAIL_IN_ROSTER)
    response = await _request(
        _make_app(), "/protected", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 502
    assert response.json() == {"error": "roster_unavailable"}
    body_text = response.text
    assert "SECRET-SHEET-ID-12345" not in body_text
    assert "spreadsheets/d/" not in body_text


@pytest.mark.asyncio
async def test_blocking_io_runs_off_event_loop(monkeypatch, roster_fixture) -> None:
    main_thread_id = threading.get_ident()
    seen_threads: dict[str, int] = {}

    def fake_verify(token: str, request_obj, audience: str):
        seen_threads["verify"] = threading.get_ident()
        return {"email": EMAIL_IN_ROSTER, "name": "Aluno", "sub": "1", "aud": audience}

    def fake_get_roster() -> dict[str, RosterEntry]:
        seen_threads["get_roster"] = threading.get_ident()
        return roster_fixture

    monkeypatch.setattr(auth_module.id_token, "verify_oauth2_token", fake_verify)
    monkeypatch.setattr(auth_module, "get_roster", fake_get_roster)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", JWT_AUDIENCE)

    token = _make_token(EMAIL_IN_ROSTER)
    response = await _request(
        _make_app(), "/protected", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert seen_threads["verify"] != main_thread_id
    assert seen_threads["get_roster"] != main_thread_id
