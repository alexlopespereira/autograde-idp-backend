"""evidence.shell.* primitives (US-14).

Consume ``evidence['shell']``, a dict produced by
``app.evidence.shell.validate_shell_evidence``. The validator parses the raw
``shell_evidence`` payload, enforces whitelist + time window, and exposes the
fields these primitives need: ``gh_version``, ``gh_auth_ok``, ``gh_repo_view``.
"""

from __future__ import annotations

import re
from typing import Any

from . import CriterioResult, register

_GH_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")


def _peso(args: dict) -> int:
    try:
        return int(args.get("_peso", 0))
    except (TypeError, ValueError):
        return 0


def _shell_context(evidence: dict) -> dict[str, Any] | None:
    shell = evidence.get("shell") if isinstance(evidence, dict) else None
    if isinstance(shell, dict):
        return shell
    return None


# Sentinel para distinguir "campo ausente" de "campo presente com valor falsy"
# (ex.: ``concluida: false`` é presente; ``_dig`` retornaria ``False``, que não
# pode ser confundido com ausência).
_MISSING = object()


def _dig(obj: Any, dotted: str) -> Any:
    """Desce por um caminho pontilhado (``a.b.c``) em dicts aninhados.

    Retorna :data:`_MISSING` se qualquer segmento não existir.
    """
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _command_entry(evidence: dict, extract: Any) -> dict | None:
    ctx = _shell_context(evidence) or {}
    commands = ctx.get("commands") or {}
    entry = commands.get(extract)
    return entry if isinstance(entry, dict) else None


@register("evidence.shell.http_json_match")
def http_json_match(args: dict, evidence: dict) -> CriterioResult:
    """Valida o corpo JSON capturado de ``extract`` campo a campo.

    ``equals`` é um mapa ``caminho_pontilhado -> valor_esperado``; ``present`` é
    uma lista de caminhos que precisam existir (qualquer valor, inclusive
    falsy). Caminhos aninhados via :func:`_dig`.
    """
    peso = _peso(args)
    extract = args.get("extract")
    entry = _command_entry(evidence, extract)
    if entry is None:
        return CriterioResult(False, 0, peso, f"comando {extract!r} nao capturado")
    data = entry.get("json")
    if data is None:
        return CriterioResult(
            False,
            0,
            peso,
            f"resposta de {extract!r} nao e JSON valido (a API estava no ar?)",
        )
    for key, expected in (args.get("equals") or {}).items():
        actual = _dig(data, str(key))
        if actual != expected:
            return CriterioResult(
                False, 0, peso, f"{key}: esperado {expected!r}, veio {actual!r}"
            )
    for key in args.get("present") or []:
        if _dig(data, str(key)) is _MISSING:
            return CriterioResult(False, 0, peso, f"campo ausente: {key}")
    return CriterioResult(True, peso, peso, "resposta JSON bate com o contrato")


@register("evidence.shell.json_list_includes")
def json_list_includes(args: dict, evidence: dict) -> CriterioResult:
    """Verifica que a lista em ``field`` contem todos os itens de ``includes``."""
    peso = _peso(args)
    extract = args.get("extract")
    entry = _command_entry(evidence, extract)
    if entry is None or entry.get("json") is None:
        return CriterioResult(False, 0, peso, f"{extract!r} sem JSON")
    val = _dig(entry["json"], str(args.get("field", "")))
    if not isinstance(val, list):
        return CriterioResult(
            False, 0, peso, f"campo {args.get('field')!r} nao e lista"
        )
    faltando = [x for x in (args.get("includes") or []) if x not in val]
    if faltando:
        return CriterioResult(False, 0, peso, f"faltam no campo: {faltando}")
    return CriterioResult(True, peso, peso, f"lista contem {args.get('includes')}")


@register("evidence.shell.json_list_min_len")
def json_list_min_len(args: dict, evidence: dict) -> CriterioResult:
    """Verifica que a lista em ``field`` tem pelo menos ``min`` itens."""
    peso = _peso(args)
    extract = args.get("extract")
    entry = _command_entry(evidence, extract)
    if entry is None or entry.get("json") is None:
        return CriterioResult(False, 0, peso, f"{extract!r} sem JSON")
    val = _dig(entry["json"], str(args.get("field", "")))
    if not isinstance(val, list):
        return CriterioResult(
            False, 0, peso, f"campo {args.get('field')!r} nao e lista"
        )
    n = int(args.get("min", 1))
    if len(val) < n:
        return CriterioResult(False, 0, peso, f"lista tem {len(val)} (< {n})")
    return CriterioResult(True, peso, peso, f"lista tem {len(val)} (>= {n})")


