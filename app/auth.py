from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.roster import RosterEntry, fetch_roster

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/healthz"})


@dataclass(frozen=True)
class GoogleUser:
    email: str
    name: str
    sub: str


@dataclass(frozen=True)
class AuthenticatedUser:
    google: GoogleUser
    roster: RosterEntry

    @property
    def email(self) -> str:
        return self.roster.email

    @property
    def github_username(self) -> str:
        return self.roster.github_username

    @property
    def turma(self) -> str:
        return self.roster.turma


class AuthError(Exception):
    def __init__(self, status_code: int, error: str, message: str = "") -> None:
        super().__init__(error)
        self.status_code = status_code
        self.error = error
        self.message = message


def verify_google_id_token(token: str) -> GoogleUser:
    audience = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not audience:
        raise AuthError(500, "missing_audience_config", "GOOGLE_OAUTH_CLIENT_ID not set")
    try:
        payload = id_token.verify_oauth2_token(token, google_requests.Request(), audience)
    except ValueError as exc:
        raise AuthError(401, "invalid_token", str(exc)) from exc
    email = payload.get("email")
    if not email:
        raise AuthError(401, "invalid_token", "no email claim in id_token")
    return GoogleUser(
        email=str(email),
        name=str(payload.get("name", "")),
        sub=str(payload.get("sub", "")),
    )


def _http_fetcher(url: str) -> str:
    import requests

    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return response.text


def get_roster() -> dict[str, RosterEntry]:
    url = os.environ.get("ROSTER_URL")
    if not url:
        raise AuthError(500, "missing_roster_config", "ROSTER_URL not set")
    return fetch_roster(url, _http_fetcher)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        correlation_id = uuid.uuid4().hex
        request.state.correlation_id = correlation_id

        if request.url.path in PUBLIC_PATHS:
            response = await call_next(request)
            response.headers["X-Correlation-Id"] = correlation_id
            return response

        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return _json_error(correlation_id, 401, "missing_authorization")
        token = auth_header[7:].strip()
        if not token:
            return _json_error(correlation_id, 401, "missing_authorization")

        try:
            google_user = await asyncio.to_thread(verify_google_id_token, token)
        except AuthError as exc:
            return _json_error(correlation_id, exc.status_code, exc.error, exc.message)

        try:
            roster = await asyncio.to_thread(get_roster)
        except AuthError as exc:
            return _json_error(correlation_id, exc.status_code, exc.error, exc.message)
        except Exception as exc:
            logger.error(
                "roster_fetch_failed",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )
            return _json_error(correlation_id, 502, "roster_unavailable")

        entry = roster.get(google_user.email)
        if entry is None:
            return _json_error(correlation_id, 403, "not_in_roster")

        request.state.user = AuthenticatedUser(google=google_user, roster=entry)
        logger.info(
            "auth_ok",
            extra={
                "correlation_id": correlation_id,
                "email": google_user.email,
                "path": request.url.path,
            },
        )
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = correlation_id
        return response


def _json_error(
    correlation_id: str, status_code: int, error: str, message: str = ""
) -> JSONResponse:
    logger.warning(
        "auth_error",
        extra={
            "correlation_id": correlation_id,
            "status_code": status_code,
            "error": error,
        },
    )
    body: dict[str, str] = {"error": error}
    if message:
        body["message"] = message
    response = JSONResponse(status_code=status_code, content=body)
    response.headers["X-Correlation-Id"] = correlation_id
    return response
