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

from app import reqctx
from app.roster import RosterEntry, fetch_roster, normalize_email

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/healthz", "/oauth/exchange", "/oauth/refresh"})


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
        """Turma "principal" — a primeira da coluna. Ver :attr:`turmas`."""
        turmas = self.roster.turmas
        return turmas[0] if turmas else self.roster.turma

    @property
    def turmas(self) -> tuple[str, ...]:
        """Todas as turmas do aluno (a coluna aceita ``TD-2026-01;IA-2026-01``)."""
        return self.roster.turmas


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
        raise AuthError(
            401,
            "invalid_token",
            f"Seu token Google nao foi aceito ({exc}). Rode `autograde login` "
            "para renovar a sessao.",
        ) from exc
    email = payload.get("email")
    if not email:
        raise AuthError(401, "invalid_token", "no email claim in id_token")
    return GoogleUser(
        # O Google já manda minúsculo; normalizamos assim mesmo para que
        # exista UMA forma canônica de email atravessando o request.
        email=normalize_email(str(email)),
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
        # Antes de qualquer `return`: mesmo a falha mais precoce sai do log
        # com correlation_id e rota. O email entra depois, quando houver.
        reqctx.current_correlation_id.set(correlation_id)
        reqctx.current_path.set(request.url.path)

        if request.url.path in PUBLIC_PATHS:
            response = await call_next(request)
            response.headers["X-Correlation-Id"] = correlation_id
            return response

        auth_header = request.headers.get("authorization", "")
        _no_auth = (
            "Requisicao sem credencial. Rode `autograde login` e tente de novo."
        )
        if not auth_header.lower().startswith("bearer "):
            return _json_error(
                correlation_id, 401, "missing_authorization", _no_auth,
                path=request.url.path,
            )
        token = auth_header[7:].strip()
        if not token:
            return _json_error(
                correlation_id, 401, "missing_authorization", _no_auth,
                path=request.url.path,
            )

        try:
            google_user = await asyncio.to_thread(verify_google_id_token, token)
        except AuthError as exc:
            return _json_error(
                correlation_id, exc.status_code, exc.error, exc.message,
                path=request.url.path,
            )

        try:
            roster = await asyncio.to_thread(get_roster)
        except AuthError as exc:
            return _json_error(
                correlation_id, exc.status_code, exc.error, exc.message,
                email=google_user.email, path=request.url.path,
            )
        except Exception as exc:
            logger.error(
                "roster_fetch_failed",
                extra={
                    "correlation_id": correlation_id,
                    "email": google_user.email,
                    "error": str(exc),
                },
            )
            return _json_error(
                correlation_id,
                502,
                "roster_unavailable",
                "O backend nao conseguiu ler a planilha da turma. E um "
                "problema do servidor, nao seu — tente de novo em alguns "
                "minutos e avise o professor se persistir.",
                email=google_user.email,
                path=request.url.path,
            )

        entry = roster.get(normalize_email(google_user.email))
        if entry is None:
            return _json_error(
                correlation_id,
                403,
                "not_in_roster",
                f"O email {google_user.email} nao esta na planilha da turma. "
                "Causa mais comum: voce fez `autograde login` com uma conta "
                "Google diferente da que o professor cadastrou (ex.: gmail "
                "pessoal no lugar do email institucional). Rode `autograde "
                "login` de novo e escolha a conta certa; se o email estiver "
                "certo, peca ao professor para incluir voce no roster.",
                email=google_user.email,
                path=request.url.path,
            )

        request.state.user = AuthenticatedUser(google=google_user, roster=entry)
        # `entry.email` (do roster, já normalizado) e não a claim crua: é a
        # forma canônica que o resto do sistema usa como identidade.
        reqctx.current_email.set(entry.email)
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
    correlation_id: str,
    status_code: int,
    error: str,
    message: str = "",
    *,
    email: str = "",
    path: str = "",
) -> JSONResponse:
    """Responde o erro E deixa rastro de QUEM falhou.

    Até 2026-09 este log tinha `correlation_id`, `status_code` e `error` —
    e mais nada. `auth_ok` tinha o email; `auth_error`, não. Ou seja: o
    sucesso era atribuível e a falha era anônima, exatamente ao contrário
    do que a investigação precisa. Nove `not_in_roster` ficaram no Cloud
    Logging sem que desse para dizer de quem eram, e o incidente da turma
    IA-2026-01 (21 alunos com email em CAIXA ALTA na planilha) só andou
    quando um aluno escreveu — duas vezes.

    `email` só entra quando o token já foi verificado; em
    `missing_authorization` e `invalid_token` não existe email confiável, e
    logar o que veio num header não-validado seria logar entrada de
    terceiro. Logamos o email (dado de aluno, já presente em `auth_ok`),
    nunca o token nem o corpo dele.
    """
    extra = {
        "correlation_id": correlation_id,
        "status_code": status_code,
        "error": error,
    }
    if email:
        extra["email"] = email
    if path:
        extra["path"] = path
    logger.warning("auth_error", extra=extra)
    body: dict[str, str] = {"error": error}
    if message:
        body["message"] = message
    response = JSONResponse(status_code=status_code, content=body)
    response.headers["X-Correlation-Id"] = correlation_id
    return response
