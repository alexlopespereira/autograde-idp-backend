"""Smoke E2E test for the autograde backend + CLI (US-15).

Sobe a aplicação FastAPI real via uvicorn em uma thread (porta 18080),
aplica monkeypatches mínimos para externalizar dependências caras
(roster fetch, OAuth verify, GitHub API, Sheets API) e exercita o fluxo
ponta-a-ponta:

  Test 1 — Ex 1.1: CLI ``autograde validar 1.1`` com auto-submit; valida
           que /grade-preview e /submissions são chamados, nota >= 80, e
           que a fake sheets recebeu o append.
  Test 2 — Ex 1.2: CLI ``autograde validar 1.2`` com ``subprocess.run``
           mockado para simular ``gh`` instalado; valida shell_evidence
           presente no payload.
  Test 3 — idempotência: dispara /submissions 2x com o mesmo
           ``submission_uuid`` direto via HTTP; segunda chamada retorna
           ``written=false``.
  Test 4 — multi-turma: aluno MBA tenta /grade-preview de Ex 1.1
           (``turmas=[TD-2026-01]``) e recebe 403 ``turma_not_eligible``.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
import requests
import uvicorn
from jose import jwt

# --- Imports do produto sob teste --------------------------------------------
from app import auth as backend_auth
from app import endpoints as backend_endpoints
from app.curriculum import parse_exercise_yaml
from app.main import app as backend_app
from app.sheets_writer import AppendResult, SubmissionRow

from autograde_idp import auth as cli_auth
from autograde_idp import cli as cli_module
from autograde_idp import validar as cli_validar
from autograde_idp.auth import TokenBundle
from autograde_idp.evidence import shell as cli_shell

# --- Constantes globais ------------------------------------------------------
PORT = 18080
BASE_URL = f"http://127.0.0.1:{PORT}"
JWT_SECRET = "smoke-e2e-test-secret"  # noqa: S105 - test-only HS256 key
JWT_AUDIENCE = "smoke-e2e.apps.googleusercontent.com"

TD_EMAIL = "ana.silva@aluno.idp.edu.br"
TD_NAME = "Ana Silva"
TD_TURMA = "TD-2026-01"
TD_GH_USER = "anasilva"

MBA_EMAIL = "joao.mba@aluno.idp.edu.br"
MBA_NAME = "Joao MBA"
MBA_TURMA = "MBA-IDP-2026"
MBA_GH_USER = "joaomba"

EXERCICIOS_DIR = Path(__file__).resolve().parent / "fixtures" / "exercicios"


# --- Fakes (in-memory replacements) ------------------------------------------
class FakeSheetsWriter:
    """Append-only in-memory replacement for SheetsWriter (AC1).

    Reproduz a semântica de idempotência por ``submission_id`` e devolve
    ``AppendResult`` compatível com o endpoint /submissions real.
    """

    def __init__(self) -> None:
        self.rows: list[SubmissionRow] = []
        # Cabeçalho fictício para simular a primeira linha da planilha
        # (read_submissions ignora linhas com índices < cabeçalho).
        self._header = ["timestamp_utc"]

    async def append_submission(self, row: SubmissionRow) -> AppendResult:
        for existing in self.rows:
            if existing.submission_id == row.submission_id:
                return AppendResult(
                    written=False,
                    row_count_before=len(self.rows) + 1,
                    row_count_after=len(self.rows) + 1,
                    sheet_row_index=self.rows.index(existing) + 2,
                )
        before = len(self.rows) + 1  # +1 = header row
        self.rows.append(row)
        after = len(self.rows) + 1
        return AppendResult(
            written=True,
            row_count_before=before,
            row_count_after=after,
            sheet_row_index=after,
        )

    async def read_submissions(self) -> list[list[str]]:
        out: list[list[str]] = [self._header]
        for r in self.rows:
            out.append([
                r.timestamp_utc, r.submission_id, r.email, r.nome, r.turma,
                r.exercicio, str(r.nota), str(r.nota_max), r.criterios_json,
                r.repo_url, str(r.github_user_verificado), str(r.late),
                str(r.dias_apos_recomendado), r.client_version,
                r.client_platform, r.spec_sha, r.ai_evidence_hashes,
            ])
        return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_github_evidence(repo_url: str) -> dict[str, Any]:
    """Constrói evidence dict que faz Ex 1.1 e Ex 1.2 passarem.

    Para Ex 1.1, garante: repo_exists, repo_public, README.md presente
    com tamanho>0, 2+ commits recentes (timestamp now()), nome do repo
    casa com 'meu-primeiro-repo'.
    Para Ex 1.2, garante: repo público + 1 PR com título descritivo.
    """
    owner, _, repo = repo_url.rpartition("/")
    owner = owner.rsplit("/", 1)[-1].replace(".git", "")
    repo = repo.replace(".git", "")
    owner_repo = f"{owner}/{repo}"
    now = _now_iso()
    return {
        "owner_repo": owner_repo,
        "repo_exists": True,
        "repo_public": True,
        "files_list": ["README.md"],
        "file_sizes": {"README.md": 128},
        "commits": [
            {"sha": "a" * 40, "message": "feat: inicial", "author_email": f"{owner}@test.dev",
             "committed_at": now},
            {"sha": "b" * 40, "message": "docs: readme", "author_email": f"{owner}@test.dev",
             "committed_at": now},
        ],
        "branches": ["main"],
        "prs_open": [
            {"number": 1, "title": "Adiciona pipeline de leitura de CSV",
             "state": "open", "merged_at": None},
        ],
        "prs_merged": [],
    }


class FakeGitHubClient:
    """Substituto de GitHubClient para o smoke E2E (AC4)."""

    def collect_evidence(self, repo_url: str) -> dict[str, Any]:
        return _build_github_evidence(repo_url)


# --- JWT helpers -------------------------------------------------------------
def _make_jwt(email: str, name: str) -> str:
    return jwt.encode(
        {"email": email, "name": name, "sub": email, "aud": JWT_AUDIENCE},
        JWT_SECRET,
        algorithm="HS256",
    )


def _fake_verify(token: str, _request: Any, audience: str) -> dict[str, Any]:
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience=audience)


# --- Roster + Exercise loaders patched in --------------------------------------
def _roster_fixture() -> dict[str, backend_auth.RosterEntry]:
    return {
        TD_EMAIL: backend_auth.RosterEntry(
            email=TD_EMAIL, nome=TD_NAME, turma=TD_TURMA, github_username=TD_GH_USER,
        ),
        MBA_EMAIL: backend_auth.RosterEntry(
            email=MBA_EMAIL, nome=MBA_NAME, turma=MBA_TURMA, github_username=MBA_GH_USER,
        ),
    }


def _load_exercise_from_disk(exercicio_id: str) -> tuple[Any, str]:
    """Replacement para endpoints.load_exercise: lê YAML do disco."""
    path = EXERCICIOS_DIR / f"{exercicio_id}.yaml"
    yaml_text = path.read_text(encoding="utf-8")
    exercise = parse_exercise_yaml(yaml_text)
    return exercise, yaml_text


# --- Server fixtures (uvicorn em thread) -------------------------------------
@pytest.fixture(scope="module")
def fake_sheets() -> FakeSheetsWriter:
    return FakeSheetsWriter()


@pytest.fixture(scope="module", autouse=True)
def patch_backend(fake_sheets: FakeSheetsWriter) -> Iterator[None]:
    """Aplica monkeypatches no backend antes do server subir (escopo módulo).

    Uso direto de ``pytest.MonkeyPatch()`` (não a fixture, que é function-scoped).
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("GOOGLE_OAUTH_CLIENT_ID", JWT_AUDIENCE)
    mp.setenv("ROSTER_URL", "file://stub")  # nunca usado: get_roster é patcheado
    mp.setenv("SHEET_ID", "stub-sheet-id")
    mp.setattr(backend_auth.id_token, "verify_oauth2_token", _fake_verify)
    mp.setattr(backend_auth, "get_roster", _roster_fixture)
    mp.setattr(backend_endpoints, "load_exercise", _load_exercise_from_disk)
    mp.setattr(backend_endpoints, "get_github_client", lambda: FakeGitHubClient())
    mp.setattr(backend_endpoints, "get_sheets_writer", lambda: fake_sheets)
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def server(patch_backend: None) -> Iterator[uvicorn.Server]:  # noqa: ARG001
    """Sobe uvicorn em thread daemon na porta 18080 (AC1)."""
    config = uvicorn.Config(
        backend_app,
        host="127.0.0.1",
        port=PORT,
        log_level="error",
        access_log=False,
    )
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            resp = requests.get(f"{BASE_URL}/healthz", timeout=1)
            if resp.status_code == 200:
                break
        except requests.RequestException:
            time.sleep(0.1)
    else:
        srv.should_exit = True
        thread.join(timeout=2)
        pytest.fail(f"uvicorn não subiu em {BASE_URL}/healthz")

    yield srv

    srv.should_exit = True
    thread.join(timeout=5)


