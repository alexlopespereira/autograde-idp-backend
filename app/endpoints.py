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
import re
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse

from app import roster as roster_module
from app import sql_exec
from app.curriculum import (
    CurriculumValidationError,
    DatasetSql,
    Exercise,
    Pergunta,
    parse_exercise_yaml,
)
from app.curso import CURSO_DEFAULT, CursoError, exercises_base_url, split_exercise_id
from app.evidence.shell import InvalidShellEvidence, validate_shell_evidence
from app.gemini import GeminiResult, generate_sql, grade_respostas
from app.github_client import GitHubAPIError, GitHubClient, parse_repo_url
from app.grader import Bulletin, grade
from app.primitives import CriterioResult
from app.roster_writer import RosterWriter
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
    # Vazio é legítimo para exercício com `requer_repositorio: false` no YAML —
    # o CLI nem lê o remote nesse caso. Quem exige repo cobra abaixo, em
    # `_validate_and_grade`, onde a mensagem pode citar o exercício.
    repo_url: str = ""
    ai_evidence: list[Any] | None = None
    shell_evidence: list[Any] | None = None
    artifacts_evidence: list[Any] | None = None
    respostas: list[str] | None = None


class SubmissionRequestBody(GradeRequestBody):
    submission_uuid: str


# Toda mensagem de erro aponta pra uma ancora da FAQ. O aluno que nao leu o
# tutorial precisa de UM link que resolva o caso dele, nao do manual inteiro.
FAQ_URL = "https://github.com/alexlopespereira/autograde-idp/blob/main/docs/FAQ.md"


def _faq(anchor: str) -> str:
    return f"Detalhes: {FAQ_URL}#{anchor}"


RATE_LIMIT_DAILY_CAP = 10  # tanto pra preview-com-respostas quanto pra submissions
RATE_LIMIT_COOLDOWN_SECONDS = 30
# Reset do cap é à meia-noite local (Brasil) — pedagogicamente intuitivo.
RATE_LIMIT_TIMEZONE = "America/Sao_Paulo"


