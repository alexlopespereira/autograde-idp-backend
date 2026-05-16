from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from jose import jwt

from app import auth as auth_module
from app import endpoints as endpoints_module
from app import roster as roster_module
from app.auth import AuthMiddleware, RosterEntry

JWT_SECRET = "test-secret-not-google"
JWT_AUDIENCE = "test-audience.apps.googleusercontent.com"
EMAIL_NEW = "novato@dominio.edu"
EMAIL_EXISTING = "aluno@dominio.edu"


def _make_token(email: str = EMAIL_NEW, name: str = "Aluno Novato") -> str:
    return jwt.encode(
        {"email": email, "name": name, "sub": "1", "aud": JWT_AUDIENCE},
        JWT_SECRET,
        algorithm="HS256",
    )


class FakeRosterWriter:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str, str, str]] = []

    async def append_member(
        self, email: str, nome: str, turma: str, github_username: str
    ) -> None:
        self.appended.append((email, nome, turma, github_username))


@pytest.fixture
def existing_roster() -> dict[str, RosterEntry]:
    return {
        EMAIL_EXISTING: RosterEntry(
            email=EMAIL_EXISTING,
            nome="Aluno Antigo",
            turma="TD-2026-01",
            github_username="antigo-gh",
        ),
    }


@pytest.fixture(autouse=True)
def _clear_roster_cache():
    roster_module._clear_cache()
    yield
    roster_module._clear_cache()


@pytest.fixture
def patches(monkeypatch, existing_roster):
    def fake_verify(token: str, request_obj, audience: str):
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience=audience)

    monkeypatch.setattr(auth_module.id_token, "verify_oauth2_token", fake_verify)
    monkeypatch.setattr(auth_module, "get_roster", lambda: existing_roster)
    # /me/register chama roster_module.fetch_roster diretamente pra pre-check.
    monkeypatch.setattr(
        roster_module, "fetch_roster", lambda url, fetcher: existing_roster
    )
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", JWT_AUDIENCE)
    monkeypatch.setenv("ROSTER_URL", "https://example.com/roster.csv")
    monkeypatch.setenv("ROSTER_SHEET_ID", "fake-roster-sheet-id")
    return monkeypatch


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(endpoints_module.router)
    return app


async def _post(app: FastAPI, path: str, body: dict, token: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token or _make_token()}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body, headers=headers)


async def _get(app: FastAPI, path: str, token: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token or _make_token()}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers)


# ---------------- GET /turmas ----------------


@pytest.mark.asyncio
async def test_turmas_returns_configured_list(patches) -> None:
    patches.setenv("TURMAS_DISPONIVEIS", "TD-2026-01, TD-2026-02 ,TD-2026-03")
    response = await _get(_make_app(), "/turmas")
    assert response.status_code == 200
    assert response.json() == {"turmas": ["TD-2026-01", "TD-2026-02", "TD-2026-03"]}


@pytest.mark.asyncio
async def test_turmas_empty_when_env_not_set(patches) -> None:
    patches.delenv("TURMAS_DISPONIVEIS", raising=False)
    response = await _get(_make_app(), "/turmas")
    assert response.status_code == 200
    assert response.json() == {"turmas": []}


@pytest.mark.asyncio
async def test_turmas_accessible_without_roster_entry(patches) -> None:
    # Token de email NÃO presente no roster — endpoint Google-only deve responder.
    patches.setenv("TURMAS_DISPONIVEIS", "TD-2026-01")
    response = await _get(_make_app(), "/turmas", token=_make_token("ninguem@x.com"))
    assert response.status_code == 200


# ---------------- POST /me/register ----------------


