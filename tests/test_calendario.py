"""Calendário da turma: schema, herança do padrão e resolução aluno→janela.

O bug que originou o módulo (107 dias de atraso espúrio em `ia-1.1`) não foi um
erro de código: foi o schema obrigar a sobrescrever as datas da turma anterior
para reaproveitar o exercício. Os testes aqui travam a propriedade que impede
a repetição — **as datas moram na turma, não no exercício** — e as recusas que
protegem o aluno de erro nosso.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app import roster as roster_module
from app.calendario import (
    CalendarioValidationError,
    calendario_url,
    fetch_calendario,
    parse_calendario,
    resolve_janela,
)

BASE = "https://exemplo/ia/exercicios"

CAL_IA = """
turma: IA-2026-01
padrao:
  abre:  2026-09-01T00:00:00-03:00
  fecha: 2026-10-20T23:59:59-03:00
exercicios:
  ia-1.1:
  ia-3.1:
    fecha: 2026-10-27T23:59:59-03:00
"""


@pytest.fixture(autouse=True)
def _cache_limpo():
    roster_module._clear_cache()
    yield
    roster_module._clear_cache()


def _fetcher(mapa: dict[str, str]):
    def buscar(url: str) -> str | None:
        return mapa.get(url)

    return buscar


# ---------- parse ------------------------------------------------------------


def test_exercicio_sem_override_herda_o_padrao_da_turma():
    """A linha vazia é a forma canônica: listar já é matricular.

    É isso que faz "abrir turma nova" custar um arquivo em vez de dez edições.
    """
    cal = parse_calendario(CAL_IA)
    janela = cal.janela("ia-1.1")
    assert janela is not None
    assert janela.turma == "IA-2026-01"
    assert janela.fecha == datetime.fromisoformat("2026-10-20T23:59:59-03:00")
    assert janela.abre == datetime.fromisoformat("2026-09-01T00:00:00-03:00")


def test_override_de_exercicio_vence_o_padrao_so_no_campo_declarado():
    cal = parse_calendario(CAL_IA)
    janela = cal.janela("ia-3.1")
    assert janela.fecha == datetime.fromisoformat("2026-10-27T23:59:59-03:00")
    # `abre` não foi sobrescrito: continua o do padrão.
    assert janela.abre == datetime.fromisoformat("2026-09-01T00:00:00-03:00")


def test_exercicio_fora_do_calendario_nao_tem_janela():
    """Não listado = não matriculado. Sem esta propriedade o calendário não
    poderia substituir `turmas:` como fonte de matrícula."""
    assert parse_calendario(CAL_IA).janela("ia-9.9") is None


def test_fecha_ausente_e_legitimo_e_significa_sem_prazo_recomendado():
    cal = parse_calendario(
        "turma: X\npadrao:\n  abre: 2026-09-01T00:00:00-03:00\nexercicios:\n  a:\n"
    )
    assert cal.janela("a").fecha is None


def test_abre_ausente_derruba_o_arquivo_em_vez_de_adivinhar():
    """`abre` bloqueia submissão. Assumir "abre sempre" seria inventar a
    intenção do professor num campo que tranca aluno — melhor recusar alto."""
    with pytest.raises(CalendarioValidationError, match="sem `abre`"):
        parse_calendario("turma: X\nexercicios:\n  a:\n")


def test_exercicios_vazio_e_recusado():
    with pytest.raises(CalendarioValidationError, match="exercicios"):
        parse_calendario(
            "turma: X\npadrao:\n  abre: 2026-09-01T00:00:00-03:00\nexercicios: {}\n"
        )


def test_turma_ausente_e_recusada():
    with pytest.raises(CalendarioValidationError, match="turma"):
        parse_calendario("exercicios:\n  a:\n")


def test_data_invalida_aponta_o_campo():
    with pytest.raises(CalendarioValidationError, match="padrao.abre"):
        parse_calendario("turma: X\npadrao:\n  abre: ontem\nexercicios:\n  a:\n")


def test_yaml_malformado_vira_erro_de_dominio_nao_yamlerror():
    with pytest.raises(CalendarioValidationError):
        parse_calendario("turma: [\n")


# ---------- fetch / resolve --------------------------------------------------


def test_url_do_calendario_e_derivada_da_base_do_curso():
    assert (
        calendario_url(BASE, "IA-2026-01")
        == "https://exemplo/ia/exercicios/turmas/IA-2026-01.yaml"
    )


def test_turma_sem_calendario_naquele_curso_devolve_none_nao_erro():
    """O aluno de TD não tem calendário no repo de IA. Isso é ausência
    legítima (404), não falha — e precisa ser distinguível dela."""
    assert fetch_calendario(BASE, "TD-2026-01", _fetcher({})) is None


def test_resolve_percorre_as_turmas_do_aluno_e_para_na_primeira_que_casa():
    mapa = {calendario_url(BASE, "IA-2026-01"): CAL_IA}
    janela = resolve_janela(
        ["TD-2026-01", "IA-2026-01"], "ia-1.1", BASE, _fetcher(mapa)
    )
    assert janela is not None and janela.turma == "IA-2026-01"


def test_resolve_devolve_none_quando_nenhuma_turma_lista_o_exercicio():
    mapa = {calendario_url(BASE, "IA-2026-01"): CAL_IA}
    assert resolve_janela(["IA-2026-01"], "ia-9.9", BASE, _fetcher(mapa)) is None


def test_segunda_leitura_da_mesma_turma_sai_do_cache():
    """TTL de 300s é o que torna aceitável consultar o calendário a cada
    submissão: uma turma inteira submetendo vira um GET, não duzentos."""
    chamadas: list[str] = []

    def contando(url: str) -> str | None:
        chamadas.append(url)
        return CAL_IA

    fetch_calendario(BASE, "IA-2026-01", contando)
    fetch_calendario(BASE, "IA-2026-01", contando)
    assert len(chamadas) == 1


def test_falha_de_rede_propaga_em_vez_de_virar_turma_sem_calendario():
    """Silenciar aqui transformaria GitHub fora do ar em `turma_not_eligible`
    na cara do aluno. Quem chama precisa poder distinguir e devolver 502."""

    def explode(url: str) -> str | None:
        raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        resolve_janela(["IA-2026-01"], "ia-1.1", BASE, explode)


def test_datas_de_turmas_diferentes_convivem_sem_se_sobrescrever():
    """A regressão original, travada de frente: o mesmo exercício em duas
    turmas, com dois prazos, sem nenhum arquivo em comum para editar."""
    cal_2 = CAL_IA.replace("IA-2026-01", "IA-2026-02").replace(
        "2026-10-20T23:59:59-03:00", "2027-03-15T23:59:59-03:00"
    )
    mapa = {
        calendario_url(BASE, "IA-2026-01"): CAL_IA,
        calendario_url(BASE, "IA-2026-02"): cal_2,
    }
    f = _fetcher(mapa)
    j1 = resolve_janela(["IA-2026-01"], "ia-1.1", BASE, f)
    j2 = resolve_janela(["IA-2026-02"], "ia-1.1", BASE, f)
    assert j1.fecha.year == 2026 and j2.fecha.year == 2027


def test_aware_e_naive_nao_se_misturam_silenciosamente():
    """YAML sem offset vira datetime naive; comparar com `now` aware explode.
    O parser preserva o que veio — quem compara é que normaliza (endpoints)."""
    cal = parse_calendario(
        "turma: X\npadrao:\n  abre: 2026-09-01T00:00:00\nexercicios:\n  a:\n"
    )
    assert cal.janela("a").abre.tzinfo is None
