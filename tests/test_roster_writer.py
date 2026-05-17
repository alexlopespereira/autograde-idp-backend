from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.roster_writer import (
    EMAIL_COLUMN_RANGE,
    ProfileUpdateResult,
    RosterWriter,
    UserNotInRoster,
)

SPREADSHEET_ID = "roster-sheet-abc"
EMAIL = "aluno@idp.edu.br"


def _build_mock_service(
    *, get_responses: list[dict[str, Any]]
) -> tuple[MagicMock, dict[str, MagicMock]]:
    """Mock of ``service.spreadsheets().values()``.

    ``get_responses`` is consumed in order, one per ``.get(...).execute()`` call.
    ``.update(...).execute()`` always returns a stub success.
    """
    service = MagicMock(name="service")
    values_obj = MagicMock(name="values")
    service.spreadsheets.return_value.values.return_value = values_obj

    def _get(**kwargs: Any) -> MagicMock:
        m = MagicMock(name="get_request", kwargs=kwargs)
        if not get_responses:
            raise AssertionError("get() called more times than canned responses")
        m.execute.return_value = get_responses.pop(0)
        return m

    values_obj.get.side_effect = _get

    update_request = MagicMock(name="update_request")
    update_request.execute.return_value = {
        "spreadsheetId": SPREADSHEET_ID,
        "updatedRange": "B5",
        "updatedRows": 1,
    }
    values_obj.update.return_value = update_request

    return service, {"values": values_obj, "update_request": update_request}


# ---------- ProfileUpdateResult dataclass ----------------------------------


def test_profile_update_result_is_frozen_with_two_list_fields() -> None:
    r = ProfileUpdateResult(updated=["nome"], skipped=["github_username"])
    assert r.updated == ["nome"]
    assert r.skipped == ["github_username"]
    with pytest.raises(Exception):
        r.updated = ["x"]  # frozen


# ---------- get_row_index --------------------------------------------------


def test_get_row_index_finds_existing_email() -> None:
    service, mocks = _build_mock_service(
        get_responses=[
            {
                "values": [
                    ["email"],
                    ["outro@idp.edu.br"],
                    [EMAIL],
                    ["mais@idp.edu.br"],
                ]
            }
        ]
    )
    writer = RosterWriter(SPREADSHEET_ID, service=service)
    assert writer.get_row_index(EMAIL) == 3

    kwargs = mocks["values"].get.call_args.kwargs
    assert kwargs["spreadsheetId"] == SPREADSHEET_ID
    assert kwargs["range"] == EMAIL_COLUMN_RANGE


def test_get_row_index_returns_none_when_email_absent() -> None:
    service, _ = _build_mock_service(
        get_responses=[{"values": [["email"], ["a@idp.edu.br"], ["b@idp.edu.br"]]}]
    )
    writer = RosterWriter(SPREADSHEET_ID, service=service)
    assert writer.get_row_index(EMAIL) is None


def test_get_row_index_skips_header_when_email_matches_first_row() -> None:
    # Header row 1 contains the literal string equal to the email — must skip.
    service, _ = _build_mock_service(
        get_responses=[
            {"values": [[EMAIL], ["someone@idp.edu.br"], [EMAIL]]}
        ]
    )
    writer = RosterWriter(SPREADSHEET_ID, service=service)
    assert writer.get_row_index(EMAIL) == 3


# ---------- update_profile happy path -------------------------------------


def test_update_profile_writes_both_cells_when_empty() -> None:
    # 1) get A:A finds email at row 3
    # 2) get B3 returns empty
    # 3) get D3 returns empty
    service, mocks = _build_mock_service(
        get_responses=[
            {"values": [["email"], ["x@idp.edu.br"], [EMAIL]]},
            {},  # B3 empty
            {},  # D3 empty
        ]
    )
    writer = RosterWriter(SPREADSHEET_ID, service=service)

    result = writer.update_profile(EMAIL, nome="Foo Bar", github_username="foo-bar")

    assert result == ProfileUpdateResult(
        updated=["nome", "github_username"], skipped=[]
    )

    update_calls = mocks["values"].update.call_args_list
    assert len(update_calls) == 2
    # Call 1: B3 = "Foo Bar"
    assert update_calls[0].kwargs["spreadsheetId"] == SPREADSHEET_ID
    assert update_calls[0].kwargs["range"] == "B3"
    assert update_calls[0].kwargs["valueInputOption"] == "RAW"
    assert update_calls[0].kwargs["body"] == {"values": [["Foo Bar"]]}
    # Call 2: D3 = "foo-bar"
    assert update_calls[1].kwargs["range"] == "D3"
    assert update_calls[1].kwargs["valueInputOption"] == "RAW"
    assert update_calls[1].kwargs["body"] == {"values": [["foo-bar"]]}


