"""O calendário da turma governando /grade-preview e /submissions.

Estes testes travam a regressão de 2026-09 no ponto onde ela apareceu para o
aluno: `ia-1.1` foi reaproveitado numa turma nova, o `prazo:` do YAML ficou
com a data do semestre anterior, e toda submissão foi gravada com 107 dias de
atraso. Com o calendário por turma, o YAML podendo estar errado deixa de
importar — a data que vale é a da turma do aluno.
"""

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
from app import roster as roster_module
from app.auth import AuthMiddleware, RosterEntry
from app.calendario import calendario_url
from app.curriculum import Criterio, Exercise
from app.primitives import CriterioResult, registry
from app.sheets_writer import AppendResult

JWT_SECRET = "test-secret-not-google"
JWT_AUDIENCE = "test-audience.apps.googleusercontent.com"
NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

BASE = "https://exemplo/ia/exercicios"
EMAIL = "aluno@idp.edu.br"
GH = "aluno-gh"
PRIMITIVE_PASS = "test.calendario.always_pass"

# O YAML com a data ERRADA — a do semestre anterior, a que produziu os 107
# dias. Ele fica assim de propósito em todos os testes: o ponto é que o
# calendário da turma torna esse erro inofensivo.
PRAZO_VELHO = datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc)

CAL_IA = """
turma: IA-2026-01
padrao:
  abre:  2026-09-01T00:00:00-03:00
  fecha: 2026-10-20T23:59:59-03:00
exercicios:
  ia-1.1:
"""


def _make_token(email: str) -> str:
    return jwt.encode(
        {"email": email, "name": "Aluno", "sub": "x", "aud": JWT_AUDIENCE},
        JWT_SECRET,
        algorithm="HS256",
    )


def _exercise(*, turmas: tuple[str, ...] = (), com_prazo_velho: bool = True) -> Exercise:
    return Exercise(
        id="ia-1.1",
        titulo="Primeiro repositorio",
        turmas=turmas,
        disponivel_a_partir_de=(NOW - timedelta(days=200)) if turmas else None,
        prazo={"recomendado_ate": PRAZO_VELHO} if com_prazo_velho else {},
        criterios=(Criterio(id="c1", peso=100, check=PRIMITIVE_PASS, args={}),),
    )


@dataclass
class FakeGitHub:
    def collect_evidence(self, repo_url: str) -> dict[str, Any]:
        return {"repo_exists": True, "files_list": []}


@dataclass
class FakeSheets:
    linhas: list[Any]

    async def append_submission(self, row: Any) -> AppendResult:
        self.linhas.append(row)
        return AppendResult(
            written=True, row_count_before=0, row_count_after=1, sheet_row_index=1
        )

    async def read_submissions(self) -> list[list[str]]:
        return []


@pytest.fixture(autouse=True)
def _primitive_pass():
    registry[PRIMITIVE_PASS] = lambda args, ev: CriterioResult(
        True, args.get("_peso", 0), args.get("_peso", 0), "ok"
    )
    yield
    registry.pop(PRIMITIVE_PASS, None)


@pytest.fixture(autouse=True)
def _cache_limpo():
    roster_module._clear_cache()
    yield
    roster_module._clear_cache()


@pytest.fixture
def ambiente(monkeypatch):
    """Roster, token, relógio e base de URLs — tudo menos o calendário."""

    def fake_verify(token: str, request_obj, audience: str):
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience=audience)

    monkeypatch.setattr(auth_module.id_token, "verify_oauth2_token", fake_verify)
    monkeypatch.setattr(
        auth_module,
        "get_roster",
        lambda: {
            EMAIL: RosterEntry(
                email=EMAIL, nome="Aluno",
                turma="IA-2026-01", github_username=GH,
            )
        },
    )
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", JWT_AUDIENCE)
    monkeypatch.setenv("EXERCISES_BASE_URL_IA", BASE)
    monkeypatch.setattr(endpoints_module, "_now_utc", lambda: NOW)
    monkeypatch.setattr(endpoints_module, "get_github_client", lambda: FakeGitHub())
    return monkeypatch


def _montar(monkeypatch, exercise: Exercise, calendarios: dict[str, str] | None):
    """`calendarios=None` simula o calendário fora do ar; `{}` simula 404."""
    monkeypatch.setattr(
        endpoints_module, "load_exercise", lambda eid: (exercise, "exercicio: ia-1.1\n")
    )
    sheets = FakeSheets(linhas=[])
    monkeypatch.setattr(endpoints_module, "get_sheets_writer", lambda: sheets)

    def fetcher(url: str) -> str | None:
        if calendarios is None:
            raise RuntimeError("connection reset by peer")
        return calendarios.get(url)

    monkeypatch.setattr(endpoints_module, "_calendario_fetcher", fetcher)
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(endpoints_module.router)
    return app, sheets