def _bypass_rate_limit_emails() -> frozenset[str]:
    """Allowlist de emails que pulam rate-limit (env var
    RATE_LIMIT_BYPASS_EMAILS, vírgula-separada). Uso: prof testando.

    Lido a cada chamada pra permitir hot-update via Cloud Run env sem rebuild.
    """
    raw = os.environ.get("RATE_LIMIT_BYPASS_EMAILS", "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())

# Column indices na tab `previews` (sheets_writer.PREVIEWS_COLUMNS).
PREVIEW_TIMESTAMP_COL_IDX = 0
PREVIEW_EMAIL_COL_IDX = 1
PREVIEW_EXERCICIO_COL_IDX = 2


def _http_fetcher(url: str) -> str:
    import requests

    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return response.text


def load_exercise(exercicio_id: str) -> tuple[Exercise, str]:
    """Fetch raw YAML and parse Exercise. Returns (exercise, yaml_text).

    A base URL é resolvida pelo curso embutido no id (``ia-1.1`` → curso
    ``ia``), permitindo que cursos diferentes morem em repositórios
    diferentes de exercícios. O arquivo mantém o id COMPLETO no nome.
    """
    curso, _ = split_exercise_id(exercicio_id)
    base = exercises_base_url(curso)
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


def get_roster_writer() -> RosterWriter:
    sheet_id = os.environ.get("ROSTER_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("ROSTER_SHEET_ID not set")
    return RosterWriter(sheet_id)


# Regex GitHub username: começa com alnum, até 39 chars, hífen só entre alnums.
# Lookahead (?=[a-zA-Z0-9]) impede trailing hyphen e hífens consecutivos ('foo--bar').
GITHUB_USERNAME_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$")


class ProfileUpdateRequestBody(BaseModel):
    nome: str
    github_username: str


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
        "judge_degraded": any(c.degraded for c in b.criterios),
    }


def _turma_for_exercise(user: Any, exercise: Exercise) -> str:
    """Turma que sera gravada na Sheet: a que casou com o exercicio.

    Com a coluna `turma` aceitando varias turmas (`TD-2026-01;IA-2026-01`),
    gravar a string crua misturaria os cursos no relatorio do professor.
    """
    match = [t for t in user.turmas if t in exercise.turmas]
    if match:
        return match[0]
    return user.turma


def _json_error(status_code: int, error: str, message: str = "") -> JSONResponse:
    body: dict[str, str] = {"error": error}
    if message:
        body["message"] = message
    return JSONResponse(status_code=status_code, content=body)


PATH_ORDER_CHECK = "github.file.first_commit_before"


def _paths_needing_first_commit(exercise: Exercise) -> list[str]:
    """Paths citados por criterios que checam ordem de entrada no repo.

    Enriquecimento sob demanda: cada path custa uma chamada extra à API do
    GitHub, então só coletamos o que o YAML do exercício realmente pediu.
    """
    paths: set[str] = set()
    for criterio in exercise.criterios:
        if criterio.check != PATH_ORDER_CHECK:
            continue
        for key in ("path_a", "path_b"):
            value = criterio.args.get(key)
            if isinstance(value, str) and value:
                paths.add(value)
    return sorted(paths)


def _collect_first_commits(exercise: Exercise, repo_url: str) -> dict[str, str | None]:
    paths = _paths_needing_first_commit(exercise)
    if not paths:
        return {}
    client = get_github_client()
    out: dict[str, str | None] = {}
    for path in paths:
        try:
            out[path] = client.first_commit_at(repo_url, path)
        except GitHubAPIError as exc:
            log.warning("first_commit_failed path=%s status=%d", path, exc.status_code)
            out[path] = None
    return out


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
        return _json_error(
            404,
            "exercise_not_found",
            f"O YAML do exercicio {body.exercicio!r} existe mas esta invalido: "
            f"{exc}. Isso e um problema do curso, nao seu — avise o professor. "
            + _faq("exercise_not_found"),
        )
    except Exception as exc:  # noqa: BLE001 - network / unknown errors
        log.warning("exercise_load_failed exercicio=%s err=%s", body.exercicio, exc)
        return _json_error(
            404,
            "exercise_not_found",
            f"Nao encontrei o exercicio {body.exercicio!r}. Confira o id: os "
            f"exercicios de Agentes de IA levam prefixo (`ia-1.1`, `ia-1.2`, "
            f"`ia-1.3`, `ia-1.4`) e os de Transformacao Digital nao (`1.1`, "
            f"`2.1`). Digitar `1.3` no lugar de `ia-1.3` cai aqui. "
            + _faq("exercise_not_found"),
        )

    submitted_at = _now_utc()
    disponivel = _ensure_aware_utc(exercise.disponivel_a_partir_de)
    if submitted_at < disponivel:
        return _json_error(
            403,
            "exercise_not_open_yet",
            f"O exercicio {exercise.id} abre em "
            f"{exercise.disponivel_a_partir_de.isoformat()}. Nao ha nada pra "
            f"consertar do seu lado — volte depois dessa data. "
            + _faq("exercise_not_open_yet"),
        )

    user = request.state.user
    if exercise.turmas and not set(user.turmas) & set(exercise.turmas):
        minhas = ", ".join(user.turmas) or "(vazio)"
        dele = ", ".join(exercise.turmas)
        return _json_error(
            403,
            "turma_not_eligible",
            f"Voce esta matriculado em: {minhas}. O exercicio {exercise.id} e "
            f"da(s) turma(s): {dele}. Isso NAO se resolve com `autograde "
            f"login` — quem define sua turma e a planilha do roster, nao o "
            f"seu login Google. Peca ao professor para corrigir a coluna "
            f"`turma` da sua linha (ela aceita mais de uma turma separada por "
            f"`;`, ex.: `{minhas};{exercise.turmas[0]}`). Se voce quis rodar "
            f"outro exercicio, confira o id: `autograde validar <id>`. "
            + _faq("turma_not_eligible"),
        )

    # Exercicio com `requer_repositorio: false` nao e avaliado pelo GitHub: nao
    # ha owner_repo, nao ha checagem de dono e nao ha chamada a API. A amarra de
    # identidade fica com o login Google + roster, e — quando o YAML declara
    # `gh auth status` — com `gh_auth_ok`, que compara o usuario retornado pelo
    # `gh` contra o `github_username` do roster sem precisar de repositorio.
    owner_repo = ""
    if exercise.requer_repositorio:
        if not body.repo_url.strip():
            return _json_error(
                400,
                "repo_url_required",
                f"O exercicio {exercise.id} precisa estar num repositorio do "
                f"GitHub, e a CLI nao encontrou um. Rode `autograde validar` de "
                f"dentro da pasta do repo — `git config --get "
                f"remote.origin.url` tem que devolver uma URL. Se a pasta ainda "
                f"nao e um repo: `git init`, `gh repo create --source=. "
                f"--public --push`. " + _faq("repo_url_required"),
            )
        try:
            owner_repo = parse_repo_url(body.repo_url)
        except ValueError as exc:
            return _json_error(
                400,
                "invalid_repo_url",
                f"O remote `origin` deste diretorio ({body.repo_url!r}) nao e uma "
                f"URL de repositorio do GitHub ({exc}). Rode `git config --get "
                f"remote.origin.url` pra ver o que esta configurado — voce "
                f"provavelmente esta no diretorio errado. " + _faq("invalid_repo_url"),
            )
        owner = owner_repo.split("/", 1)[0]
        if owner.lower() != user.github_username.lower():
            return _json_error(
                403,
                "repo_owner_mismatch",
                f"O repo {owner_repo} pertence ao usuario GitHub `{owner}`, mas o "
                f"github_username cadastrado no seu roster e "
                f"`{user.github_username or '(vazio)'}`. Ou voce esta no diretorio "
                f"de outro repo, ou o roster tem o username errado. Confira com "
                f"`gh auth status` qual conta GitHub voce usa e avise o professor "
                f"se o roster estiver desatualizado. " + _faq("repo_owner_mismatch"),
            )

    try:
        shell_context = validate_shell_evidence(
            body.shell_evidence or [],
            exercise,
            expected_github_user=user.github_username,
            submitted_at=submitted_at,
            owner_repo=owner_repo,
        )
    except InvalidShellEvidence as exc:
        log.warning("shell_evidence_invalid exercicio=%s reason=%s", body.exercicio, exc.reason)
        return _json_error(
            400,
            "invalid_shell_evidence",
            f"A evidencia local coletada pela CLI foi rejeitada: {exc.reason}. "
            f"Normalmente isso significa CLI desatualizada — rode `git pull && "
            f"pip install -e .` no diretorio do autograde-idp. "
            + _faq("invalid_shell_evidence"),
        )

    github_evidence: dict[str, Any] = {}
    try:
        if exercise.requer_repositorio:
            github_evidence = get_github_client().collect_evidence(body.repo_url)
    except GitHubAPIError as exc:
        log.error("github_collect_failed status=%d", exc.status_code)
        return _json_error(
            502,
            "github_unavailable",
            f"Nao consegui ler {body.repo_url} pela API do GitHub "
            f"(HTTP {exc.status_code}). Se o repo for PRIVADO ou tiver sido "
            f"renomeado/apagado, o backend nao enxerga: deixe-o publico em "
            f"Settings > General > Danger Zone > Change visibility. Se o repo "
            f"esta publico e acessivel, foi instabilidade do GitHub — tente de "
            f"novo em um minuto. " + _faq("github_unavailable"),
        )

    evidence: dict[str, Any] = {
        **github_evidence,
        "ai_evidence": body.ai_evidence or [],
        "shell": shell_context.to_evidence_dict(),
        "artifacts": body.artifacts_evidence or [],
        "file_first_commit": _collect_first_commits(exercise, body.repo_url)
        if github_evidence.get("repo_exists")
        else {},
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

    # Se respostas vieram E exercício tem perguntas, valida + grada com Gemini.
    # Sem respostas: retorna bulletin "cru" + lista de perguntas pra CLI prompt.
    if exercise.perguntas and body.respostas is not None:
        respostas_check = _validate_respostas(exercise, body.respostas)
        if isinstance(respostas_check, JSONResponse):
            return respostas_check
        respostas_clean: list[str] = respostas_check

        user = request.state.user
        submitted_at = _now_utc()
        writer = get_sheets_writer()
        preview_rows = await writer.read_previews()
        rl = _check_rate_limit(
            preview_rows,
            user.email,
            body.exercicio,
            submitted_at,
            timestamp_col=PREVIEW_TIMESTAMP_COL_IDX,
            email_col=PREVIEW_EMAIL_COL_IDX,
            exercicio_col=PREVIEW_EXERCICIO_COL_IDX,
            error_prefix="rate_limit_preview",
        )
        if rl is not None:
            return rl

        gemini_results = await asyncio.to_thread(_grade_with_gemini, exercise, respostas_clean)
        bulletin = _append_gemini_to_bulletin(bulletin, exercise, gemini_results)
        # Conta esta tentativa pro rate-limit. Side-effect: se Gemini falhar,
        # aluno ainda perde 1 tentativa (decisão consciente — senão dá pra
        # spam Gemini com payloads garbage pra invalidar a contagem).
        await writer.append_preview_attempt(submitted_at.isoformat(), user.email, body.exercicio)

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
        return _json_error(
            400,
            "respostas_missing",
            "Este exercicio tem pergunta de reflexao e ela precisa ser "
            "respondida. Rode `autograde validar` num terminal interativo "
            "(sem pipe, sem redirect, sem `--auto-submit` em script). "
            + _faq("perguntas"),
        )
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


def _today_local(now: datetime) -> date:
    """Data corrente em America/Sao_Paulo (reset do cap = meia-noite local)."""
    from zoneinfo import ZoneInfo

    return now.astimezone(ZoneInfo(RATE_LIMIT_TIMEZONE)).date()


def _check_rate_limit(
    rows: list[list[str]],
    email: str,
    exercicio: str,
    now: datetime,
    *,
    timestamp_col: int = TIMESTAMP_COL_IDX,
    email_col: int = EMAIL_COL_IDX,
    exercicio_col: int = EXERCICIO_COL_IDX,
    error_prefix: str = "rate_limit",
) -> JSONResponse | None:
    """Cooldown + cap diário com reset à meia-noite local. None = ok.

    Usado tanto pra submissoes (cols 0/2/5) quanto pra previews (cols 0/1/2).
    Coluna `timestamp_col` deve ser ISO8601 com timezone (UTC preferido).
    Emails na allowlist `RATE_LIMIT_BYPASS_EMAILS` pulam direto (testing).
    """
    if email.lower() in _bypass_rate_limit_emails():
        return None
    today = _today_local(now)
    count_today = 0
    for r in rows[1:]:  # pula header
        if len(r) <= max(email_col, exercicio_col, timestamp_col):
            continue
        if r[email_col] != email or r[exercicio_col] != exercicio:
            continue
        try:
            row_ts = datetime.fromisoformat(r[timestamp_col])
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
                f"{error_prefix}_cooldown",
                f"Aguarde {RATE_LIMIT_COOLDOWN_SECONDS}s entre tentativas e "
                f"rode o mesmo comando de novo — nada foi perdido. "
                + _faq("rate_limit"),
            )
        if _today_local(row_ts) == today:
            count_today += 1
    if count_today >= RATE_LIMIT_DAILY_CAP:
        return _json_error(
            429,
            f"{error_prefix}_daily_cap",
            f"Voce ja usou as {RATE_LIMIT_DAILY_CAP} tentativas de hoje neste "
            f"exercicio. O contador zera a meia-noite (horario de Brasilia). "
            f"Suas submissoes anteriores continuam valendo — a MAIOR nota e "
            f"a que conta. " + _faq("rate_limit"),
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
    for _idx, (pergunta, gr) in enumerate(zip(exercise.perguntas, gemini_results)):
        extra.append(
            CriterioResult(
                passed=gr.nota >= pergunta.peso // 2,  # passou se ≥ 50% do peso
                points_earned=gr.nota,
                points_max=pergunta.peso,
                # Feedback do grader (justificativa concreta da nota).
                # CLI renderiza em linha separada indentada se > 50 chars.
                message=gr.feedback,
                # ok=False ⇒ nota de fallback (grader indisponível) → PROVISÓRIA.
                degraded=not gr.ok,
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
    """Grada cada pergunta conforme seu tipo, preservando a ordem do YAML.

    - ``reflexao`` → judge Gemini subjetivo (``grade_respostas``).
    - ``sql``      → prompt do aluno vira SELECT (``generate_sql``) e é executado
      contra o dataset; nota cheia se o resultado bate o gabarito, senão 0.

    Retorna ``GeminiResult`` por pergunta (campo ``ok=False`` = fallback
    degradado de infra, tanto pro judge quanto pro grader SQL).
    """
    results: list[GeminiResult | None] = [None] * len(exercise.perguntas)

    reflexao_items: list[tuple[str, str, str, int]] = []
    reflexao_idx: list[int] = []
    for i, (p, r) in enumerate(zip(exercise.perguntas, respostas)):
        if p.tipo == "sql":
            results[i] = _grade_one_sql(exercise.dataset_sql, p, r)
        else:
            reflexao_idx.append(i)
            reflexao_items.append((p.texto, p.criterios_avaliacao, r, p.peso))

    if reflexao_items:
        for idx, gr in zip(reflexao_idx, grade_respostas(reflexao_items)):
            results[idx] = gr

    return [r for r in results if r is not None]


def _grade_one_sql(dataset: DatasetSql | None, pergunta: Pergunta, resposta: str) -> GeminiResult:
    if dataset is None:  # parse já barra isso; guarda defensiva
        return GeminiResult(
            nota=pergunta.peso, feedback="sql_grader_degraded: sem dataset", ok=False
        )

    gen = generate_sql(resposta, dataset.schema)
    if not gen.ok:
        # Falha de infra (Gemini fora) → nota cheia + degraded (bug nosso não pune aluno).
        return GeminiResult(
            nota=pergunta.peso,
            feedback=f"sql_grader_degraded (nota provisória): {gen.error}",
            ok=False,
        )

    ev = sql_exec.evaluate(
        dataset.schema,
        dataset.seed,
        pergunta.query_referencia,
        gen.sql,
        ordered=pergunta.ordenado,
    )
    nota = pergunta.peso if ev.matched else 0
    mark = "✓" if ev.matched else "✗"
    feedback = f"{mark} {ev.reason}\nSQL gerado a partir do seu prompt: {gen.sql}"
    return GeminiResult(nota=nota, feedback=feedback, ok=True)


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
        turma=_turma_for_exercise(user, exercise),
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
        judge_degraded=any(c.degraded for c in bulletin.criterios),
        curso=split_exercise_id(body.exercicio)[0],
    )

    result: AppendResult = await writer.append_submission(row)

    if result.written and result.row_count_after != result.row_count_before + 1:
        return _json_error(
            503,
            "sheets_drop_detected",
            "A planilha de submissoes nao confirmou a gravacao. Rode "
            "`autograde validar` de novo — a CLI reusa o mesmo id de "
            "submissao, entao nao vai duplicar sua nota. " + _faq("erro_5xx"),
        )

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
            # `curso` sai do próprio id, não da coluna T: linhas históricas
            # (anteriores à coluna) continuam agrupando corretamente. Id
            # malformado numa linha antiga não pode derrubar o boletim.
            try:
                curso = split_exercise_id(exercicio)[0]
            except CursoError:
                curso = CURSO_DEFAULT
            by_exercicio[exercicio] = {
                "exercicio": exercicio,
                "curso": curso,
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
        "turmas": list(user.turmas),
        "github_username": user.roster.github_username,
    }


@router.post("/me/profile")
async def me_profile(body: ProfileUpdateRequestBody, request: Request) -> Any:
    if not GITHUB_USERNAME_RE.match(body.github_username):
        return _json_error(
            400,
            "invalid_github_username",
            "Username do GitHub invalido. Use so o seu login (sem `@`, sem "
            "URL): letras, numeros e hifen, ate 39 caracteres.",
        )
    try:
        writer = get_roster_writer()
    except RuntimeError:
        return _json_error(
            500,
            "missing_roster_sheet_config",
            "O backend esta sem ROSTER_SHEET_ID configurado. E um problema do "
            "servidor — avise o professor.",
        )
    user = request.state.user
    result = await asyncio.to_thread(
        writer.update_profile, user.email, body.nome, body.github_username
    )
    # Cache invalidation: senão próximo /me/identity ainda vê o roster antigo
    # por até ROSTER_TTL_SECONDS (5min).
    roster_module._clear_cache()
    return {"updated": list(result.updated), "skipped": list(result.skipped)}
