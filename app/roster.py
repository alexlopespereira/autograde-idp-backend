from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

REQUIRED_COLUMNS = ("email", "nome", "turma", "github_username")
# Subset que precisa estar PREENCHIDO em cada linha. 'nome' e 'github_username'
# podem chegar vazios no paste manual e ser completados depois via /me/profile.
REQUIRED_NONEMPTY_COLUMNS = ("email", "turma")
ROSTER_TTL_SECONDS = 300

T = TypeVar("T")


class RosterValidationError(Exception):
    """Raised when a roster CSV violates schema or uniqueness constraints."""


@dataclass(frozen=True)
class RosterEntry:
    email: str
    nome: str
    turma: str
    github_username: str


def parse_roster(csv_text: str) -> dict[str, RosterEntry]:
    if not csv_text or not csv_text.strip():
        raise RosterValidationError("CSV vazio")

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise RosterValidationError("CSV sem header")

    missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
    if missing:
        raise RosterValidationError(f"colunas obrigatorias faltantes: {missing}")

    result: dict[str, RosterEntry] = {}
    for idx, row in enumerate(reader, start=2):  # header is line 1
        for col in REQUIRED_NONEMPTY_COLUMNS:
            value = (row.get(col) or "").strip()
            if not value:
                raise RosterValidationError(f"row {idx}: campo '{col}' vazio")
        email = row["email"].strip()
        if email in result:
            raise RosterValidationError(f"row {idx}: email duplicado '{email}'")
        result[email] = RosterEntry(
            email=email,
            nome=(row.get("nome") or "").strip(),
            turma=row["turma"].strip(),
            github_username=(row.get("github_username") or "").strip(),
        )

    if not result:
        raise RosterValidationError("CSV sem linhas de dados")

    return result


_CACHE: dict[str, tuple[float, Any]] = {}


def _now() -> float:
    return time.time()


def get_or_fetch(
    url: str,
    fetcher: Callable[[str], T],
    ttl_seconds: int,
) -> T:
    if ttl_seconds <= 0:
        return fetcher(url)
    now = _now()
    cached = _CACHE.get(url)
    if cached is not None:
        cached_at, cached_value = cached
        if (now - cached_at) < ttl_seconds:
            return cached_value
    value = fetcher(url)
    _CACHE[url] = (now, value)
    return value


def _clear_cache() -> None:
    _CACHE.clear()


def fetch_roster(
    url: str,
    fetcher: Callable[[str], str],
) -> dict[str, RosterEntry]:
    def _fetch_and_parse(u: str) -> dict[str, RosterEntry]:
        return parse_roster(fetcher(u))

    return get_or_fetch(url, _fetch_and_parse, ttl_seconds=ROSTER_TTL_SECONDS)