@register("evidence.shell.gh_auth_ok")
def gh_auth_ok(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    ctx = _shell_context(evidence)
    if ctx is None:
        return CriterioResult(False, 0, peso, "shell_evidence ausente")
    if ctx.get("gh_auth_ok") is True:
        user = ctx.get("gh_auth_user") or "?"
        return CriterioResult(True, peso, peso, f"gh autenticado como {user}")
    user = ctx.get("gh_auth_user")
    if user is None:
        return CriterioResult(
            False, 0, peso, "stdout de 'gh auth status' nao confirma login"
        )
    return CriterioResult(
        False, 0, peso, f"usuario autenticado ({user}) difere do roster"
    )


@register("evidence.shell.gh_version_present")
def gh_version_present(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    ctx = _shell_context(evidence)
    if ctx is None:
        return CriterioResult(False, 0, peso, "shell_evidence ausente")
    version = ctx.get("gh_version")
    if isinstance(version, str) and _GH_VERSION_RE.match(version):
        return CriterioResult(True, peso, peso, f"gh version {version}")
    return CriterioResult(False, 0, peso, "gh --version nao capturado ou formato invalido")


@register("evidence.shell.gh_repo_view_ok")
def gh_repo_view_ok(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    ctx = _shell_context(evidence)
    if ctx is None:
        return CriterioResult(False, 0, peso, "shell_evidence ausente")
    view = ctx.get("gh_repo_view")
    if not isinstance(view, dict):
        return CriterioResult(False, 0, peso, "gh repo view nao capturado")
    name = view.get("name")
    visibility = view.get("visibility")
    if not isinstance(name, str) or not name:
        return CriterioResult(False, 0, peso, "gh repo view sem campo 'name'")
    if visibility != "PUBLIC":
        return CriterioResult(
            False, 0, peso, f"repo nao publico (visibility={visibility!r})"
        )
    return CriterioResult(True, peso, peso, f"repo publico: {name}")


# ---------------------------------------------------------------------------
# pytest — "a suíte do aluno passa na máquina do aluno"
#
# O CLI roda `python -m pytest` no repo e manda stdout como evidência. Aqui só
# lemos a linha de sumário. Não é prova criptográfica de nada: o aluno poderia
# forjar o stdout. É a mesma confiança que já damos ao `gh auth status` — o
# objetivo é feedback honesto, não à prova de fraude.
# ---------------------------------------------------------------------------

# Casa "12 passed", "12 passed, 1 warning in 0.4s", "1 failed, 3 passed".
_PYTEST_PASSED_RE = re.compile(r"(\d+)\s+passed", re.IGNORECASE)
_PYTEST_FAILED_RE = re.compile(r"(\d+)\s+(failed|error(?:s)?)", re.IGNORECASE)
_PYTEST_COLLECT_ERROR_RE = re.compile(r"error(?:s)? during collection", re.IGNORECASE)


@register("evidence.shell.pytest_passed")
def pytest_passed(args: dict, evidence: dict) -> CriterioResult:
    """Suíte pytest verde com pelo menos ``args.min_tests`` casos.

    args: ``{extract="pytest", min_tests=1}``
    """
    peso = _peso(args)
    minimo = int(args.get("min_tests", 1) or 1)
    # `extract` aceita lista: o YAML declara `python -m pytest` e
    # `python3 -m pytest` com rótulos distintos, e vale o primeiro que
    # realmente rodou — o outro binário simplesmente não existe na máquina.
    bruto = args.get("extract", "pytest")
    rotulos = [str(x) for x in bruto] if isinstance(bruto, list) else [str(bruto)]
    entry = None
    fallback = None
    for rotulo in rotulos:
        candidato = _command_entry(evidence, rotulo)
        if candidato is None:
            continue
        fallback = fallback or candidato
        saida = str(candidato.get("stdout") or "")
        if not saida.strip():
            continue
        # Interpretador ausente nao e veredito: `python` costuma faltar em
        # WSL/macOS e `python3` no Windows nativo. Segue procurando o outro
        # rotulo; so se TODOS faltarem e que a mensagem de PATH prevalece.
        if "not found in PATH" in saida:
            continue
        entry = candidato
        break
    entry = entry or fallback
    if entry is None:
        return CriterioResult(
            False, 0, peso, f"evidência {rotulos} ausente (o pytest não rodou?)"
        )
    stdout = str(entry.get("stdout") or "")
    if not stdout.strip():
        return CriterioResult(False, 0, peso, "pytest não produziu saída")
    if "not found in PATH" in stdout:
        return CriterioResult(
            False, 0, peso, "o interpretador Python não foi encontrado no PATH"
        )

    falhas = _PYTEST_FAILED_RE.search(stdout)
    if falhas:
        return CriterioResult(
            False, 0, peso, f"pytest reportou {falhas.group(1)} {falhas.group(2)}"
        )
    if _PYTEST_COLLECT_ERROR_RE.search(stdout):
        return CriterioResult(
            False, 0, peso, "pytest falhou na coleta (erro de import nos testes?)"
        )

    passou = _PYTEST_PASSED_RE.search(stdout)
    if not passou:
        exit_code = entry.get("exit_code")
        if exit_code == 5:
            return CriterioResult(False, 0, peso, "pytest não encontrou nenhum teste")
        return CriterioResult(
            False,
            0,
            peso,
            f"não achei o sumário do pytest na saída (exit_code={exit_code})",
        )
    n = int(passou.group(1))
    if n < minimo:
        return CriterioResult(
            False, 0, peso, f"{n} teste(s) passando, esperado >= {minimo}"
        )
    return CriterioResult(True, peso, peso, f"{n} teste(s) passando, nenhum falhando")


# ---------------------------------------------------------------------------
# stdout genérico — o canivete suíço do lado shell
#
# Contraparte de ``evidence.artifacts.content_matches``. Existe porque há
# exercício cuja evidência é inteiramente local (nada de API do GitHub): o que
# prova o fato é a saída de um comando na máquina do aluno, e a regra de qual
# saída prova o quê pertence ao YAML, não a um primitive novo por caso.
# ---------------------------------------------------------------------------

_REGEX_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}


def _compile_shell_arg(args: dict) -> "re.Pattern[str] | str":
    pattern = str(args.get("pattern") or "")
    if not pattern:
        return "args.pattern obrigatorio"
    flags = 0
    for letra in str(args.get("flags") or ""):
        flags |= _REGEX_FLAGS.get(letra.lower(), 0)
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        return f"regex invalida: {exc}"


def _stdout_of(evidence: dict, extract: Any) -> tuple[str, str]:
    """Devolve o stdout de um rótulo, ou a concatenação de vários.

    Aceitar uma LISTA existe para o caso em que o mesmo fato pode aparecer em
    mais de um comando. O motivador é `skills list`: o escopo projeto e o
    escopo usuário são invocações diferentes, e o aluno pode ter instalado a
    skill em qualquer um dos dois — exigir um escopo específico transformaria
    uma checagem de disponibilidade numa pegadinha de flag.

    Basta um rótulo ter rodado. O que não rodou não contribui e não reprova.
    """
    rotulos = extract if isinstance(extract, (list, tuple)) else [extract]
    partes: list[str] = []
    achou = False
    for rotulo in rotulos:
        entry = _command_entry(evidence, rotulo)
        if entry is None:
            continue
        achou = True
        partes.append(str(entry.get("stdout") or ""))
    if not achou:
        return "", f"evidencia {extract!r} ausente (o comando nao rodou?)"
    return "\n".join(partes), ""


@register("evidence.shell.stdout_matches")
def stdout_matches(args: dict, evidence: dict) -> CriterioResult:
    """Conta ocorrências de ``args.pattern`` no stdout de ``args.extract``.

    args: ``{extract, pattern, min=1, flags="", descricao=""}``. ``extract``
    aceita um rótulo ou uma lista deles (os stdouts são concatenados).

    Uso típico: confirmar o nome do repo na saída de ``gh repo view`` sem exigir
    que ele seja público (o `gh` do aluno enxerga o repo privado dele), ou achar
    uma skill na saída de ``skills list``, que é agnóstica de harness.
    """
    peso = _peso(args)
    descricao = str(args.get("descricao") or f"padrao {args.get('pattern')!r}")
    try:
        minimo = int(args.get("min", 1))
    except (TypeError, ValueError):
        return CriterioResult(False, 0, peso, f"args.min invalido: {args.get('min')!r}")
    regex = _compile_shell_arg(args)
    if isinstance(regex, str):
        return CriterioResult(False, 0, peso, regex)
    stdout, erro = _stdout_of(evidence, args.get("extract"))
    if erro:
        return CriterioResult(False, 0, peso, erro)
    n = len(regex.findall(stdout))
    if n >= minimo:
        return CriterioResult(True, peso, peso, f"{descricao}: {n} ocorrencia(s)")
    return CriterioResult(
        False, 0, peso, f"{descricao}: {n} ocorrencia(s), esperado >= {minimo}"
    )


@register("evidence.shell.stdout_absent")
def stdout_absent(args: dict, evidence: dict) -> CriterioResult:
    """Reprova se ``args.pattern`` aparecer no stdout — o inverso do anterior.

    args: ``{extract, pattern, flags="", descricao=""}``. Uso: garantir que um
    ``git ls-files`` não lista arquivo com cara de segredo.
    """
    peso = _peso(args)
    descricao = str(args.get("descricao") or f"padrao {args.get('pattern')!r}")
    regex = _compile_shell_arg(args)
    if isinstance(regex, str):
        return CriterioResult(False, 0, peso, regex)
    stdout, erro = _stdout_of(evidence, args.get("extract"))
    if erro:
        return CriterioResult(False, 0, peso, erro)
    hits = regex.findall(stdout)
    if not hits:
        return CriterioResult(True, peso, peso, f"{descricao}: ausente, como esperado")
    return CriterioResult(
        False, 0, peso, f"{descricao}: {len(hits)} ocorrencia(s) — deveria estar ausente"
    )
