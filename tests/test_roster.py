import pytest

from app import roster
from app.roster import (
    RosterEntry,
    RosterValidationError,
    fetch_roster,
    parse_roster,
)

HAPPY_CSV = (
    "email,nome,turma,github_username\n"
    "ana@idp.edu.br,Ana Silva,TD-2026-01,anasilva\n"
    "beto@idp.edu.br,Beto Souza,TD-2026-01,betosouza\n"
)


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    roster._clear_cache()
    yield
    roster._clear_cache()


def test_parse_roster_happy_path():
    result = parse_roster(HAPPY_CSV)
    assert len(result) == 2
    assert result["ana@idp.edu.br"] == RosterEntry(
        email="ana@idp.edu.br",
        nome="Ana Silva",
        turma="TD-2026-01",
        github_username="anasilva",
    )
    assert result["beto@idp.edu.br"].github_username == "betosouza"


def test_parse_roster_empty_raises():
    with pytest.raises(RosterValidationError, match="vazio"):
        parse_roster("")


def test_parse_roster_duplicate_email_raises():
    csv_text = (
        "email,nome,turma,github_username\n"
        "ana@idp.edu.br,Ana Silva,TD-2026-01,anasilva\n"
        "ana@idp.edu.br,Ana Outra,TD-2026-02,outraana\n"
    )
    with pytest.raises(RosterValidationError, match="duplicado.*ana@idp.edu.br"):
        parse_roster(csv_text)


def test_parse_roster_missing_required_column_raises():
    csv_text = "email,nome,turma\nana@idp.edu.br,Ana Silva,TD-2026-01\n"
    with pytest.raises(RosterValidationError, match="github_username"):
        parse_roster(csv_text)


def test_parse_roster_empty_field_raises():
    csv_text = "email,nome,turma,github_username\nana@idp.edu.br,,TD-2026-01,anasilva\n"
    with pytest.raises(RosterValidationError, match="row 2.*nome.*vazio"):
        parse_roster(csv_text)


def test_fetch_roster_caches_within_ttl(monkeypatch):
    calls = {"n": 0}

    def fetcher(url: str) -> str:
        calls["n"] += 1
        return HAPPY_CSV

    fake_clock = [1000.0]
    monkeypatch.setattr(roster, "_now", lambda: fake_clock[0])

    url = "https://example.com/roster.csv"

    # 1st call: fetcher invoked
    r1 = fetch_roster(url, fetcher=fetcher)
    assert calls["n"] == 1
    assert "ana@idp.edu.br" in r1

    # within TTL: cached
    fake_clock[0] += 100
    fetch_roster(url, fetcher=fetcher)
    assert calls["n"] == 1

    # just before TTL: still cached
    fake_clock[0] += 199  # total elapsed = 299
    fetch_roster(url, fetcher=fetcher)
    assert calls["n"] == 1

    # past TTL (300s): refetched
    fake_clock[0] += 2  # total elapsed = 301
    fetch_roster(url, fetcher=fetcher)
    assert calls["n"] == 2
