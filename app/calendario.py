"""Calendário da turma: quando cada exercício abre e quando vence.

Por que este módulo existe
--------------------------
No schema original o exercício carregava o próprio calendário::

    turmas: [IA-2026-01]              # lista
    disponivel_a_partir_de: ...       # um só
    prazo: {recomendado_ate: ...}     # um só

`turmas` é plural, mas as datas são únicas. Reaproveitar um exercício numa
turma nova obrigava a SOBRESCREVER as datas da turma anterior — o YAML não
tinha onde guardar as duas. Na prática isso virou dez arquivos para editar a
cada semestre, e em setembro de 2026 a conta chegou: as datas das aulas 3, 4 e
5 foram atualizadas à mão e as da aula 1 ficaram no calendário do semestre
anterior. Toda submissão de `ia-1.1` foi gravada com 107 dias de atraso, e
ninguém viu até um aluno escrever.

A inversão que este módulo faz: **o calendário da turma é a fonte da matrícula
E do cronograma; o YAML do exercício volta a ser só conteúdo pedagógico.**
Abrir turma nova passa a ser um arquivo; reaproveitar um exercício, uma linha.
O calendário da turma que já terminou fica imutável, porque ninguém precisa
tocá-lo para abrir a seguinte.

Formato (`<base do curso>/turmas/<TURMA>.yaml`)::

    turma: IA-2026-01
    padrao:
      abre:  2026-09-01T00:00:00-03:00
      fecha: 2026-10-20T23:59:59-03:00
    exercicios:
      ia-1.1:                              # herda o padrão
      ia-3.1:
        fecha: 2026-10-27T23:59:59-03:00   # exceção explícita

`fecha` é o prazo recomendado: passar dele marca `late`, não bloqueia. `abre`
bloqueia — antes dela o exercício responde `exercise_not_open_yet`.

Estar listado aqui É a matrícula: se o exercício não aparece no calendário de
nenhuma turma do aluno, ele não é elegível. Por isso ``exercicios:`` aceita
entrada vazia — listar já diz tudo, e o `{}` seria ruído.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

import yaml

from app.roster import get_or_fetch

CALENDARIO_TTL_SECONDS = 300


class CalendarioValidationError(Exception):
    """O arquivo do calendário existe mas viola o schema."""


# Falha de rede ao buscar o calendário NÃO tem exceção própria: o erro do
# `fetcher` sobe como veio. A distinção que importa é outra — "esta turma não
# tem calendário neste curso" é ausência legítima e vira ``None``, enquanto
# qualquer erro levantado significa "não sei", e quem chama precisa poder não
# punir o aluno por indisponibilidade nossa.


@dataclass(frozen=True)
class Janela:
    turma: str
    exercicio: str
    abre: datetime
    fecha: datetime | None


@dataclass(frozen=True)
class Calendario:
    turma: str
    janelas: Mapping[str, Janela]

    def janela(self, exercicio_id: str) -> Janela | None:
        return self.janelas.get(exercicio_id)


def _parse_datetime(val: Any, onde: str) -> datetime:
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val)
        except ValueError as exc:
            raise CalendarioValidationError(f"{onde}: data invalida ({exc})") from exc
    raise CalendarioValidationError(
        f"{onde}: esperado datetime ou string ISO, recebi {type(val).__name__}"
    )


def parse_calendario(yaml_text: str) -> Calendario:
    if not yaml_text or not yaml_text.strip():
        raise CalendarioValidationError("calendario vazio")
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise CalendarioValidationError(f"YAML malformado: {exc}") from exc
    if not isinstance(data, dict):
        raise CalendarioValidationError("root do calendario precisa ser mapping")

    turma = data.get("turma")
    if not isinstance(turma, str) or not turma.strip():
        raise CalendarioValidationError("`turma` obrigatoria e precisa ser string")
    turma = turma.strip()

    padrao = data.get("padrao") or {}
    if not isinstance(padrao, dict):
        raise CalendarioValidationError("`padrao` precisa ser mapping")
    abre_padrao = (
        _parse_datetime(padrao["abre"], "padrao.abre") if "abre" in padrao else None
    )
    fecha_padrao = (
        _parse_datetime(padrao["fecha"], "padrao.fecha") if "fecha" in padrao else None
    )

    exercicios = data.get("exercicios")
    if not isinstance(exercicios, dict) or not exercicios:
        raise CalendarioValidationError(
            "`exercicios` precisa ser mapping nao-vazio (listar o exercicio "
            "e o que matricula a turma nele)"
        )

    janelas: dict[str, Janela] = {}
    for eid, bruto in exercicios.items():
        if not isinstance(eid, str) or not eid.strip():
            raise CalendarioValidationError(f"id de exercicio invalido: {eid!r}")
        eid = eid.strip()
        override = bruto or {}
        if not isinstance(override, dict):
            raise CalendarioValidationError(
                f"exercicios.{eid}: precisa ser mapping ou vazio, recebi "
                f"{type(override).__name__}"
            )
        abre = (
            _parse_datetime(override["abre"], f"exercicios.{eid}.abre")
            if "abre" in override
            else abre_padrao
        )
        fecha = (
            _parse_datetime(override["fecha"], f"exercicios.{eid}.fecha")
            if "fecha" in override
            else fecha_padrao
        )
        # `abre` sem default e sem override deixaria o exercicio sem janela de
        # abertura — tratar como "abre sempre" seria adivinhar a intencao do
        # professor num campo que bloqueia aluno. Melhor recusar o arquivo.
        if abre is None:
            raise CalendarioValidationError(
                f"exercicios.{eid}: sem `abre` e sem `padrao.abre`"
            )
        janelas[eid] = Janela(turma=turma, exercicio=eid, abre=abre, fecha=fecha)

    return Calendario(turma=turma, janelas=janelas)


def calendario_url(base_url: str, turma: str) -> str:
    return f"{base_url.rstrip('/')}/turmas/{turma}.yaml"


def fetch_calendario(
    base_url: str,
    turma: str,
    fetcher: Callable[[str], str | None],
) -> Calendario | None:
    """Calendário da turma nesse curso, ou ``None`` se a turma não tem um.

    ``fetcher`` devolve ``None`` quando o arquivo não existe (404) e levanta
    para qualquer outra falha — a distinção é o ponto: "turma sem calendário
    neste curso" é resposta legítima (o aluno de TD não tem calendário no repo
    de IA), enquanto rede fora não pode virar `turma_not_eligible`.
    """

    def _buscar(url: str) -> Calendario | None:
        texto = fetcher(url)
        if texto is None:
            return None
        return parse_calendario(texto)

    return get_or_fetch(
        calendario_url(base_url, turma), _buscar, ttl_seconds=CALENDARIO_TTL_SECONDS
    )


def resolve_janela(
    turmas_do_aluno: tuple[str, ...] | list[str],
    exercicio_id: str,
    base_url: str,
    fetcher: Callable[[str], str | None],
) -> Janela | None:
    """Primeira janela que casa o aluno com o exercício, ou ``None``.

    Percorre as turmas na ordem em que aparecem no roster, então quem cursa
    duas disciplinas tem a primeira como desempate — mesma regra que
    `split_turmas` já usava para a coluna `turma` da Submissions Sheet.
    """
    for turma in turmas_do_aluno:
        calendario = fetch_calendario(base_url, turma, fetcher)
        if calendario is None:
            continue
        janela = calendario.janela(exercicio_id)
        if janela is not None:
            return janela
    return None
