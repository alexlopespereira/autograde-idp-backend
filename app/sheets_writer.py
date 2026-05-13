"""Google Sheets writer with idempotency by submission_id and row-count telemetry.

Uses Application Default Credentials (ADC) — no service account JSON key file
needed (R8): in production runs against the Service Account attached to the
Cloud Run service; in dev uses ``gcloud auth application-default login``.

Idempotency: before each append, column B (``submission_id``) is read and
searched for the candidate ``submission_uuid``. If a hit is found, no write
happens and ``AppendResult.written`` is ``False``.

Telemetry: row count of column B is captured before and after the append.
If ``after != before + 1``, ``SHEETS_DROP_DETECTED`` is logged at ERROR
level for alerting (R3 — silent drops would otherwise be invisible).

Concurrency: a module-level ``asyncio.Lock`` serializes appends within the
same event loop. With Cloud Run ``--max-instances=1`` (single process) and
``--concurrency=200`` (multiple in-flight requests on the same loop) this
gives single-writer semantics against the Sheet.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any

import google.auth
from googleapiclient.discovery import Resource, build

log = logging.getLogger(__name__)

SHEET_TAB = "submissoes"
ID_COLUMN_RANGE = f"{SHEET_TAB}!B:B"
READ_RANGE = f"{SHEET_TAB}!A:R"
APPEND_RANGE = SHEET_TAB
SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)

COLUMNS: tuple[str, ...] = (
    "timestamp_utc",
    "submission_id",
    "email",
    "nome",
    "turma",
    "exercicio",
    "nota",
    "nota_max",
    "criterios_json",
    "repo_url",
    "github_user_verificado",
    "late",
    "dias_apos_recomendado",
    "client_version",
    "client_platform",
    "spec_sha",
    "ai_evidence_hashes",
    "respostas_json",
)


@dataclass(frozen=True)
class SubmissionRow:
    timestamp_utc: str
    submission_id: str
    email: str
    nome: str
    turma: str
    exercicio: str
    nota: int
    nota_max: int
    criterios_json: str
    repo_url: str
    github_user_verificado: bool
    late: bool
    dias_apos_recomendado: int
    client_version: str
    client_platform: str
    spec_sha: str
    ai_evidence_hashes: str = ""
    respostas_json: str = ""


@dataclass(frozen=True)
class AppendResult:
    written: bool
    row_count_before: int
    row_count_after: int
    sheet_row_index: int


_append_lock = asyncio.Lock()


def _build_service() -> Resource:
    creds, _ = google.auth.default(scopes=list(SCOPES))
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _row_to_values(row: SubmissionRow) -> list[Any]:
    d = asdict(row)
    return [d[c] for c in COLUMNS]


class SheetsWriter:
    """Append-only writer for the ``submissoes`` tab with idempotency.

    ``service`` is exposed for tests; production code calls the
    1-arg constructor and ADC is used.
    """

    def __init__(self, spreadsheet_id: str, *, service: Resource | None = None):
        self.spreadsheet_id = spreadsheet_id
        self._service = service if service is not None else _build_service()

    async def append_submission(self, row: SubmissionRow) -> AppendResult:
        async with _append_lock:
            return await asyncio.to_thread(self._append_sync, row)

    async def read_submissions(self) -> list[list[str]]:
        return await asyncio.to_thread(self._read_submissions_sync)

    def _read_submissions_sync(self) -> list[list[str]]:
        resp = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=READ_RANGE)
            .execute()
        )
        return resp.get("values", []) or []

    def _read_id_column(self) -> list[str]:
        resp = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=ID_COLUMN_RANGE)
            .execute()
        )
        values = resp.get("values", []) or []
        return [(v[0] if v else "") for v in values]

    def _find_uuid_row(self, ids: list[str], submission_id: str) -> int:
        for i, existing_id in enumerate(ids):
            if i == 0:
                continue
            if existing_id == submission_id:
                return i + 1
        return -1

    def _append_sync(self, row: SubmissionRow) -> AppendResult:
        ids_before = self._read_id_column()
        row_count_before = len(ids_before)

        existing_index = self._find_uuid_row(ids_before, row.submission_id)
        if existing_index > 0:
            log.info(
                "sheets_writer.idempotent_hit submission_id=%s sheet_row_index=%d row_count=%d",
                row.submission_id,
                existing_index,
                row_count_before,
            )
            return AppendResult(
                written=False,
                row_count_before=row_count_before,
                row_count_after=row_count_before,
                sheet_row_index=existing_index,
            )

        values = [_row_to_values(row)]
        # RAW (not USER_ENTERED): user-controlled fields like submission_id,
        # repo_url, and grader-generated criterios_json must not be parsed as
        # Sheets formulas (=HYPERLINK, =IMPORTRANGE, etc.).
        self._service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=APPEND_RANGE,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()

        ids_after = self._read_id_column()
        row_count_after = len(ids_after)
        expected_after = row_count_before + 1
        sheet_row_index = self._find_uuid_row(ids_after, row.submission_id)

        if row_count_after != expected_after:
            log.error(
                "SHEETS_DROP_DETECTED submission_id=%s row_count_before=%d "
                "row_count_after=%d esperado_after=%d",
                row.submission_id,
                row_count_before,
                row_count_after,
                expected_after,
            )
        else:
            log.info(
                "sheets_writer.append_ok submission_id=%s row_count_before=%d "
                "row_count_after=%d esperado_after=%d sheet_row_index=%d",
                row.submission_id,
                row_count_before,
                row_count_after,
                expected_after,
                sheet_row_index,
            )

        return AppendResult(
            written=True,
            row_count_before=row_count_before,
            row_count_after=row_count_after,
            sheet_row_index=sheet_row_index,
        )
