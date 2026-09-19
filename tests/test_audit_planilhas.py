"""Testes do `scripts/audit_planilhas.py`.

Um audit que para de acusar em silêncio é pior que audit nenhum: some o sinal
E a percepção de que falta sinal. Por isso os dois incidentes de setembro/2026
viram caso de teste aqui — cada um na forma exata em que apareceu na planilha.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_CAMINHO = (
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "audit_planilhas.py"
)
_spec = importlib.util.spec_from_file_location("audit_planilhas", _CAMINHO)
assert _spec and _spec.loader
audit = importlib.util.module_from_spec(_spec)
sys.modules["audit_planilhas"] = audit
_spec.loader.exec_module(audit)


BASES = {"ia": "https://raw.example/ia/exercicios", "td": "https://raw.example/td/exercicios"}

ROSTER_OK = (
    "email,nome,turma,github_username\n"
    "ana@idp.edu.br,Ana,IA-2026-01,ana\n"
    "beto@idp.edu.br,Beto,TD-2026-01,beto\n"
)

CAL_IA = (
    "turma: IA-2026-01\n"
    "padrao:\n"
    "  abre: 2026-05-01T00:00:00-03:00\n"
    "  fecha: 2099-10-20T23:59:59-03:00\n"
    "exercicios:\n"
    "  ia-1.1:\n"
)
CAL_TD = (
    "turma: TD-2026-01\n"
    "padrao:\n"
    "  abre: 2026-05-01T00:00:00-03:00\n"
    "  fecha: 2099-10-20T23:59:59-03:00\n"
    "exercicios:\n"
    '  "1.1":\n'
)


def _fake_http(mapa: dict[str, str], *, roster: str = ROSTER_OK):
    """`http_get` de mentira. Qualquer URL fora do mapa responde 404.

    404 é a ausência legítima que o script tem que tolerar (turma de TD não tem
    calendário no repo de IA), então é o default certo.
    """

    def _get(url: str) -> tuple[int, str]:
        if url.startswith("roster://"):
            return 200, roster
        if url in mapa:
            return 200, mapa[url]
        return 404, ""

    return _get


def _mapa_saudavel() -> dict[str, str]:
    return {
        f"{BASES['ia']}/turmas/IA-2026-01.yaml": CAL_IA,
        f"{BASES['ia']}/ia-1.1.yaml": "exercicio: ia-1.1\n",
        f"{BASES['td']}/turmas/TD-2026-01.yaml": CAL_TD,
        f"{BASES['td']}/1.1.yaml": "exercicio: '1.1'\n",
    }


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ROSTER_URL", "roster://planilha")
    monkeypatch.setenv("EXERCISES_BASE_URL", BASES["td"])
    monkeypatch.setenv("EXERCISES_BASE_URL_IA", BASES["ia"])
    return monkeypatch


def _rodar(monkeypatch, mapa, roster=ROSTER_OK, alvo="tudo") -> tuple[int, audit.Achados]:
    monkeypatch.setattr(audit, "http_get", _fake_http(mapa, roster=roster))
    capturado: list[audit.Achados] = []
    original = audit.Achados

    class Espiao(original):  # type: ignore[misc,valid-type]
        def __init__(self) -> None:
            super().__init__()
            capturado.append(self)

    monkeypatch.setattr(audit, "Achados", Espiao)
    codigo = audit.main(["audit_planilhas.py", alvo])
    return codigo, capturado[0]


def test_planilhas_saudaveis_saem_limpas(env):
    codigo, a = _rodar(env, _mapa_saudavel())
    assert a.fatais == []
    assert codigo == 0


def test_bases_por_curso_le_do_ambiente(env):
    """Curso novo entra no audit por env var, sem editar o script."""
    env.setenv("EXERCISES_BASE_URL_MBA", "https://raw.example/mba/exercicios")
    bases = audit.bases_por_curso()
    assert bases["td"] == BASES["td"]
    assert bases["ia"] == BASES["ia"]
    assert bases["mba"] == "https://raw.example/mba/exercicios"


# --- incidente 1: autofill do Sheets na coluna `turma` ---------------------
# 2026-09-16, 13h de exposição. Um arraste incrementou o número final da turma
# linha a linha: `TD-2026-02;IA-2026-02`, `;IA-2026-03`, ... `;IA-2026-30`.
# Nenhum aluno de TD tentou submeter nessas 13h — o dano foi zero por sorte.


def test_autofill_na_coluna_turma_e_fatal(env):
    linhas = ["email,nome,turma,github_username"]
    for i in range(2, 8):
        linhas.append(f"aluno{i}@idp.edu.br,Aluno {i},TD-2026-02;IA-2026-{i:02d},gh{i}")
    roster = "\n".join(linhas) + "\n"

    codigo, a = _rodar(env, _mapa_saudavel(), roster=roster)

    assert codigo == 1
    assert any("sem NENHUMA turma com calendario" in m for m in a.fatais)
    assert any("sem calendario em curso" in m for m in a.avisos)


def test_uma_turma_boa_salva_o_aluno_do_fatal(env):
    """Aluno com uma turma real E uma inventada continua conseguindo fazer
    exercício — é aviso, não fatal. Sem esta distinção o audit gritaria
    `FATAL` em toda turma futura ainda sem YAML publicado."""
    roster = (
        "email,nome,turma,github_username\n"
        "ana@idp.edu.br,Ana,IA-2026-01;IA-2026-99,ana\n"
    )
    codigo, a = _rodar(env, _mapa_saudavel(), roster=roster)
    assert a.fatais == []
    assert any("IA-2026-99" in m for m in a.avisos)
    assert codigo == 0


# --- incidente 2: conta Google divergindo da planilha ----------------------


def test_conta_duplicada_entre_linhas_e_fatal(env):
    """`parse_roster` é all-or-nothing: recusado = 502 para a turma TODA."""
    roster = (
        "email,nome,turma,github_username\n"
        "ana@idp.edu.br,Ana,IA-2026-01,ana\n"
        "ANA@IDP.EDU.BR,Ana de novo,TD-2026-01,ana2\n"
    )
    codigo, a = _rodar(env, _mapa_saudavel(), roster=roster)
    assert codigo == 1
    assert any("RECUSA o CSV" in m for m in a.fatais)


def test_aluno_com_duas_contas_resolve_as_duas(env):
    """A linha que consertou o incidente: institucional + pessoal na mesma
    célula, as duas resolvendo, e o aluno contado UMA vez."""
    roster = (
        "email,nome,turma,github_username\n"
        "ricardo.c.costa@caixa.gov.br;rick.palmeiras@uol.com.br,Ricardo,IA-2026-01,rick\n"
    )
    codigo, a = _rodar(env, _mapa_saudavel(), roster=roster)
    assert a.fatais == []
    assert codigo == 0


# --- calendários ----------------------------------------------------------


def test_calendario_que_nao_parseia_e_fatal(env):
    """Id numérico de TD sem aspas: `1.1:` é lido como o float 1.1 e o
    arquivo inteiro é recusado — e isso chega ao aluno como 502, não como
    erro de arquivo."""
    mapa = _mapa_saudavel()
    mapa[f"{BASES['td']}/turmas/TD-2026-01.yaml"] = (
        "turma: TD-2026-01\n"
        "padrao:\n"
        "  abre: 2026-05-01T00:00:00-03:00\n"
        "exercicios:\n"
        "  1.1:\n"  # sem aspas
    )
    codigo, a = _rodar(env, mapa)
    assert codigo == 1
    assert any("NAO parseia" in m for m in a.fatais)


def test_exercicio_listado_sem_yaml_na_main_e_fatal(env):
    """Listar matricula a turma: exercício listado que responde 404 entrega ao
    aluno `exercise_not_found` num id que o calendário afirma ser dele."""
    mapa = _mapa_saudavel()
    del mapa[f"{BASES['ia']}/ia-1.1.yaml"]
    codigo, a = _rodar(env, mapa)
    assert codigo == 1
    assert any("responde HTTP 404" in m for m in a.fatais)


def test_campo_turma_divergindo_do_nome_do_arquivo_e_fatal(env):
    mapa = _mapa_saudavel()
    mapa[f"{BASES['ia']}/turmas/IA-2026-01.yaml"] = CAL_IA.replace(
        "turma: IA-2026-01", "turma: IA-2026-02"
    )
    codigo, a = _rodar(env, mapa)
    assert codigo == 1
    assert any("mas o arquivo se chama" in m for m in a.fatais)


def test_prazo_vencido_e_aviso_nao_fatal(env):
    """Prazo vencido é estado legítimo depois da aula. Continua reportado
    porque prazo do semestre ANTERIOR marcou toda a aula 1 de IA com 107 dias
    de atraso sem disparar nada."""
    mapa = _mapa_saudavel()
    mapa[f"{BASES['ia']}/turmas/IA-2026-01.yaml"] = CAL_IA.replace("2099", "2020")
    codigo, a = _rodar(env, mapa)
    assert a.fatais == []
    assert any("prazo venceu" in m for m in a.avisos)
    assert codigo == 0