def test_update_profile_uses_raw_value_input_option() -> None:
    # Explicit AC6(f): a github_username com '=' não deve virar fórmula.
    service, mocks = _build_mock_service(
        get_responses=[
            {"values": [["email"], [EMAIL]]},
            {},  # B2 empty
            {},  # D2 empty
        ]
    )
    writer = RosterWriter(SPREADSHEET_ID, service=service)
    writer.update_profile(EMAIL, nome="X", github_username="foo=BAR()")

    for call in mocks["values"].update.call_args_list:
        assert call.kwargs["valueInputOption"] == "RAW"


def test_update_profile_partial_update_when_one_cell_already_filled() -> None:
    # B2 already has a name, D2 is empty.
    service, mocks = _build_mock_service(
        get_responses=[
            {"values": [["email"], [EMAIL]]},
            {"values": [["Nome Existente"]]},  # B2 occupied
            {},  # D2 empty
        ]
    )
    writer = RosterWriter(SPREADSHEET_ID, service=service)
    result = writer.update_profile(EMAIL, nome="Outro Nome", github_username="gh-user")

    assert result == ProfileUpdateResult(
        updated=["github_username"], skipped=["nome"]
    )

    update_calls = mocks["values"].update.call_args_list
    assert len(update_calls) == 1
    assert update_calls[0].kwargs["range"] == "D2"
    assert update_calls[0].kwargs["body"] == {"values": [["gh-user"]]}


# ---------- anti-hijacking (AC4) ------------------------------------------


def test_update_profile_anti_hijacking_second_call_does_not_overwrite() -> None:
    # First call: row 2 found, B2 + D2 vazios → both written.
    # Second call: same email, but now B2 = 'Nome Original', D2 = 'orig-user'.
    service, mocks = _build_mock_service(
        get_responses=[
            # --- first call ---
            {"values": [["email"], [EMAIL]]},  # get A:A
            {},  # B2 empty
            {},  # D2 empty
            # --- second call ---
            {"values": [["email"], [EMAIL]]},  # get A:A again
            {"values": [["Nome Original"]]},  # B2 now filled
            {"values": [["orig-user"]]},  # D2 now filled (hijacking attempt)
        ]
    )
    writer = RosterWriter(SPREADSHEET_ID, service=service)

    r1 = writer.update_profile(EMAIL, nome="Nome Original", github_username="orig-user")
    r2 = writer.update_profile(EMAIL, nome="Hijacker", github_username="hijacker-gh")

    assert r1 == ProfileUpdateResult(updated=["nome", "github_username"], skipped=[])
    assert r2 == ProfileUpdateResult(updated=[], skipped=["nome", "github_username"])

    # Total update calls: 2 from first call only.
    assert mocks["values"].update.call_count == 2


# ---------- email not in roster -------------------------------------------


def test_update_profile_raises_user_not_in_roster_for_absent_email() -> None:
    service, mocks = _build_mock_service(
        get_responses=[
            {"values": [["email"], ["outro@idp.edu.br"]]},
        ]
    )
    writer = RosterWriter(SPREADSHEET_ID, service=service)

    with pytest.raises(UserNotInRoster) as exc_info:
        writer.update_profile(EMAIL, nome="X", github_username="x")

    assert EMAIL in str(exc_info.value)
    # No update should have been called.
    mocks["values"].update.assert_not_called()


# ---------- range targeting (AC3) -----------------------------------------


def test_update_profile_reads_b_and_d_cells_for_resolved_row() -> None:
    service, mocks = _build_mock_service(
        get_responses=[
            {"values": [["email"], ["a@x"], ["b@x"], [EMAIL]]},  # row 4
            {},  # B4
            {},  # D4
        ]
    )
    writer = RosterWriter(SPREADSHEET_ID, service=service)
    writer.update_profile(EMAIL, nome="N", github_username="g")

    get_ranges = [c.kwargs["range"] for c in mocks["values"].get.call_args_list]
    assert get_ranges == [EMAIL_COLUMN_RANGE, "B4", "D4"]
