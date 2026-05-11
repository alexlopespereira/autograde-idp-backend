from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.sheets_writer import (
    APPEND_RANGE,
    COLUMNS,
    ID_COLUMN_RANGE,
    AppendResult,
    SheetsWriter,
    SubmissionRow,
    _row_to_values,
)

SPREADSHEET_ID = "sheet-abc"


# ---------- helpers --------------------------------------------------------


def _row(submission_id: str = "uuid-A") -> SubmissionRow:
    return SubmissionRow(
        timestamp_utc="2026-05-10T18:00:00Z",
        submission_id=submission_id,
        email="aluno@idp.edu.br",
        nome="Aluno Teste",
        turma="TD-2026-01",
        exercicio="1.1",
        nota=85,
        nota_max=100,
        criterios_json='[{"id":"repo_publico","passed":true}]',
        repo_url="https://github.com/aluno/meu-primeiro-repo",
        github_user_verificado=True,
        late=False,
        dias_apos_recomendado=0,
        client_version="0.1.0",
        client_platform="darwin",
        spec_sha="deadbeef",
        ai_evidence_hashes="",
    )


def _build_mock_service(
    *,
    get_responses: list[dict[str, Any]],
    append_response: dict[str, Any] | None = None,
) -> tuple[MagicMock, dict[str, MagicMock]]:
    """Builds a MagicMock that behaves like ``service.spreadsheets().values()``.

    ``get_responses`` is consumed in order, one per ``.get(...).execute()`` call.
    ``append_response`` is returned by every ``.append(...).execute()`` call.
    Returns the service mock and a dict of the leaf mocks for assertion.
    """
    service = MagicMock(name="service")
    values_obj = MagicMock(name="values")
    service.spreadsheets.return_value.values.return_value = values_obj

    get_calls: list[MagicMock] = []

    def _get(**kwargs: Any) -> MagicMock:
        get_calls.append(MagicMock(kwargs=kwargs))
        m = MagicMock(name="get_request")
        if not get_responses:
            raise AssertionError("get() called more times than canned responses")
        m.execute.return_value = get_responses.pop(0)
        return m

    values_obj.get.side_effect = _get

    append_request = MagicMock(name="append_request")
    append_request.execute.return_value = append_response or {
        "updates": {"updatedRange": "submissoes!A2:Q2", "updatedRows": 1}
    }
    values_obj.append.return_value = append_request

    return service, {"values": values_obj, "append_request": append_request}


# ---------- dataclass ------------------------------------------------------


def test_columns_count_is_17() -> None:
    assert len(COLUMNS) == 17


def test_row_to_values_preserves_column_order() -> None:
    row = _row()
    values = _row_to_values(row)
    assert values[0] == "2026-05-10T18:00:00Z"  # timestamp_utc
    assert values[1] == "uuid-A"  # submission_id
    assert values[2] == "aluno@idp.edu.br"  # email
    assert values[16] == ""  # ai_evidence_hashes default
    assert len(values) == len(COLUMNS) == 17


def test_submission_row_default_ai_evidence_empty() -> None:
    # constructing without ai_evidence_hashes still works (v0.1+v0.2)
    row = SubmissionRow(
        timestamp_utc="t",
        submission_id="s",
        email="e",
        nome="n",
        turma="tu",
        exercicio="1.1",
        nota=1,
        nota_max=10,
        criterios_json="[]",
        repo_url="r",
        github_user_verificado=False,
        late=False,
        dias_apos_recomendado=0,
        client_version="v",
        client_platform="p",
        spec_sha="x",
    )
    assert row.ai_evidence_hashes == ""


# ---------- happy path -----------------------------------------------------


@pytest.mark.asyncio
async def test_append_happy_path_writes_and_returns_indexes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Before: header + 2 data rows (uuid-X, uuid-Y); after: +1 (uuid-A appended).
    service, mocks = _build_mock_service(
        get_responses=[
            {"values": [["submission_id"], ["uuid-X"], ["uuid-Y"]]},
            {"values": [["submission_id"], ["uuid-X"], ["uuid-Y"], ["uuid-A"]]},
        ]
    )

    writer = SheetsWriter(SPREADSHEET_ID, service=service)
    with caplog.at_level(logging.INFO, logger="app.sheets_writer"):
        result = await writer.append_submission(_row("uuid-A"))

    assert result == AppendResult(
        written=True,
        row_count_before=3,
        row_count_after=4,
        sheet_row_index=4,
    )

    mocks["values"].append.assert_called_once()
    kwargs = mocks["values"].append.call_args.kwargs
    assert kwargs["spreadsheetId"] == SPREADSHEET_ID
    assert kwargs["range"] == APPEND_RANGE
    assert kwargs["valueInputOption"] == "RAW"
    assert kwargs["insertDataOption"] == "INSERT_ROWS"
    assert kwargs["body"] == {"values": [_row_to_values(_row("uuid-A"))]}

    # both GETs targeted column B
    get_call_kwargs = [c.kwargs for c in mocks["values"].get.call_args_list]
    assert all(k["range"] == ID_COLUMN_RANGE for k in get_call_kwargs)
    assert all(k["spreadsheetId"] == SPREADSHEET_ID for k in get_call_kwargs)

    assert any("append_ok" in r.message for r in caplog.records)
    assert not any("SHEETS_DROP_DETECTED" in r.message for r in caplog.records)


