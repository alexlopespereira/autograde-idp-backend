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