async def _post(app: FastAPI, path: str, body: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(
            path, json=body, headers={"Authorization": f"Bearer {_make_token(EMAIL)}"}
        )


CORPO = {"exercicio": "ia-1.1", "repo_url": f"https://github.com/{GH}/projeto"}
COM_CAL = {calendario_url(BASE, "IA-2026-01"): CAL_IA}


# ---------- a regressão dos 107 dias ----------------------------------------


@pytest.mark.asyncio
async def test_prazo_do_calendario_vence_o_prazo_desatualizado_do_yaml(ambiente):
    """O caso do aluno, exatamente: YAML com 31/05, calendário com 20/10.

    Antes disso, `late=True` e `dias_apos_recomendado=107`.
    """
    app, _ = _montar(ambiente, _exercise(), COM_CAL)
    r = await _post(app, "/grade-preview", CORPO)
    assert r.status_code == 200
    assert r.json()["late"] is False
    assert r.json()["dias_apos_recomendado"] == 0


@pytest.mark.asyncio
async def test_atraso_real_contra_o_calendario_continua_sendo_marcado(ambiente):
    """O oposto do teste acima: o calendário não desliga a marcação de
    atraso, só passa a medi-la contra a data certa."""
    cal = CAL_IA.replace("2026-10-20T23:59:59-03:00", "2026-09-10T23:59:59-03:00")
    app, _ = _montar(ambiente, _exercise(), {calendario_url(BASE, "IA-2026-01"): cal})
    r = await _post(app, "/grade-preview", CORPO)
    assert r.json()["late"] is True
    assert r.json()["dias_apos_recomendado"] == 5


# ---------- matrícula pelo calendário ---------------------------------------


@pytest.mark.asyncio
async def test_estar_no_calendario_basta_sem_turmas_no_yaml(ambiente):
    """O YAML volta a ser só conteúdo: sem `turmas:`, sem
    `disponivel_a_partir_de`. Quem matricula é o calendário."""
    app, _ = _montar(ambiente, _exercise(com_prazo_velho=False), COM_CAL)
    assert (await _post(app, "/grade-preview", CORPO)).status_code == 200


@pytest.mark.asyncio
async def test_fora_do_calendario_e_sem_legado_recusa_dizendo_onde_conserta(ambiente):
    outro = (
        "turma: IA-2026-01\n"
        "exercicios:\n"
        "  ia-9.9:\n"
        "    abre: 2026-01-01T00:00:00-03:00\n"
    )
    app, _ = _montar(
        ambiente,
        _exercise(com_prazo_velho=False),
        {calendario_url(BASE, "IA-2026-01"): outro},
    )
    r = await _post(app, "/grade-preview", CORPO)
    assert r.status_code == 403
    assert r.json()["error"] == "turma_not_eligible"
    # A mensagem precisa apontar o roster, não `autograde login`.
    assert "roster" in r.json()["message"]


@pytest.mark.asyncio
async def test_abre_no_futuro_pelo_calendario_bloqueia(ambiente):
    cal = CAL_IA.replace("2026-09-01T00:00:00-03:00", "2026-12-01T00:00:00-03:00")
    app, _ = _montar(ambiente, _exercise(), {calendario_url(BASE, "IA-2026-01"): cal})
    r = await _post(app, "/grade-preview", CORPO)
    assert r.status_code == 403
    assert r.json()["error"] == "exercise_not_open_yet"


# ---------- degradação: erro nosso não vira culpa do aluno -------------------


@pytest.mark.asyncio
async def test_calendario_fora_do_ar_cai_no_legado_quando_ele_existe(ambiente):
    """A janela entre o deploy do backend e a publicação dos calendários é
    real. Enquanto o YAML legado responder, ele responde."""
    app, _ = _montar(ambiente, _exercise(turmas=("IA-2026-01",)), None)
    r = await _post(app, "/grade-preview", CORPO)
    assert r.status_code == 200
    # Sem calendário, o prazo velho do YAML volta a valer — e a submissão
    # sai marcada como atrasada. É pior que o caminho novo, e é melhor que
    # recusar o aluno.
    assert r.json()["late"] is True


@pytest.mark.asyncio
async def test_calendario_fora_do_ar_sem_legado_e_502_nao_403(ambiente):
    """`turma_not_eligible` mandaria o aluno pedir ao professor para corrigir
    o roster por causa de um GitHub fora do ar. 502 diz de quem é o problema."""
    app, _ = _montar(ambiente, _exercise(com_prazo_velho=False), None)
    r = await _post(app, "/grade-preview", CORPO)
    assert r.status_code == 502
    assert r.json()["error"] == "calendario_unavailable"


@pytest.mark.asyncio
async def test_calendario_corrompido_sem_legado_tambem_e_502(ambiente):
    app, _ = _montar(
        ambiente,
        _exercise(com_prazo_velho=False),
        {calendario_url(BASE, "IA-2026-01"): "turma: [\n"},
    )
    r = await _post(app, "/grade-preview", CORPO)
    assert r.status_code == 502


# ---------- o que chega na planilha -----------------------------------------


@pytest.mark.asyncio
async def test_coluna_turma_da_planilha_vem_da_janela_que_casou(ambiente):
    """Aluno das duas disciplinas: a turma gravada é a que de fato lista o
    exercício, não a primeira da coluna `turma` do roster."""
    ambiente.setattr(
        auth_module,
        "get_roster",
        lambda: {
            EMAIL: RosterEntry(
                email=EMAIL, nome="Aluno",
                turma="TD-2026-01;IA-2026-01", github_username=GH,
            )
        },
    )
    app, sheets = _montar(ambiente, _exercise(com_prazo_velho=False), COM_CAL)
    r = await _post(app, "/submissions", {**CORPO, "submission_uuid": "s1"})
    assert r.status_code == 200, r.json()
    assert sheets.linhas[0].turma == "IA-2026-01"
    assert sheets.linhas[0].late is False