@pytest.mark.asyncio
async def test_register_happy_path(patches) -> None:
    patches.setenv("TURMAS_DISPONIVEIS", "TD-2026-01,TD-2026-02")
    fake_writer = FakeRosterWriter()
    patches.setattr(endpoints_module, "get_roster_writer", lambda: fake_writer)

    response = await _post(
        _make_app(),
        "/me/register",
        {"github_username": "novato-gh", "turma": "TD-2026-01"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "email": EMAIL_NEW,
        "nome": "Aluno Novato",
        "turma": "TD-2026-01",
        "github_username": "novato-gh",
    }
    assert fake_writer.appended == [(EMAIL_NEW, "Aluno Novato", "TD-2026-01", "novato-gh")]


@pytest.mark.asyncio
async def test_register_accepts_nome_override(patches) -> None:
    patches.setenv("TURMAS_DISPONIVEIS", "TD-2026-01")
    fake_writer = FakeRosterWriter()
    patches.setattr(endpoints_module, "get_roster_writer", lambda: fake_writer)

    response = await _post(
        _make_app(),
        "/me/register",
        {"github_username": "novato-gh", "turma": "TD-2026-01", "nome": "Nome Customizado"},
    )
    assert response.status_code == 200
    assert response.json()["nome"] == "Nome Customizado"
    assert fake_writer.appended[0][1] == "Nome Customizado"


@pytest.mark.asyncio
async def test_register_falls_back_to_email_local_when_name_missing(patches) -> None:
    patches.setenv("TURMAS_DISPONIVEIS", "TD-2026-01")
    fake_writer = FakeRosterWriter()
    patches.setattr(endpoints_module, "get_roster_writer", lambda: fake_writer)

    response = await _post(
        _make_app(),
        "/me/register",
        {"github_username": "novato-gh", "turma": "TD-2026-01"},
        token=_make_token(name=""),  # token sem name claim
    )
    assert response.status_code == 200
    # email = novato@dominio.edu → fallback nome = "novato"
    assert response.json()["nome"] == "novato"


@pytest.mark.asyncio
async def test_register_invalid_turma(patches) -> None:
    patches.setenv("TURMAS_DISPONIVEIS", "TD-2026-01,TD-2026-02")
    fake_writer = FakeRosterWriter()
    patches.setattr(endpoints_module, "get_roster_writer", lambda: fake_writer)

    response = await _post(
        _make_app(),
        "/me/register",
        {"github_username": "novato-gh", "turma": "TD-2026-99"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_turma"
    assert "TD-2026-01" in body["message"]
    assert fake_writer.appended == []


@pytest.mark.asyncio
async def test_register_invalid_github_username(patches) -> None:
    patches.setenv("TURMAS_DISPONIVEIS", "TD-2026-01")
    fake_writer = FakeRosterWriter()
    patches.setattr(endpoints_module, "get_roster_writer", lambda: fake_writer)

    for bad in ["", "-startsdash", "endsdash-", "has space", "tem/barra", "a" * 40]:
        response = await _post(
            _make_app(),
            "/me/register",
            {"github_username": bad, "turma": "TD-2026-01"},
        )
        assert response.status_code == 400, f"esperado 400 pra {bad!r}"
        assert response.json()["error"] == "invalid_github_username"
    assert fake_writer.appended == []


@pytest.mark.asyncio
async def test_register_already_in_roster_returns_409(patches) -> None:
    patches.setenv("TURMAS_DISPONIVEIS", "TD-2026-01")
    fake_writer = FakeRosterWriter()
    patches.setattr(endpoints_module, "get_roster_writer", lambda: fake_writer)

    response = await _post(
        _make_app(),
        "/me/register",
        {"github_username": "novato-gh", "turma": "TD-2026-01"},
        token=_make_token(email=EMAIL_EXISTING),
    )
    assert response.status_code == 409
    assert response.json()["error"] == "already_registered"
    assert fake_writer.appended == []


@pytest.mark.asyncio
async def test_register_disabled_when_no_turmas_configured(patches) -> None:
    patches.delenv("TURMAS_DISPONIVEIS", raising=False)
    fake_writer = FakeRosterWriter()
    patches.setattr(endpoints_module, "get_roster_writer", lambda: fake_writer)

    response = await _post(
        _make_app(),
        "/me/register",
        {"github_username": "novato-gh", "turma": "TD-2026-01"},
    )
    assert response.status_code == 503
    assert response.json()["error"] == "registration_disabled"


@pytest.mark.asyncio
async def test_register_disabled_when_no_roster_sheet_id(patches) -> None:
    patches.setenv("TURMAS_DISPONIVEIS", "TD-2026-01")
    patches.delenv("ROSTER_SHEET_ID", raising=False)

    response = await _post(
        _make_app(),
        "/me/register",
        {"github_username": "novato-gh", "turma": "TD-2026-01"},
    )
    assert response.status_code == 503
    assert response.json()["error"] == "registration_disabled"


@pytest.mark.asyncio
async def test_register_writer_failure_returns_502(patches) -> None:
    patches.setenv("TURMAS_DISPONIVEIS", "TD-2026-01")

    class BrokenWriter:
        async def append_member(self, *_: Any) -> None:
            raise RuntimeError("sheets api down")

    patches.setattr(endpoints_module, "get_roster_writer", lambda: BrokenWriter())

    response = await _post(
        _make_app(),
        "/me/register",
        {"github_username": "novato-gh", "turma": "TD-2026-01"},
    )
    assert response.status_code == 502
    assert response.json()["error"] == "roster_write_unavailable"


@pytest.mark.asyncio
async def test_register_invalidates_roster_cache(patches) -> None:
    """Após registro, cache do roster deve ser limpo pra próxima request resolver."""
    patches.setenv("TURMAS_DISPONIVEIS", "TD-2026-01")
    fake_writer = FakeRosterWriter()
    patches.setattr(endpoints_module, "get_roster_writer", lambda: fake_writer)

    # Popula cache do roster_module com algo
    roster_module._CACHE["foo"] = (9999999999.0, "stale-value")

    response = await _post(
        _make_app(),
        "/me/register",
        {"github_username": "novato-gh", "turma": "TD-2026-01"},
    )
    assert response.status_code == 200
    assert roster_module._CACHE == {}


# ---------------- Middleware: GOOGLE_ONLY_PATHS ----------------


@pytest.mark.asyncio
async def test_register_requires_google_token(patches) -> None:
    """Sem header Authorization → 401 (token Google obrigatório mesmo fora do roster)."""
    transport = httpx.ASGITransport(app=_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/me/register",
            json={"github_username": "x", "turma": "y"},
        )
    assert response.status_code == 401
    assert response.json()["error"] == "missing_authorization"


@pytest.mark.asyncio
async def test_turmas_requires_google_token(patches) -> None:
    transport = httpx.ASGITransport(app=_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/turmas")
    assert response.status_code == 401
