"""evidence.artifacts.* primitives.

Consome ``evidence['artifacts']`` — a lista (raw, sem validador) entregue pelo
endpoint a partir do ``GradeRequestBody.artifacts_evidence``. Cada entry é um
dict produzido por ``autograde_idp.evidence.artifacts.ArtifactResult.to_dict()``
no cliente, com campos: ``role, exists, size_bytes, word_count, sha256,
headings[], links[], content, captured_at`` (+ ``truncated`` opcional).

Decisão deliberada de não criar validador (como ``app/evidence/shell.py``):
nada no payload de artifacts justifica whitelist (não há comandos shell sendo
re-executados; é só texto). Schema validation acontece implicitamente — chave
ausente vira "ausente", primitive reprova com mensagem clara.

Time-window check (captured_at ∈ janela do exercício) é nice-to-have e deve
ser adicionado em iteração seguinte se aparecer abuse pattern.
"""
from __future__ import annotations

import re
from typing import Any

from . import CriterioResult, register


def _peso(args: dict) -> int:
    try:
        return int(args.get("_peso", 0))
    except (TypeError, ValueError):
        return 0


def _artifacts_list(evidence: dict) -> list[dict[str, Any]]:
    raw = evidence.get("artifacts") if isinstance(evidence, dict) else None
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    return []


def _artifact_by_role(evidence: dict, role: str) -> dict[str, Any] | None:
    for entry in _artifacts_list(evidence):
        if entry.get("role") == role:
            return entry
    return None


def _str_arg(args: dict, key: str) -> str:
    val = args.get(key)
    return str(val) if val is not None else ""


def _int_arg(args: dict, key: str, default: int = 0) -> int:
    try:
        return int(args.get(key, default))
    except (TypeError, ValueError):
        return default


