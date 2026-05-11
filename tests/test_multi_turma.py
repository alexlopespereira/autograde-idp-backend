"""Multi-turma backend filter (US-12): if exercise.turmas is non-empty and
user.turma is not in exercise.turmas, /grade-preview and /submissions must
return 403 with error=turma_not_eligible. Roster fixture covers 2 turmas
(TD-2026-01 and MBA-IDP-2026).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from jose import jwt

from app import auth as auth_module
from app import endpoints as endpoints_module
from app.auth import AuthMiddleware, RosterEntry
from app.curriculum import Criterio, Exercise
from app.primitives import CriterioResult, registry
from app.sheets_writer import AppendResult

JWT_SECRET = "test-secret-not-google"
JWT_AUDIENCE = "test-audience.apps.googleusercontent.com"
NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)

EMAIL_TD = "td-aluno@idp.edu.br"
EMAIL_MBA = "mba-aluno@idp.edu.br"
GH_TD = "td-fulano"
GH_MBA = "mba-cicrano"


PRIMITIVE_PASS = "test.multi_turma.always_pass"


def _make_token(email: str) -> str:
    return jwt.encode(
        {"email": email, "name": "Aluno", "sub": "x", "aud": JWT_AUDIENCE},
        JWT_SECRET,
        algorithm="HS256",
    )


def _make_exercise(turmas: tuple[str, ...]) -> Exercise:
    return Exercise(
        id="1.2",
        titulo="GitHub CLI",
        turmas=turmas,
        disponivel_a_partir_de=NOW - timedelta(days=7),
        prazo={"recomendado_ate": NOW + timedelta(days=7)},
        criterios=(Criterio(id="c1", peso=100, check=PRIMITIVE_PASS, args={}),),
    )


@pytest.fixture(autouse=True)
def _register_pass_primitive():
    def always_pass(args: dict, evidence: dict) -> CriterioResult:
        peso = args.get("_peso", 0)
        return CriterioResult(True, peso, peso, "ok")

    registry[PRIMITIVE_PASS] = always_pass
    yield
    registry.pop(PRIMITIVE_PASS, None)


@pytest.fixture
def roster_two_turmas() -> dict[str, RosterEntry]:
    return {
        EMAIL_TD: RosterEntry(
            email=EMAIL_TD, nome="TD Aluno",
            turma="TD-2026-01", github_username=GH_TD,
        ),
        EMAIL_MBA: RosterEntry(
            email=EMAIL_MBA, nome="MBA Aluno",
            turma="MBA-IDP-2026", github_username=GH_MBA,
        ),
    }


@dataclass
class FakeGitHub:
    evidence: dict[str, Any]

    def collect_evidence(self, repo_url: str) -> dict[str, Any]:
        return self.evidence


@dataclass
class FakeSheets:
    append_result: AppendResult | None = None

    async def append_submission(self, row: Any) -> AppendResult:
        assert self.append_result is not None
        return self.append_result

    async def read_submissions(self) -> list[list[str]]:
        return []


@pytest.fixture
def patches(monkeypatch, roster_two_turmas):
    def fake_verify(token: str, request_obj, audience: str):
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience=audience)

    monkeypatch.setattr(auth_module.id_token, "verify_oauth2_token", fake_verify)
    monkeypatch.setattr(auth_module, "get_roster", lambda: roster_two_turmas)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", JWT_AUDIENCE)
    monkeypatch.setattr(endpoints_module, "_now_utc", lambda: NOW)
    return monkeypatch


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(endpoints_module.router)
    return app


def _patch_endpoints(monkeypatch, exercise: Exercise) -> FakeSheets:
    sheets = FakeSheets(
        append_result=AppendResult(
            written=True, row_count_before=0, row_count_after=1, sheet_row_index=1
        )
    )
    monkeypatch.setattr(
        endpoints_module, "load_exercise",
        lambda eid: (exercise, "exercicio: 1.2\n"),
    )
    monkeypatch.setattr(
        endpoints_module, "get_github_client",
        lambda: FakeGitHub(evidence={"repo_exists": True, "files_list": []}),
    )
    monkeypatch.setattr(endpoints_module, "get_sheets_writer", lambda: sheets)
    return sheets


async def _post(app: FastAPI, path: str, body: dict, email: str) -> httpx.Response:
    headers = {"Authorization": f"Bearer {_make_token(email)}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body, headers=headers)


# ---------- /grade-preview ---------------------------------------------------


@pytest.mark.asyncio
async def test_grade_preview_blocks_mba_student_from_td_only_exercise(patches):
    _patch_endpoints(patches, _make_exercise(turmas=("TD-2026-01",)))
    response = await _post(
        _make_app(), "/grade-preview",
        {"exercicio": "1.2", "repo_url": f"https://github.com/{GH_MBA}/projeto"},
        email=EMAIL_MBA,
    )
    assert response.status_code == 403
    assert response.json() == {"error": "turma_not_eligible"}


@pytest.mark.asyncio
async def test_grade_preview_blocks_td_student_from_mba_only_exercise(patches):
    _patch_endpoints(patches, _make_exercise(turmas=("MBA-IDP-2026",)))
    response = await _post(
        _make_app(), "/grade-preview",
        {"exercicio": "1.2", "repo_url": f"https://github.com/{GH_TD}/projeto"},
        email=EMAIL_TD,
    )
    assert response.status_code == 403
    assert response.json() == {"error": "turma_not_eligible"}


@pytest.mark.asyncio
async def test_grade_preview_allows_td_student_when_turma_matches(patches):
    _patch_endpoints(patches, _make_exercise(turmas=("TD-2026-01",)))
    response = await _post(
        _make_app(), "/grade-preview",
        {"exercicio": "1.2", "repo_url": f"https://github.com/{GH_TD}/projeto"},
        email=EMAIL_TD,
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_grade_preview_allows_both_turmas_when_multi(patches):
    _patch_endpoints(patches, _make_exercise(turmas=("TD-2026-01", "MBA-IDP-2026")))
    r_td = await _post(
        _make_app(), "/grade-preview",
        {"exercicio": "1.2", "repo_url": f"https://github.com/{GH_TD}/projeto"},
        email=EMAIL_TD,
    )
    r_mba = await _post(
        _make_app(), "/grade-preview",
        {"exercicio": "1.2", "repo_url": f"https://github.com/{GH_MBA}/projeto"},
        email=EMAIL_MBA,
    )
    assert r_td.status_code == 200
    assert r_mba.status_code == 200


# ---------- /submissions ----------------------------------------------------


@pytest.mark.asyncio
async def test_submissions_blocks_wrong_turma(patches):
    _patch_endpoints(patches, _make_exercise(turmas=("TD-2026-01",)))
    response = await _post(
        _make_app(), "/submissions",
        {
            "exercicio": "1.2",
            "repo_url": f"https://github.com/{GH_MBA}/projeto",
            "submission_uuid": "uuid-1",
        },
        email=EMAIL_MBA,
    )
    assert response.status_code == 403
    assert response.json() == {"error": "turma_not_eligible"}
