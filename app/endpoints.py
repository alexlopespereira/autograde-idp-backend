"""HTTP endpoints: /grade-preview, /submissions, /me/grades, /me/identity.

Orchestra: curriculum.fetch_exercise → github_client.collect_evidence → grader.grade
→ sheets_writer.append_submission (only /submissions).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse

from app.curriculum import CurriculumValidationError, Exercise, parse_exercise_yaml
from app.evidence.shell import InvalidShellEvidence, validate_shell_evidence
from app.gemini import GeminiResult, grade_respostas
from app.github_client import GitHubAPIError, GitHubClient, parse_repo_url
from app.grader import Bulletin, grade
from app.primitives import CriterioResult
from app.sheets_writer import AppendResult, SheetsWriter, SubmissionRow

log = logging.getLogger(__name__)

router = APIRouter()

# Column indices in submissoes tab (see sheets_writer.COLUMNS).
TIMESTAMP_COL_IDX = 0
EMAIL_COL_IDX = 2
EXERCICIO_COL_IDX = 5
NOTA_COL_IDX = 6


class GradeRequestBody(BaseModel):
    exercicio: str
    repo_url: str
    ai_evidence: list[Any] | None = None
    shell_evidence: list[Any] | None = None


class SubmissionRequestBody(GradeRequestBody):
    submission_uuid: str
    respostas: list[str] | None = None


RATE_LIMIT_DAILY_CAP = 10
RATE_LIMIT_COOLDOWN_SECONDS = 30


def _http_fetcher(url: str) -> str:
    import requests

    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return response.text


def _exercises_base_url() -> str:
    base = os.environ.get("EXERCISES_BASE_URL")
    if not base:
        raise RuntimeError("EXERCISES_BASE_URL not set")
    return base.rstrip("/")


def load_exercise(exercicio_id: str) -> tuple[Exercise, str]:
    """Fetch raw YAML and parse Exercise. Returns (exercise, yaml_text)."""
    base = _exercises_base_url()
    url = f"{base}/{exercicio_id}.yaml"
    yaml_text = _http_fetcher(url)
    exercise = parse_exercise_yaml(yaml_text)
    if exercise.id != exercicio_id:
        raise CurriculumValidationError(
            f"YAML.exercicio ({exercise.id!r}) != solicitado ({exercicio_id!r})"
        )
    return exercise, yaml_text


_github_client_singleton: GitHubClient | None = None


def get_github_client() -> GitHubClient:
    global _github_client_singleton
    if _github_client_singleton is None:
        _github_client_singleton = GitHubClient()
    return _github_client_singleton


def get_sheets_writer() -> SheetsWriter:
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        raise RuntimeError("SHEET_ID not set")
    return SheetsWriter(sheet_id)


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _ensure_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _coerce_datetime(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, date):
        return datetime.combine(raw, datetime.min.time())
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


def _compute_late(exercise: Exercise, submitted_at: datetime) -> tuple[bool, int]:
    recomendado_raw = exercise.prazo.get("recomendado_ate")
    recomendado = _coerce_datetime(recomendado_raw)
    if recomendado is None:
        return False, 0
    recomendado = _ensure_aware_utc(recomendado)
    days = max(0, (submitted_at - recomendado).days)
    return (days > 0), days


def _bulletin_to_dict(b: Bulletin) -> dict[str, Any]:
    return {
        "criterios": [asdict(c) for c in b.criterios],
        "total": b.total,
        "max_total": b.max_total,
    }


def _json_error(status_code: int, error: str, message: str = "") -> JSONResponse:
    body: dict[str, str] = {"error": error}
    if message:
        body["message"] = message
    return JSONResponse(status_code=status_code, content=body)


def _validate_and_grade(
    request: Request, body: GradeRequestBody
) -> JSONResponse | tuple[Exercise, str, Bulletin, bool, int]:
    """Shared validation pipeline. Returns JSONResponse on error or
    (exercise, yaml_text, bulletin, late, days_apos_recomendado) on success.
    """
    try:
        exercise, yaml_text = load_exercise(body.exercicio)
    except CurriculumValidationError as exc:
        log.warning("exercise_validation_failed exercicio=%s err=%s", body.exercicio, exc)
        return _json_error(404, "exercise_not_found", str(exc))
    except Exception as exc:  # noqa: BLE001 - network / unknown errors
        log.warning("exercise_load_failed exercicio=%s err=%s", body.exercicio, exc)
        return _json_error(404, "exercise_not_found")

    submitted_at = _now_utc()
    disponivel = _ensure_aware_utc(exercise.disponivel_a_partir_de)
    if submitted_at < disponivel:
        return _json_error(403, "exercise_not_open_yet")

    user = request.state.user
    if exercise.turmas and user.turma not in exercise.turmas:
        return _json_error(403, "turma_not_eligible")

    try:
        owner_repo = parse_repo_url(body.repo_url)
    except ValueError as exc:
        return _json_error(400, "invalid_repo_url", str(exc))
    owner = owner_repo.split("/", 1)[0]
    if owner.lower() != user.github_username.lower():
        return _json_error(403, "repo_owner_mismatch")

    try:
        shell_context = validate_shell_evidence(
            body.shell_evidence or [],
            exercise,
            expected_github_user=user.github_username,
            submitted_at=submitted_at,
        )
    except InvalidShellEvidence as exc:
        log.warning("shell_evidence_invalid exercicio=%s reason=%s", body.exercicio, exc.reason)
        return _json_error(400, "invalid_shell_evidence", exc.reason)

    try:
        github_evidence = get_github_client().collect_evidence(body.repo_url)
    except GitHubAPIError as exc:
        log.error("github_collect_failed status=%d", exc.status_code)
        return _json_error(502, "github_unavailable")

    evidence: dict[str, Any] = {
        **github_evidence,
        "ai_evidence": body.ai_evidence or [],
        "shell": shell_context.to_evidence_dict(),
    }
    bulletin = grade(exercise, evidence)
    late, days = _compute_late(exercise, submitted_at)
    return exercise, yaml_text, bulletin, late, days


@router.post("/grade-preview")
async def grade_preview(body: GradeRequestBody, request: Request) -> Any:
    validated = await asyncio.to_thread(_validate_and_grade, request, body)
    if isinstance(validated, JSONResponse):
        return validated
    exercise, _yaml_text, bulletin, late, days = validated
    return {
        "bulletin": _bulletin_to_dict(bulletin),
        "late": late,
        "dias_apos_recomendado": days,
        "perguntas": [{"texto": p.texto, "peso": p.peso} for p in exercise.perguntas],
    }


def _validate_respostas(
    exercise: Exercise, respostas: list[str] | None
) -> JSONResponse | list[str]:
    perguntas = exercise.perguntas
    if not perguntas:
        return []
    if respostas is None:
        return _json_error(400, "respostas_missing", "exercício exige respostas")
    if len(respostas) != len(perguntas):
        return _json_error(
            400,
            "respostas_count_mismatch",
            f"esperado {len(perguntas)} respostas, recebido {len(respostas)}",
        )
    cleaned: list[str] = []
    for idx, r in enumerate(respostas):
        text = (r or "").strip()
        if not text:
            return _json_error(400, "resposta_empty", f"resposta {idx + 1} está vazia")
        cleaned.append(text)
    return cleaned


def _check_rate_limit(
    rows: list[list[str]],
    email: str,
    exercicio: str,
    now: datetime,
) -> JSONResponse | None:
    """Lê rows da sheet e aplica cap diário + cooldown. None = ok."""
    count_24h = 0
    for r in rows[1:]:  # pula header
        if len(r) <= max(EMAIL_COL_IDX, EXERCICIO_COL_IDX):
            continue
        if r[EMAIL_COL_IDX] != email or r[EXERCICIO_COL_IDX] != exercicio:
            continue
        try:
            row_ts = datetime.fromisoformat(r[TIMESTAMP_COL_IDX])
        except (ValueError, IndexError):
            continue
        if row_ts.tzinfo is None:
            row_ts = row_ts.replace(tzinfo=timezone.utc)
        delta_s = (now - row_ts).total_seconds()
        if delta_s < 0:
            continue
        if delta_s < RATE_LIMIT_COOLDOWN_SECONDS:
            return _json_error(
                429,
                "rate_limit_cooldown",
                f"aguarde {RATE_LIMIT_COOLDOWN_SECONDS}s entre tentativas",
            )
        if delta_s < 86400:
            count_24h += 1
    if count_24h >= RATE_LIMIT_DAILY_CAP:
        return _json_error(
            429,
            "rate_limit_daily_cap",
            f"limite de {RATE_LIMIT_DAILY_CAP} tentativas/dia atingido",
        )
    return None


def _append_gemini_to_bulletin(
    bulletin: Bulletin,
    exercise: Exercise,
    gemini_results: list[GeminiResult],
) -> Bulletin:
    extra: list[CriterioResult] = []
    extra_total = 0
    extra_max = 0
    for idx, (pergunta, gr) in enumerate(zip(exercise.perguntas, gemini_results)):
        extra.append(
            CriterioResult(
                passed=gr.nota >= pergunta.peso // 2,  # passou se ≥ 50% do peso
                points_earned=gr.nota,
                points_max=pergunta.peso,
                message=f"reflexao_{idx + 1}: {gr.feedback}",
            )
        )
        extra_total += gr.nota
        extra_max += pergunta.peso
    return Bulletin(
        criterios=bulletin.criterios + tuple(extra),
        total=bulletin.total + extra_total,
        max_total=bulletin.max_total + extra_max,
    )


def _grade_with_gemini(exercise: Exercise, respostas: list[str]) -> list[GeminiResult]:
    return grade_respostas(
        [(p.texto, p.criterios_avaliacao, r, p.peso) for p, r in zip(exercise.perguntas, respostas)]
    )


@router.post("/submissions")
async def submissions(body: SubmissionRequestBody, request: Request) -> Any:
    validated = await asyncio.to_thread(_validate_and_grade, request, body)
    if isinstance(validated, JSONResponse):
        return validated
    exercise, yaml_text, bulletin, late, days = validated

    respostas_check = _validate_respostas(exercise, body.respostas)
    if isinstance(respostas_check, JSONResponse):
        return respostas_check
    respostas_clean: list[str] = respostas_check

    user = request.state.user
    submitted_at = _now_utc()
    writer = get_sheets_writer()

    if exercise.perguntas:
        rows = await writer.read_submissions()
        rate_limit_err = _check_rate_limit(rows, user.email, body.exercicio, submitted_at)
        if rate_limit_err is not None:
            return rate_limit_err
        gemini_results = await asyncio.to_thread(_grade_with_gemini, exercise, respostas_clean)
        bulletin = _append_gemini_to_bulletin(bulletin, exercise, gemini_results)
    else:
        gemini_results = []

    spec_sha = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()
    criterios_payload = [asdict(c) for c in bulletin.criterios]

    respostas_payload = [
        {
            "texto": p.texto,
            "resposta": r,
            "nota": gr.nota,
            "feedback": gr.feedback,
            "gemini_ok": gr.ok,
        }
        for p, r, gr in zip(exercise.perguntas, respostas_clean, gemini_results)
    ]
    row = SubmissionRow(
        timestamp_utc=submitted_at.isoformat(),
        submission_id=body.submission_uuid,
        email=user.email,
        nome=user.roster.nome,
        turma=user.turma,
        exercicio=body.exercicio,
        nota=bulletin.total,
        nota_max=bulletin.max_total,
        criterios_json=json.dumps(criterios_payload, ensure_ascii=False),
        repo_url=body.repo_url,
        github_user_verificado=True,
        late=late,
        dias_apos_recomendado=days,
        client_version=request.headers.get("x-client-version", ""),
        client_platform=request.headers.get("x-client-platform", ""),
        spec_sha=spec_sha,
        respostas_json=(
            json.dumps(respostas_payload, ensure_ascii=False) if respostas_payload else ""
        ),
    )

    result: AppendResult = await writer.append_submission(row)

    if result.written and result.row_count_after != result.row_count_before + 1:
        return _json_error(503, "sheets_drop_detected")

    return {
        "bulletin": _bulletin_to_dict(bulletin),
        "submission_id": body.submission_uuid,
        "written": result.written,
        "late": late,
        "dias_apos_recomendado": days,
    }


@router.get("/me/grades")
async def me_grades(request: Request) -> Any:
    user = request.state.user
    writer = get_sheets_writer()
    rows = await writer.read_submissions()

    max_idx = max(EMAIL_COL_IDX, EXERCICIO_COL_IDX, NOTA_COL_IDX, TIMESTAMP_COL_IDX)
    by_exercicio: dict[str, dict[str, Any]] = {}
    for row in rows:
        if len(row) <= max_idx:
            continue
        if row[EMAIL_COL_IDX] != user.email:
            continue
        exercicio = row[EXERCICIO_COL_IDX]
        try:
            nota = int(row[NOTA_COL_IDX])
        except (TypeError, ValueError):
            nota = 0
        ts = row[TIMESTAMP_COL_IDX]
        entry = by_exercicio.get(exercicio)
        if entry is None:
            by_exercicio[exercicio] = {
                "exercicio": exercicio,
                "melhor_nota": nota,
                "num_tentativas": 1,
                "ultima_submissao_at": ts,
            }
        else:
            entry["num_tentativas"] += 1
            if nota > entry["melhor_nota"]:
                entry["melhor_nota"] = nota
            if ts > entry["ultima_submissao_at"]:
                entry["ultima_submissao_at"] = ts

    return {"grades": list(by_exercicio.values())}


@router.get("/me/identity")
async def me_identity(request: Request) -> Any:
    user = request.state.user
    return {
        "email": user.email,
        "nome": user.roster.nome,
        "turma": user.turma,
    }