@register("evidence.artifacts.exists")
def artifact_exists(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    role = _str_arg(args, "role")
    entry = _artifact_by_role(evidence, role)
    if entry is None:
        return CriterioResult(False, 0, peso, f"artefato com role={role!r} ausente do payload")
    if entry.get("exists") is True:
        path = entry.get("path", "?")
        return CriterioResult(True, peso, peso, f"{path} encontrado")
    path = entry.get("path", "?")
    return CriterioResult(False, 0, peso, f"{path} não existe no repo do aluno")


@register("evidence.artifacts.word_count_min")
def word_count_min(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    role = _str_arg(args, "role")
    min_words = _int_arg(args, "min")
    entry = _artifact_by_role(evidence, role)
    if entry is None or not entry.get("exists"):
        return CriterioResult(False, 0, peso, f"artefato {role!r} ausente")
    wc = _int_arg(entry, "word_count")
    if wc >= min_words:
        return CriterioResult(True, peso, peso, f"{wc} palavras (mínimo {min_words})")
    return CriterioResult(
        False,
        0,
        peso,
        f"{wc} palavras é menos que o mínimo de {min_words} para {role!r}",
    )


@register("evidence.artifacts.distinct_reports")
def distinct_reports(args: dict, evidence: dict) -> CriterioResult:
    """Garante que ≥2 relatórios são distintos (sha256 + primeiros 500 chars).

    args: { roles: [role_a, role_b, ...] }
    Detecta cópia trivial entre A1 e A2 (anti-cola). Não detecta paráfrase.
    """
    peso = _peso(args)
    roles_raw = args.get("roles") or []
    if not isinstance(roles_raw, list) or len(roles_raw) < 2:
        return CriterioResult(False, 0, peso, "args.roles precisa ser lista com ≥2 entradas")
    roles = [str(r) for r in roles_raw]
    entries = [_artifact_by_role(evidence, r) for r in roles]
    missing = [r for r, e in zip(roles, entries) if e is None or not e.get("exists")]
    if missing:
        return CriterioResult(False, 0, peso, f"artefatos ausentes: {missing}")
    shas = [str(e.get("sha256", "")) for e in entries]
    if len(set(shas)) < len(shas):
        return CriterioResult(
            False, 0, peso, f"relatórios idênticos (mesmo sha256): {roles}"
        )
    prefixes = [str(e.get("content", ""))[:500] for e in entries]
    if len(set(prefixes)) < len(prefixes):
        return CriterioResult(
            False,
            0,
            peso,
            f"relatórios começam idênticos (primeiros 500 chars iguais) em {roles}",
        )
    return CriterioResult(True, peso, peso, f"relatórios distintos confirmado para {roles}")


@register("evidence.artifacts.heading_pattern_min")
def heading_pattern_min(args: dict, evidence: dict) -> CriterioResult:
    """Conta headings que casam regex; usado pra ``## v\\d+`` (B6).

    args: { role: str, pattern: str, min: int }
    """
    peso = _peso(args)
    role = _str_arg(args, "role")
    pattern = _str_arg(args, "pattern")
    min_count = _int_arg(args, "min")
    if not pattern:
        return CriterioResult(False, 0, peso, "args.pattern obrigatório")
    try:
        regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        return CriterioResult(False, 0, peso, f"regex inválida: {exc}")
    entry = _artifact_by_role(evidence, role)
    if entry is None or not entry.get("exists"):
        return CriterioResult(False, 0, peso, f"artefato {role!r} ausente")
    headings = entry.get("headings") or []
    if not isinstance(headings, list):
        headings = []
    matches = [h for h in headings if isinstance(h, str) and regex.search(h)]
    n = len(matches)
    if n >= min_count:
        return CriterioResult(
            True, peso, peso, f"{n} headings casam {pattern!r} (mínimo {min_count})"
        )
    return CriterioResult(
        False,
        0,
        peso,
        f"{n} headings casam {pattern!r}; esperado ≥{min_count} em {role!r}",
    )


@register("evidence.artifacts.cross_reference_required")
def cross_reference_required(args: dict, evidence: dict) -> CriterioResult:
    """Garante que termos extraídos de A aparecem em B (consistência cruzada).

    args: { role_a: str, role_b: str, pattern_in_a: str }

    Extrai todos os matches do ``pattern_in_a`` no ``content`` de A; cada match
    deve aparecer no ``content`` de B (substring case-insensitive). Útil pra
    "todo ator no mapa precisa aparecer no transcript".
    """
    peso = _peso(args)
    role_a = _str_arg(args, "role_a")
    role_b = _str_arg(args, "role_b")
    pattern_in_a = _str_arg(args, "pattern_in_a")
    if not pattern_in_a:
        return CriterioResult(False, 0, peso, "args.pattern_in_a obrigatório")
    try:
        regex = re.compile(pattern_in_a, re.IGNORECASE)
    except re.error as exc:
        return CriterioResult(False, 0, peso, f"regex inválida: {exc}")
    entry_a = _artifact_by_role(evidence, role_a)
    entry_b = _artifact_by_role(evidence, role_b)
    if entry_a is None or not entry_a.get("exists"):
        return CriterioResult(False, 0, peso, f"artefato {role_a!r} ausente")
    if entry_b is None or not entry_b.get("exists"):
        return CriterioResult(False, 0, peso, f"artefato {role_b!r} ausente")
    content_a = str(entry_a.get("content", ""))
    content_b_lower = str(entry_b.get("content", "")).lower()
    # Convenção re.findall: usa grupo 1 se a regex tiver capture group,
    # senão o match inteiro. Permite "extrair nomes" via `\|\s*(\w+)\s*\|`.
    def _extract(m: "re.Match[str]") -> str:
        try:
            return m.group(1) if m.lastindex else m.group(0)
        except IndexError:
            return m.group(0)

    matches_a = {
        _extract(m).strip().lower()
        for m in regex.finditer(content_a)
        if _extract(m).strip()
    }
    if not matches_a:
        return CriterioResult(False, 0, peso, f"nenhum match de {pattern_in_a!r} em {role_a!r}")
    missing = sorted(m for m in matches_a if m not in content_b_lower)
    if missing:
        preview = ", ".join(missing[:5])
        more = f" (+{len(missing) - 5})" if len(missing) > 5 else ""
        return CriterioResult(
            False,
            0,
            peso,
            f"{len(missing)} termo(s) de {role_a!r} ausentes em {role_b!r}: {preview}{more}",
        )
    return CriterioResult(
        True,
        peso,
        peso,
        f"{len(matches_a)} termo(s) de {role_a!r} confirmados em {role_b!r}",
    )


# ---------------------------------------------------------------------------
# Checagens genéricas de conteúdo (regex e CSV)
#
# Deliberadamente genéricas: a especificidade do exercício (qual regex, quais
# colunas, qual soma) mora no YAML, não aqui. Ver "conteúdo de exercício mora
# no YAML" no CLAUDE.md.
# ---------------------------------------------------------------------------

_REGEX_FLAGS = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
}


def _compile_arg(args: dict) -> "re.Pattern[str] | str":
    """Compila ``args.pattern`` com ``args.flags`` (ex.: ``flags: im``).

    Devolve string (a mensagem de erro) quando a regex é inválida — o chamador
    reprova o critério em vez de estourar exceção.
    """
    pattern = _str_arg(args, "pattern")
    if not pattern:
        return "args.pattern obrigatório"
    flags = 0
    for ch in _str_arg(args, "flags"):
        flags |= _REGEX_FLAGS.get(ch.lower(), 0)
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        return f"regex inválida: {exc}"


def _content_of(evidence: dict, role: str) -> tuple[str, str]:
    """``(content, erro)`` — ``erro`` não-vazio quando o artefato falta."""
    entry = _artifact_by_role(evidence, role)
    if entry is None or not entry.get("exists"):
        return "", f"artefato {role!r} ausente"
    return str(entry.get("content", "")), ""


@register("evidence.artifacts.content_matches")
def content_matches(args: dict, evidence: dict) -> CriterioResult:
    """Conta ocorrências de ``args.pattern`` no conteúdo do artefato.

    args: ``{role, pattern, min=1, flags="", descricao=""}``

    É o canivete suíço do YAML: "o ralph.sh tem `set -euo pipefail`", "o
    index.html tem um `<canvas>`", "o plugin-list.txt cita `grill-me`". A
    ``descricao`` é o que aparece no boletim — escreva pensando no aluno.
    """
    peso = _peso(args)
    role = _str_arg(args, "role")
    minimo = _int_arg(args, "min", 1)
    descricao = _str_arg(args, "descricao") or f"padrão {_str_arg(args, 'pattern')!r}"
    regex = _compile_arg(args)
    if isinstance(regex, str):
        return CriterioResult(False, 0, peso, regex)
    content, erro = _content_of(evidence, role)
    if erro:
        return CriterioResult(False, 0, peso, erro)
    n = len(regex.findall(content))
    if n >= minimo:
        return CriterioResult(True, peso, peso, f"{descricao}: {n} ocorrência(s)")
    return CriterioResult(
        False, 0, peso, f"{descricao}: {n} ocorrência(s), esperado >= {minimo}"
    )


@register("evidence.artifacts.content_absent")
def content_absent(args: dict, evidence: dict) -> CriterioResult:
    """Reprova se ``args.pattern`` aparecer — o inverso do ``content_matches``.

    args: ``{role, pattern, flags="", descricao=""}``. Uso: garantir que um
    template não foi entregue com os placeholders por preencher.
    """
    peso = _peso(args)
    role = _str_arg(args, "role")
    descricao = _str_arg(args, "descricao") or f"padrão {_str_arg(args, 'pattern')!r}"
    regex = _compile_arg(args)
    if isinstance(regex, str):
        return CriterioResult(False, 0, peso, regex)
    content, erro = _content_of(evidence, role)
    if erro:
        return CriterioResult(False, 0, peso, erro)
    hits = regex.findall(content)
    if not hits:
        return CriterioResult(True, peso, peso, f"{descricao}: ausente, como esperado")
    return CriterioResult(
        False, 0, peso, f"{descricao}: {len(hits)} ocorrência(s) — deveria estar ausente"
    )


@register("evidence.artifacts.line_count_min")
def line_count_min(args: dict, evidence: dict) -> CriterioResult:
    """Conta linhas NÃO vazias do artefato. args: ``{role, min, max=0}``.

    ``max=0`` desliga o teto. Usado para "5–10 linhas de reflexão": o piso é
    duro, o teto é generoso de propósito (ver comentário no YAML).
    """
    peso = _peso(args)
    role = _str_arg(args, "role")
    minimo = _int_arg(args, "min", 1)
    maximo = _int_arg(args, "max", 0)
    content, erro = _content_of(evidence, role)
    if erro:
        return CriterioResult(False, 0, peso, erro)
    linhas = [ln for ln in content.splitlines() if ln.strip()]
    n = len(linhas)
    if n < minimo:
        return CriterioResult(
            False, 0, peso, f"{n} linha(s) não vazia(s), esperado >= {minimo}"
        )
    if maximo and n > maximo:
        return CriterioResult(
            False, 0, peso, f"{n} linha(s) não vazia(s), esperado <= {maximo}"
        )
    return CriterioResult(True, peso, peso, f"{n} linha(s) não vazia(s)")


# --- CSV -------------------------------------------------------------------


def _parse_csv(content: str, delimiter: str) -> list[list[str]]:
    """Lê o CSV do artefato. Tolera BOM e linhas em branco no meio/fim."""
    import csv
    import io

    text = content.lstrip("﻿")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [row for row in reader if any((cell or "").strip() for cell in row)]


def _csv_of(args: dict, evidence: dict) -> tuple[list[list[str]], str]:
    role = _str_arg(args, "role")
    delimiter = _str_arg(args, "delimiter") or ","
    if len(delimiter) != 1:
        return [], f"args.delimiter precisa ter 1 caractere, recebi {delimiter!r}"
    content, erro = _content_of(evidence, role)
    if erro:
        return [], erro
    try:
        rows = _parse_csv(content, delimiter)
    except Exception as exc:  # noqa: BLE001 - csv.Error e afins viram mensagem
        return [], f"CSV ilegível em {role!r}: {exc}"
    if not rows:
        return [], f"CSV {role!r} está vazio"
    return rows, ""


def _norm(cell: str) -> str:
    return (cell or "").strip().strip('"').lower()


@register("evidence.artifacts.csv_columns")
def csv_columns(args: dict, evidence: dict) -> CriterioResult:
    """Confere o cabeçalho do CSV. args: ``{role, columns[], delimiter, exact}``.

    ``exact: true`` exige o cabeçalho idêntico (mesmas colunas, mesma ordem);
    o default exige apenas que as colunas pedidas estejam presentes, o que
    deixa o aluno acrescentar colunas próprias sem quebrar a nota.
    """
    peso = _peso(args)
    esperadas = [str(c) for c in (args.get("columns") or [])]
    if not esperadas:
        return CriterioResult(False, 0, peso, "args.columns obrigatório")
    rows, erro = _csv_of(args, evidence)
    if erro:
        return CriterioResult(False, 0, peso, erro)
    header = [_norm(c) for c in rows[0]]
    alvo = [_norm(c) for c in esperadas]
    if bool(args.get("exact")):
        if header == alvo:
            return CriterioResult(True, peso, peso, f"cabeçalho exato: {rows[0]}")
        return CriterioResult(
            False,
            0,
            peso,
            f"cabeçalho {rows[0]} difere do esperado {esperadas} (a ordem conta)",
        )
    faltando = [orig for orig, n in zip(esperadas, alvo) if n not in header]
    if faltando:
        return CriterioResult(
            False, 0, peso, f"colunas faltando: {faltando} (cabeçalho lido: {rows[0]})"
        )
    return CriterioResult(True, peso, peso, f"{len(esperadas)} coluna(s) presentes")


@register("evidence.artifacts.csv_rows_min")
def csv_rows_min(args: dict, evidence: dict) -> CriterioResult:
    """Conta linhas de dados (exclui o cabeçalho). args: ``{role, min, delimiter}``."""
    peso = _peso(args)
    minimo = _int_arg(args, "min", 1)
    rows, erro = _csv_of(args, evidence)
    if erro:
        return CriterioResult(False, 0, peso, erro)
    n = len(rows) - 1
    if n >= minimo:
        return CriterioResult(True, peso, peso, f"{n} linha(s) de dados (>= {minimo})")
    return CriterioResult(False, 0, peso, f"{n} linha(s) de dados, esperado >= {minimo}")


@register("evidence.artifacts.csv_shape")
def csv_shape(args: dict, evidence: dict) -> CriterioResult:
    """Confere a forma exata do CSV. args: ``{role, rows, cols, delimiter}``.

    ``rows`` = linhas de dados (sem cabeçalho); ``cols`` = campos do cabeçalho.
    Para o pivot região x mês do ia-3.1: ``rows: 4`` e ``cols: 7`` (a coluna
    ``regiao`` mais os 6 meses).
    """
    peso = _peso(args)
    rows_esperado = _int_arg(args, "rows", -1)
    cols_esperado = _int_arg(args, "cols", -1)
    rows, erro = _csv_of(args, evidence)
    if erro:
        return CriterioResult(False, 0, peso, erro)
    n_rows = len(rows) - 1
    n_cols = len(rows[0])
    problemas = []
    if rows_esperado >= 0 and n_rows != rows_esperado:
        problemas.append(f"{n_rows} linhas de dados (esperado {rows_esperado})")
    if cols_esperado >= 0 and n_cols != cols_esperado:
        problemas.append(f"{n_cols} colunas (esperado {cols_esperado})")
    if problemas:
        return CriterioResult(False, 0, peso, "; ".join(problemas))
    return CriterioResult(True, peso, peso, f"forma {n_rows}x{n_cols} confere")


def _to_float(cell: str) -> float | None:
    """Converte célula em float aceitando ``1.234,56`` e ``1234.56``.

    Célula vazia ou não numérica devolve ``None`` e é ignorada na soma — num
    pivot, as colunas de rótulo caem aqui sem virar erro.
    """
    text = (cell or "").strip().replace("R$", "").replace(" ", "")
    if not text:
        return None
    if "," in text:
        # "1.234,56" -> ponto é separador de milhar; "1234,56" -> só a vírgula.
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


@register("evidence.artifacts.csv_sum_equals")
def csv_sum_equals(args: dict, evidence: dict) -> CriterioResult:
    """Soma células numéricas do CSV e compara com ``args.expected``.

    args: ``{role, expected, tolerance=0.05, columns=[], skip_first_col=false,
    delimiter}``

    Sem ``columns``, soma toda célula que parseia como número — o que, num
    pivot, é exatamente o total geral. ``skip_first_col`` protege pivots cuja
    primeira coluna é um rótulo que por acaso parece número.
    """
    peso = _peso(args)
    if "expected" not in args:
        return CriterioResult(False, 0, peso, "args.expected obrigatório")
    try:
        esperado = float(args["expected"])
    except (TypeError, ValueError):
        return CriterioResult(
            False, 0, peso, f"args.expected inválido: {args.get('expected')!r}"
        )
    try:
        tolerancia = float(args.get("tolerance", 0.05))
    except (TypeError, ValueError):
        tolerancia = 0.05
    rows, erro = _csv_of(args, evidence)
    if erro:
        return CriterioResult(False, 0, peso, erro)

    header = [_norm(c) for c in rows[0]]
    alvos = [_norm(str(c)) for c in (args.get("columns") or [])]
    if alvos:
        idxs = [i for i, h in enumerate(header) if h in alvos]
        if not idxs:
            return CriterioResult(
                False, 0, peso, f"nenhuma das colunas {args.get('columns')} no cabeçalho"
            )
    else:
        inicio = 1 if bool(args.get("skip_first_col")) else 0
        idxs = list(range(inicio, len(header)))

    total = 0.0
    lidas = 0
    for row in rows[1:]:
        for i in idxs:
            if i >= len(row):
                continue
            valor = _to_float(row[i])
            if valor is None:
                continue
            total += valor
            lidas += 1
    if lidas == 0:
        return CriterioResult(False, 0, peso, "nenhuma célula numérica encontrada")
    if abs(total - esperado) <= tolerancia:
        return CriterioResult(
            True, peso, peso, f"soma {total:.2f} confere com {esperado:.2f}"
        )
    return CriterioResult(
        False,
        0,
        peso,
        f"soma {total:.2f} difere do esperado {esperado:.2f} "
        f"(tolerância +/-{tolerancia}); {lidas} célula(s) somadas",
    )
