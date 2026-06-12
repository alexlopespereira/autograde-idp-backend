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
