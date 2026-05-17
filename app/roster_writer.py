"""Roster Sheet writer: completa `nome` e `github_username` no perfil do aluno.

Layout esperado da Roster Sheet (mesmo CSV consumido por `app.roster`):
    A: email | B: nome | C: turma | D: github_username

Contrato anti-hijacking: cada célula só é escrita se estiver vazia. Aluno
que já tem `github_username` preenchido NÃO pode sobrescrever — protege
contra o caso em que aluno A loga e tenta cravar o GitHub de aluno B.

Auth via Application Default Credentials (`google.auth.default`) — em
produção usa a Service Account do Cloud Run; em dev usa
`gcloud auth application-default login`. Requer Editor na Roster Sheet
(setup manual documentado em docs/setup.md §4.1).

valueInputOption='RAW' em todos os updates: github_username controlado por
usuário (ex: 'foo=BAR()') não pode ser interpretado como fórmula do Sheets.
"""

from __future__ import annotations

from dataclasses import dataclass

import google.auth
from googleapiclient.discovery import Resource, build

SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
EMAIL_COLUMN_RANGE = "A:A"
NOME_COLUMN = "B"
GITHUB_COLUMN = "D"


@dataclass(frozen=True)
class ProfileUpdateResult:
    updated: list[str]
    skipped: list[str]


class UserNotInRoster(Exception):
    """Raised when update_profile is called for an email not present in column A."""


def _build_service() -> Resource:
    creds, _ = google.auth.default(scopes=list(SCOPES))
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


class RosterWriter:
    """Single-cell editor for the Roster Sheet (anti-hijacking: empty-only writes)."""

    def __init__(self, spreadsheet_id: str, *, service: Resource | None = None):
        self.spreadsheet_id = spreadsheet_id
        self._service = service if service is not None else _build_service()

    def get_row_index(self, email: str) -> int | None:
        resp = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=EMAIL_COLUMN_RANGE)
            .execute()
        )
        values = resp.get("values", []) or []
        for i, row in enumerate(values):
            if i == 0:
                continue
            if row and row[0] == email:
                return i + 1
        return None

    def update_profile(
        self, email: str, nome: str, github_username: str
    ) -> ProfileUpdateResult:
        row = self.get_row_index(email)
        if row is None:
            raise UserNotInRoster(email)

        nome_cell = f"{NOME_COLUMN}{row}"
        github_cell = f"{GITHUB_COLUMN}{row}"

        current_nome = self._read_cell(nome_cell)
        current_github = self._read_cell(github_cell)

        updated: list[str] = []
        skipped: list[str] = []

        if current_nome == "":
            self._update_cell(nome_cell, nome)
            updated.append("nome")
        else:
            skipped.append("nome")

        if current_github == "":
            self._update_cell(github_cell, github_username)
            updated.append("github_username")
        else:
            skipped.append("github_username")

        return ProfileUpdateResult(updated=updated, skipped=skipped)

    def _read_cell(self, cell_range: str) -> str:
        resp = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=cell_range)
            .execute()
        )
        values = resp.get("values", []) or []
        if not values or not values[0]:
            return ""
        return values[0][0] if values[0] else ""

    def _update_cell(self, cell_range: str, value: str) -> None:
        self._service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=cell_range,
            valueInputOption="RAW",
            body={"values": [[value]]},
        ).execute()
