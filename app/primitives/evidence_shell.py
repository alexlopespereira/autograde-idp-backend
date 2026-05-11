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
