"""Proxy endpoints for Google OAuth 2.0 Device Flow token exchange.

The CLI calls /device/code directly against Google (only client_id needed).
For /token (both exchange and refresh) the CLI hits this backend instead — the
backend holds GOOGLE_OAUTH_CLIENT_SECRET server-side so the secret never lives
on student machines.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import requests
from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint
GOOGLE_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
GOOGLE_REQUEST_TIMEOUT = 15

router = APIRouter(prefix="/oauth")


class ExchangeBody(BaseModel):
    client_id: str
    device_code: str


class RefreshBody(BaseModel):
    client_id: str
    refresh_token: str


def _client_secret() -> str | None:
    return os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")


def _passthrough(google_response: requests.Response) -> JSONResponse:
    try:
        body: Any = google_response.json()
    except ValueError:
        body = {"error": "google_token_invalid_json"}
    return JSONResponse(status_code=google_response.status_code, content=body)


def _post_to_google(data: dict[str, str]) -> requests.Response:
    return requests.post(GOOGLE_TOKEN_URL, data=data, timeout=GOOGLE_REQUEST_TIMEOUT)


@router.post("/exchange")
async def exchange(body: ExchangeBody) -> JSONResponse:
    secret = _client_secret()
    if not secret:
        log.error("oauth_exchange_missing_secret")
        return JSONResponse(status_code=500, content={"error": "missing_oauth_secret"})
    payload = {
        "client_id": body.client_id,
        "client_secret": secret,
        "device_code": body.device_code,
        "grant_type": GOOGLE_DEVICE_GRANT,
    }
    google_resp = await asyncio.to_thread(_post_to_google, payload)
    return _passthrough(google_resp)


@router.post("/refresh")
async def refresh(body: RefreshBody) -> JSONResponse:
    secret = _client_secret()
    if not secret:
        log.error("oauth_refresh_missing_secret")
        return JSONResponse(status_code=500, content={"error": "missing_oauth_secret"})
    payload = {
        "client_id": body.client_id,
        "client_secret": secret,
        "refresh_token": body.refresh_token,
        "grant_type": "refresh_token",
    }
    google_resp = await asyncio.to_thread(_post_to_google, payload)
    return _passthrough(google_resp)
