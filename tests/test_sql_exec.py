"""Unit tests pro sql_exec — execução read-only + comparação com gabarito."""

from __future__ import annotations

from app.sql_exec import SqlExecError, compare_rows, evaluate, strip_sql_fences

SCHEMA = "CREATE TABLE contratos (id INTEGER, fornecedor TEXT, valor REAL);"
SEED = (
    "INSERT INTO contratos VALUES "
    "(1,'Alpha',100),(2,'Alpha',300),(3,'Beta',50),(4,'Gamma',200);"
)
# Alpha total=400 (maior); média Alpha = 200. Beta=50, Gamma=200.
REF_AVG_TOP = (
    "SELECT AVG(valor) FROM contratos WHERE fornecedor = "
    "(SELECT fornecedor FROM contratos GROUP BY fornecedor "
    "ORDER BY SUM(valor) DESC LIMIT 1)"
)


def test_match_when_student_sql_gives_same_scalar():
    # query do aluno escrita de forma diferente, mesmo resultado (200.0)
    student = (
        "SELECT AVG(valor) FROM contratos "
        "WHERE fornecedor = 'Alpha'"
    )
    result = evaluate(SCHEMA, SEED, REF_AVG_TOP, student)
    assert result.matched is True
    assert result.expected_rows == [(200.0,)]


def test_no_match_on_wrong_result():
    student = "SELECT SUM(valor) FROM contratos"  # 650, não 200
    result = evaluate(SCHEMA, SEED, REF_AVG_TOP, student)
    assert result.matched is False
    assert result.error is None  # rodou, só não bateu


def test_anti_gaming_constant_select_rejected():
    # aluno tenta hardcodar o gabarito no prompt → LLM produz SELECT constante
    result = evaluate(SCHEMA, SEED, REF_AVG_TOP, "SELECT 200.0")
    assert result.matched is False
    assert result.error == "no_table_referenced"


def test_readonly_blocks_write():
    ref = "SELECT COUNT(*) FROM contratos"
    result = evaluate(SCHEMA, SEED, ref, "DELETE FROM contratos")
    assert result.matched is False
    assert result.error is not None


def test_readonly_blocks_drop():
    ref = "SELECT COUNT(*) FROM contratos"
    result = evaluate(SCHEMA, SEED, ref, "DROP TABLE contratos")
    assert result.matched is False
    assert result.error is not None


def test_invalid_sql_is_student_failure_not_raise():
    ref = "SELECT COUNT(*) FROM contratos"
    result = evaluate(SCHEMA, SEED, ref, "SELECT naoexiste FROM contratos")
    assert result.matched is False
    assert result.error is not None
    assert result.actual_rows is None


def test_order_insensitive_by_default():
    ref = "SELECT fornecedor FROM contratos ORDER BY id"
    student = "SELECT fornecedor FROM contratos ORDER BY fornecedor DESC"
    # mesmas linhas, ordem diferente → bate por padrão
    assert evaluate(SCHEMA, SEED, ref, student).matched is True


def test_ordered_true_enforces_order():
    ref = "SELECT fornecedor FROM contratos ORDER BY valor ASC"
    student = "SELECT fornecedor FROM contratos ORDER BY valor DESC"
    assert evaluate(SCHEMA, SEED, ref, student, ordered=True).matched is False


def test_strip_sql_fences():
    assert strip_sql_fences("```sql\nSELECT 1\n```") == "SELECT 1"
    assert strip_sql_fences("```\nSELECT 1\n```") == "SELECT 1"
    assert strip_sql_fences("SELECT 1;") == "SELECT 1"
    assert strip_sql_fences("  SELECT 1  ") == "SELECT 1"


def test_multi_statement_rejected():
    ref = "SELECT COUNT(*) FROM contratos"
    student = "SELECT 1; SELECT 2"
    result = evaluate(SCHEMA, SEED, ref, student)
    assert result.matched is False
    assert result.error is not None


def test_compare_rows_float_tolerance():
    assert compare_rows([(200.0,)], [(200.0000001,)], ordered=False) is True
    assert compare_rows([(1,)], [(1.0,)], ordered=False) is True


def test_fences_with_real_query_still_runs():
    student = "```sql\nSELECT AVG(valor) FROM contratos WHERE fornecedor='Alpha'\n```"
    assert evaluate(SCHEMA, SEED, REF_AVG_TOP, student).matched is True


def test_sqlexecerror_is_exported():
    assert issubclass(SqlExecError, Exception)