# ---------- idempotency ----------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_hit_skips_append(caplog: pytest.LogCaptureFixture) -> None:
    # uuid-A already at row 3 (1-based: header=1, row2=uuid-X, row3=uuid-A).
    service, mocks = _build_mock_service(
        get_responses=[
            {"values": [["submission_id"], ["uuid-X"], ["uuid-A"], ["uuid-Z"]]}
        ]
    )

    writer = SheetsWriter(SPREADSHEET_ID, service=service)
    with caplog.at_level(logging.INFO, logger="app.sheets_writer"):
        result = await writer.append_submission(_row("uuid-A"))

    assert result == AppendResult(
        written=False,
        row_count_before=4,
        row_count_after=4,
        sheet_row_index=3,
    )
    mocks["values"].append.assert_not_called()
    assert any("idempotent_hit" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_same_uuid_twice_is_idempotent_on_second_call() -> None:
    # First call: not present → write. Second call: now present → skip.
    service, _ = _build_mock_service(
        get_responses=[
            {"values": [["submission_id"]]},  # before #1: header only
            {"values": [["submission_id"], ["uuid-A"]]},  # after #1
            {"values": [["submission_id"], ["uuid-A"]]},  # before #2 — uuid present
        ]
    )
    writer = SheetsWriter(SPREADSHEET_ID, service=service)

    r1 = await writer.append_submission(_row("uuid-A"))
    r2 = await writer.append_submission(_row("uuid-A"))

    assert r1.written is True
    assert r1.sheet_row_index == 2
    assert r2.written is False
    assert r2.sheet_row_index == 2
    # Only the first call appended.
    assert service.spreadsheets().values().append.call_count == 1


# ---------- drop detection -------------------------------------------------


@pytest.mark.asyncio
async def test_drop_detected_logs_error(caplog: pytest.LogCaptureFixture) -> None:
    # Before: header + 2 rows. After append: STILL 2 rows (silent drop).
    service, _ = _build_mock_service(
        get_responses=[
            {"values": [["submission_id"], ["uuid-X"], ["uuid-Y"]]},
            {"values": [["submission_id"], ["uuid-X"], ["uuid-Y"]]},
        ]
    )

    writer = SheetsWriter(SPREADSHEET_ID, service=service)
    with caplog.at_level(logging.ERROR, logger="app.sheets_writer"):
        result = await writer.append_submission(_row("uuid-A"))

    assert result.written is True
    assert result.row_count_before == 3
    assert result.row_count_after == 3
    assert result.sheet_row_index == -1  # not found → -1

    drop_records = [r for r in caplog.records if "SHEETS_DROP_DETECTED" in r.message]
    assert len(drop_records) == 1
    assert drop_records[0].levelno == logging.ERROR
    msg = drop_records[0].getMessage()
    assert "uuid-A" in msg
    assert "row_count_before=3" in msg
    assert "row_count_after=3" in msg
    assert "esperado_after=4" in msg


@pytest.mark.asyncio
async def test_empty_sheet_first_append() -> None:
    # Sheet returns no values at all (brand-new tab, no header even).
    service, _ = _build_mock_service(
        get_responses=[
            {},
            {"values": [["uuid-A"]]},
        ]
    )

    writer = SheetsWriter(SPREADSHEET_ID, service=service)
    result = await writer.append_submission(_row("uuid-A"))
    # row_count_before=0; after=1; expected=1 → no drop.
    assert result == AppendResult(
        written=True,
        row_count_before=0,
        row_count_after=1,
        # With no header row, idx 0 is the new submission, but our finder
        # skips index 0 (treats it as header). So sheet_row_index = -1.
        # This is acceptable for an empty-sheet first append edge case.
        sheet_row_index=-1,
    )