# --- CLI fixtures (token + env + monkeypatches in-process) -------------------
@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Configura ambiente do CLI: tmp config_dir, token válido, env vars."""
    config_dir = tmp_path / ".git-exercicios"
    config_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(cli_auth, "config_dir", lambda: config_dir)
    monkeypatch.setattr(cli_validar, "config_dir", lambda: config_dir)
    monkeypatch.setenv("AUTOGRADE_API_URL", BASE_URL)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", JWT_AUDIENCE)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-secret")  # noqa: S105
    return config_dir


def _seed_token(config_dir: Path, email: str, name: str) -> None:
    """Persiste TokenBundle válido (expira em +1h) para `email`."""
    bundle = TokenBundle(
        access_token=_make_jwt(email, name),
        refresh_token="refresh-stub",
        id_token=_make_jwt(email, name),
        expires_at=time.time() + 3600,
        first_login_at=time.time(),
        client_id=JWT_AUDIENCE,
    )
    cli_auth.save_token(bundle, path=config_dir / "token.json")


# --- Tests -------------------------------------------------------------------
def test_smoke_ex_1_1_happy_path(
    server: uvicorn.Server,  # noqa: ARG001 - fixture chain
    cli_env: Path,
    fake_sheets: FakeSheetsWriter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC5 — Ex 1.1: CLI submete via auto-submit, nota >= 80, sheet recebeu row."""
    fake_sheets.rows.clear()
    _seed_token(cli_env, TD_EMAIL, TD_NAME)
    monkeypatch.setattr(
        cli_validar,
        "detect_repo_url",
        lambda *_a, **_k: f"https://github.com/{TD_GH_USER}/meu-primeiro-repo",
    )

    rc = cli_module.main(["validar", "1.1", "--auto-submit"])
    out = capsys.readouterr().out

    assert rc == 0, f"exit code esperado 0, recebido {rc}; saida: {out}"
    assert "Total:" in out
    assert "Submetido" in out
    assert "written=True" in out
    assert len(fake_sheets.rows) == 1
    row = fake_sheets.rows[0]
    assert row.email == TD_EMAIL
    assert row.exercicio == "1.1"
    assert row.nota >= 80, f"nota {row.nota} < 80; bulletin: {row.criterios_json}"


