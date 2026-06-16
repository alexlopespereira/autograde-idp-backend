"""Execução de SQL de aluno contra SQLite efêmero + comparação com gabarito.

Usado pelo grading de perguntas ``tipo: sql`` (exercício 5.x). O aluno submete
um PROMPT em linguagem natural; o Gemini gera um ``SELECT`` (ver
``gemini.generate_sql``); este módulo:

  1. semeia um SQLite ``:memory:`` com o ``schema`` + ``seed`` do exercício;
  2. roda a ``query_referencia`` do professor (confiável) → linhas-gabarito;
  3. roda o SELECT do aluno de forma READ-ONLY → linhas obtidas;
  4. compara os result-sets (order-insensitive por padrão).

Decisões de segurança (defense-in-depth, banco já é descartável por chamada):
- ``sqlite3`` da stdlib → zero dependência nova (supply-chain).
- ``set_authorizer`` nega tudo que não for leitura: um ``DROP``/``UPDATE`` do
  aluno é recusado antes de executar (mesmo num banco efêmero).
- ``Cursor.execute`` já recusa múltiplos statements (1 SELECT por chamada).
- ``set_progress_handler`` aborta query que estoure o teto de opcodes (runaway).

Anti-gaming: o SELECT do aluno PRECISA ler ao menos uma tabela real. Um
``SELECT 200`` literal (aluno hardcodando o gabarito no prompt) não lê tabela
nenhuma → reprovado, mesmo que o número bata.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# Teto de opcodes da VM do SQLite por query do aluno. O dataset é minúsculo
# (~dezenas de linhas), então qualquer query honesta termina em poucos milhares
# de passos; o teto só existe pra matar loop patológico (ex: CTE recursiva).
MAX_VM_STEPS = 2_000_000

# Casas decimais ao normalizar números na comparação. AVG pode divergir no
# último bit entre a query do aluno e a de referência; arredondar elimina ruído
# de ponto flutuante sem mascarar erro real.
FLOAT_NDIGITS = 6


class SqlExecError(Exception):
    """SELECT do aluno falhou ao executar (sintaxe, coluna inexistente, ação negada)."""


@dataclass(frozen=True)
class SqlEval:
    """Resultado da avaliação de um SELECT do aluno contra o gabarito."""

    matched: bool
    reason: str  # mensagem pt-BR endereçada ao aluno
    expected_rows: list[tuple]
    actual_rows: list[tuple] | None  # None se o SELECT falhou ao executar
    error: str | None = None  # mensagem do erro de execução, se houver


def strip_sql_fences(text: str) -> str:
    """Remove cercas markdown (```sql ... ```) e ruído ao redor do SQL.

    O LLM às vezes embrulha a query em bloco de código apesar do pedido de
    "só o SQL". Mantém o conteúdo interno intacto.
    """
    s = (text or "").strip()
    if s.startswith("```"):
        # remove a primeira linha de cerca (```sql / ```) e a cerca final
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s.strip().rstrip(";").strip()


def _seed_db(schema: str, seed: str) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(schema)
    if seed and seed.strip():
        con.executescript(seed)
    return con


def _real_tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def _run_readonly(con: sqlite3.Connection, sql: str) -> tuple[list[tuple], set[str]]:
    """Executa ``sql`` em modo leitura-apenas. Retorna (linhas, tabelas_lidas).

    Levanta ``SqlExecError`` se a query falhar, for negada pelo authorizer, ou
    tiver mais de um statement.
    """
    tables_read: set[str] = set()

    def authorizer(action, arg1, arg2, db_name, trigger):  # noqa: ANN001
        if action == sqlite3.SQLITE_READ:
            if arg1:
                tables_read.add(arg1)
            return sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
            return sqlite3.SQLITE_OK
        # qualquer escrita/DDL/pragma/attach/transação → negado
        return sqlite3.SQLITE_DENY

    steps = [0]

    def progress() -> int:
        steps[0] += 1
        return 1 if steps[0] > (MAX_VM_STEPS // 1000) else 0

    con.set_authorizer(authorizer)
    con.set_progress_handler(progress, 1000)
    try:
        cur = con.execute(sql)
        rows = cur.fetchall()
    except (sqlite3.Error, sqlite3.Warning) as exc:
        raise SqlExecError(str(exc)) from exc
    finally:
        con.set_authorizer(None)
        con.set_progress_handler(None, 1000)
    return rows, tables_read


def _normalize_cell(value: object) -> object:
    if isinstance(value, bool):  # antes de int — bool é subclasse de int
        return int(value)
    if isinstance(value, (int, float)):
        return round(float(value), FLOAT_NDIGITS)
    return value


def _normalize_rows(rows: list[tuple]) -> list[tuple]:
    return [tuple(_normalize_cell(c) for c in row) for row in rows]


def compare_rows(expected: list[tuple], actual: list[tuple], *, ordered: bool) -> bool:
    exp = _normalize_rows(expected)
    act = _normalize_rows(actual)
    if ordered:
        return exp == act
    # order-insensitive: multiset de linhas (mesmas linhas, qualquer ordem)
    from collections import Counter

    return Counter(exp) == Counter(act)


def evaluate(
    schema: str,
    seed: str,
    reference_query: str,
    student_sql: str,
    *,
    ordered: bool = False,
) -> SqlEval:
    """Avalia o SELECT do aluno contra a query de referência na mesma base.

    Nunca levanta por culpa do aluno: SQL inválido/negado vira ``matched=False``
    com ``error`` preenchido. Levanta apenas se o EXERCÍCIO estiver mal
    configurado (schema/seed/reference_query inválidos) — isso é bug do professor.
    """
    con = _seed_db(schema, seed)
    try:
        expected = con.execute(reference_query).fetchall()
        real = _real_tables(con)

        student_sql = strip_sql_fences(student_sql)
        if not student_sql:
            return SqlEval(
                matched=False,
                reason="o LLM não produziu nenhuma consulta a partir do seu prompt.",
                expected_rows=expected,
                actual_rows=None,
                error="empty_sql",
            )
        try:
            actual, tables_read = _run_readonly(con, student_sql)
        except SqlExecError as exc:
            return SqlEval(
                matched=False,
                reason="o SQL gerado a partir do seu prompt não pôde ser executado.",
                expected_rows=expected,
                actual_rows=None,
                error=str(exc),
            )

        if not (tables_read & real):
            return SqlEval(
                matched=False,
                reason=(
                    "o SQL gerado não consultou nenhuma tabela da base — descreva "
                    "no prompt O QUE calcular sobre os dados, não o resultado."
                ),
                expected_rows=expected,
                actual_rows=actual,
                error="no_table_referenced",
            )

        if compare_rows(expected, actual, ordered=ordered):
            return SqlEval(
                matched=True,
                reason="resultado confere com o gabarito.",
                expected_rows=expected,
                actual_rows=actual,
            )
        return SqlEval(
            matched=False,
            reason="o resultado da consulta não bateu com o gabarito.",
            expected_rows=expected,
            actual_rows=actual,
        )
    finally:
        con.close()
