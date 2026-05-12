"""Tests for /oauth/exchange and /oauth/refresh proxy endpoints.

These endpoints sit in front of Google's /token endpoint, holding the
GOOGLE_OAUTH_CLIENT_SECRET server-side so the CLI never sees it.
"""

from __future__ import annotations

import httpx
import pytest
import responses

from app.main import app

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
CLIENT_ID = "1065810445001-test.apps.googleusercontent.com"


@pytest.fixture
def secret_in_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-server-side-secret")


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@responses.activate
async def test_exchange_passes_secret_to_google_and_returns_tokens(
    secret_in_env, client
):
    captured: dict = {}

    def _callback(request):
        captured["body"] = request.body
        return (
            200,
            {},
            '{"access_token":"at-1","refresh_token":"rt-1","id_token":"id-1",'
            '"expires_in":3600,"token_type":"Bearer"}',
        )

    responses.add_callback(responses.POST, GOOGLE_TOKEN_URL, callback=_callback)

    resp = await client.post(
        "/oauth/exchange",
        json={"client_id": CLIENT_ID, "device_code": "dc-abc"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "at-1"
    assert body["refresh_token"] == "rt-1"
    assert body["id_token"] == "id-1"

    sent = captured["body"]
    assert f"client_id={CLIENT_ID}" in sent
    assert "client_secret=test-server-side-secret" in sent
    assert "device_code=dc-abc" in sent
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code" in sent


@responses.activate
async def test_exchange_passes_through_authorization_pending(secret_in_env, client):
    responses.add(
        responses.POST,
        GOOGLE_TOKEN_URL,
        json={"error": "authorization_pending"},
        status=428,
    )

    resp = await client.post(
        "/oauth/exchange",
        json={"client_id": CLIENT_ID, "device_code": "dc"},
    )

    assert resp.status_code == 428
    assert resp.json() == {"error": "authorization_pending"}


@responses.activate
async def test_exchange_passes_through_slow_down(secret_in_env, client):
    responses.add(
        responses.POST, GOOGLE_TOKEN_URL, json={"error": "slow_down"}, status=428
    )

    resp = await client.post(
        "/oauth/exchange",
        json={"client_id": CLIENT_ID, "device_code": "dc"},
    )

    assert resp.status_code == 428
    assert resp.json() == {"error": "slow_down"}


@responses.activate
async def test_exchange_passes_through_expired_token(secret_in_env, client):
    responses.add(
        responses.POST,
        GOOGLE_TOKEN_URL,
        json={"error": "expired_token"},
        status=400,
    )

    resp = await client.post(
        "/oauth/exchange",
        json={"client_id": CLIENT_ID, "device_code": "dc"},
    )

    assert resp.status_code == 400
    assert resp.json() == {"error": "expired_token"}


async def test_exchange_returns_500_when_secret_not_configured(monkeypatch, client):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)

    resp = await client.post(
        "/oauth/exchange",
        json={"client_id": CLIENT_ID, "device_code": "dc"},
    )

    assert resp.status_code == 500
    assert resp.json() == {"error": "missing_oauth_secret"}


async def test_exchange_returns_422_on_missing_fields(secret_in_env, client):
    resp = await client.post("/oauth/exchange", json={"client_id": CLIENT_ID})
    assert resp.status_code == 422


@responses.activate
async def test_refresh_passes_secret_to_google_and_returns_new_access_token(
    secret_in_env, client
):
    captured: dict = {}

    def _callback(request):
        captured["body"] = request.body
        return (
            200,
            {},
            '{"access_token":"at-new","id_token":"id-new","expires_in":3600,'
            '"token_type":"Bearer"}',
        )

    responses.add_callback(responses.POST, GOOGLE_TOKEN_URL, callback=_callback)

    resp = await client.post(
        "/oauth/refresh",
        json={"client_id": CLIENT_ID, "refresh_token": "rt-1"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "at-new"

    sent = captured["body"]
    assert f"client_id={CLIENT_ID}" in sent
    assert "client_secret=test-server-side-secret" in sent
    assert "refresh_token=rt-1" in sent
    assert "grant_type=refresh_token" in sent


@responses.activate
async def test_refresh_passes_through_invalid_grant(secret_in_env, client):
    responses.add(
        responses.POST,
        GOOGLE_TOKEN_URL,
        json={"error": "invalid_grant"},
        status=400,
    )

    resp = await client.post(
        "/oauth/refresh",
        json={"client_id": CLIENT_ID, "refresh_token": "rt-bad"},
    )

    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid_grant"}


async def test_exchange_does_not_require_authorization_header(secret_in_env, client):
    """Endpoint must be in PUBLIC_PATHS — CLI calls it before it has any tokens."""
    # Don't even mock Google — just verify the middleware doesn't 401 us.
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, GOOGLE_TOKEN_URL, json={"error": "x"}, status=400)
        resp = await client.post(
            "/oauth/exchange",
            json={"client_id": CLIENT_ID, "device_code": "dc"},
        )
    assert resp.status_code != 401


async def test_refresh_does_not_require_authorization_header(secret_in_env, client):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, GOOGLE_TOKEN_URL, json={"error": "x"}, status=400)
        resp = await client.post(
            "/oauth/refresh",
            json={"client_id": CLIENT_ID, "refresh_token": "rt"},
        )
    assert resp.status_code != 401