def test_smoke_ex_1_2_with_gh_mock(
    server: uvicorn.Server,  # noqa: ARG001
    cli_env: Path,
    fake_sheets: FakeSheetsWriter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC6 — Ex 1.2: subprocess.run mockado para 'gh', shell_evidence enviado."""
    fake_sheets.rows.clear()
    _seed_token(cli_env, TD_EMAIL, TD_NAME)
    monkeypatch.setattr(
        cli_validar,
        "detect_repo_url",
        lambda *_a, **_k: f"https://github.com/{TD_GH_USER}/projeto-cli",
    )
    monkeypatch.setattr(cli_shell.shutil, "which", lambda _b: "/fake/bin/gh")

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        joined = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        if joined == "gh --version":
            stdout = "gh version 2.40.1 (2024-01-01)\n"
        elif joined.startswith("gh auth status"):
            stdout = f"Logged in to github.com account {TD_GH_USER} (keyring)\n"
        elif joined.startswith("gh repo view"):
            stdout = (
                '{"name":"projeto-cli","visibility":"PUBLIC","isPrivate":false}\n'
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(cli_shell.subprocess, "run", fake_run)

    rc = cli_module.main(["validar", "1.2", "--auto-submit"])
    out = capsys.readouterr().out

    assert rc == 0, f"exit code esperado 0, recebido {rc}; saida: {out}"
    assert "Submetido" in out
    assert len(fake_sheets.rows) == 1
    row = fake_sheets.rows[0]
    assert row.exercicio == "1.2"
    # Os 3 critérios evidence.shell.* somam 40 pontos; precisam estar presentes E passing no boletim.
    # criterios_json serializa CriterioResult ({passed, points_earned, points_max, message}); sem id/check.
    # Validamos pela presença de mensagens-fingerprint que SÓ os primitives evidence.shell.* produzem.
    criterios = json.loads(row.criterios_json)
    assert len(criterios) == 6, f"esperado 6 critérios (Ex 1.2), recebido {len(criterios)}: {criterios}"
    msgs = " ".join(c.get("message", "") for c in criterios).lower()
    assert "gh version" in msgs, (
        f"primitive evidence.shell.gh_version_present não rodou — shell_evidence ausente do payload? msgs: {msgs}"
    )
    assert TD_GH_USER.lower() in msgs, (
        f"primitive evidence.shell.gh_auth_ok não pegou {TD_GH_USER}; msgs: {msgs}"
    )
    # Total de critérios passando: 3 API + pelo menos 1 shell (com nota >= 80 e API=60 max, shell precisa ≥20pts)
    passing_count = sum(1 for c in criterios if c.get("passed"))
    assert passing_count >= 5, (
        f"esperado >=5 critérios passando (3 API + ≥2 shell), recebido {passing_count}: {criterios}"
    )
    assert row.nota >= 80, f"nota {row.nota} < 80; bulletin: {row.criterios_json}"


def test_smoke_idempotency_same_uuid(
    server: uvicorn.Server,  # noqa: ARG001
    fake_sheets: FakeSheetsWriter,
) -> None:
    """AC7 — chama /submissions 2x com mesmo uuid; segunda diz written=false."""
    fake_sheets.rows.clear()
    token = _make_jwt(TD_EMAIL, TD_NAME)
    body = {
        "exercicio": "1.1",
        "repo_url": f"https://github.com/{TD_GH_USER}/meu-primeiro-repo",
        "submission_uuid": "uuid-idempotency-001",
    }
    headers = {"Authorization": f"Bearer {token}"}

    r1 = requests.post(f"{BASE_URL}/submissions", json=body, headers=headers, timeout=10)
    r2 = requests.post(f"{BASE_URL}/submissions", json=body, headers=headers, timeout=10)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["written"] is True
    assert r2.json()["written"] is False
    assert len(fake_sheets.rows) == 1


def test_smoke_multi_turma_blocks_mba_aluno(
    server: uvicorn.Server,  # noqa: ARG001
) -> None:
    """AC8 — aluno MBA tenta /grade-preview de Ex 1.1 (TD-only) → 403."""
    token = _make_jwt(MBA_EMAIL, MBA_NAME)
    body = {
        "exercicio": "1.1",
        "repo_url": f"https://github.com/{MBA_GH_USER}/meu-primeiro-repo",
    }
    resp = requests.post(
        f"{BASE_URL}/grade-preview",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"error": "turma_not_eligible"}


