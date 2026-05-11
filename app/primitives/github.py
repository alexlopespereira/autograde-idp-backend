from __future__ import annotations

import fnmatch
import re
from datetime import datetime, timedelta, timezone

from . import CriterioResult, register

_DURATION_RE = re.compile(r"^\s*(\d+)\s*(h|d|w)\s*$", re.IGNORECASE)


def _peso(args: dict) -> int:
    try:
        return int(args.get("_peso", 0))
    except (TypeError, ValueError):
        return 0


def _parse_duration(text: str) -> timedelta:
    m = _DURATION_RE.match(text or "")
    if not m:
        raise ValueError(f"duration nao reconhecida: {text!r} (esperado ex.: '24h', '7d', '1w')")
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    return timedelta(weeks=n)


def _parse_iso(ts: str) -> datetime:
    text = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


@register("github.repo.exists")
def repo_exists(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    if evidence.get("repo_exists"):
        return CriterioResult(True, peso, peso, "repositorio encontrado")
    return CriterioResult(False, 0, peso, "repositorio nao encontrado")


@register("github.repo.public")
def repo_public(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    if evidence.get("repo_public"):
        return CriterioResult(True, peso, peso, "repositorio publico")
    return CriterioResult(False, 0, peso, "repositorio nao e publico (privado ou inexistente)")


@register("github.repo.has_file")
def repo_has_file(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    path = args.get("path")
    if not path:
        return CriterioResult(False, 0, peso, "args.path obrigatorio")
    files = evidence.get("files_list") or []
    if path in files:
        return CriterioResult(True, peso, peso, f"arquivo '{path}' presente")
    return CriterioResult(False, 0, peso, f"arquivo '{path}' ausente")


@register("github.repo.file_not_empty")
def repo_file_not_empty(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    path = args.get("path")
    if not path:
        return CriterioResult(False, 0, peso, "args.path obrigatorio")
    sizes = evidence.get("file_sizes") or {}
    size = sizes.get(path)
    if isinstance(size, int) and size > 0:
        return CriterioResult(True, peso, peso, f"arquivo '{path}' tem {size} bytes")
    return CriterioResult(False, 0, peso, f"arquivo '{path}' vazio ou ausente")


@register("github.repo.name_matches")
def repo_name_matches(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    pattern = args.get("pattern")
    if not pattern:
        return CriterioResult(False, 0, peso, "args.pattern obrigatorio")
    owner_repo = evidence.get("owner_repo") or ""
    if "/" not in owner_repo:
        return CriterioResult(False, 0, peso, "owner_repo ausente no evidence")
    name = owner_repo.split("/", 1)[1]
    if fnmatch.fnmatch(name, pattern):
        return CriterioResult(True, peso, peso, f"nome '{name}' bate com '{pattern}'")
    return CriterioResult(False, 0, peso, f"nome '{name}' nao bate com '{pattern}'")


@register("github.commits.count_at_least")
def commits_count_at_least(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    try:
        n = int(args.get("n", 1))
    except (TypeError, ValueError):
        return CriterioResult(False, 0, peso, f"args.n invalido: {args.get('n')!r}")
    commits = evidence.get("commits") or []
    count = len(commits)
    if count >= n:
        return CriterioResult(True, peso, peso, f"{count} commits (>= {n})")
    return CriterioResult(False, 0, peso, f"{count} commits (< {n})")


_PR_PLACEHOLDER_TITLES = frozenset(
    {
        "update readme",
        "update readme.md",
        "update",
        "wip",
        "draft",
        "test",
        "test pr",
        "fix",
        "tmp",
        "temp",
        "initial commit",
    }
)


def _pr_pool(evidence: dict, state: str) -> list[dict]:
    """Resolve PR pool for a given state. Defensive on missing keys.

    Evidence today exposes ``prs_open`` and ``prs_merged``. Optional
    ``prs_closed_unmerged`` is honored if present (forward compat).
    """
    open_prs = evidence.get("prs_open") or []
    merged_prs = evidence.get("prs_merged") or []
    closed_unmerged = evidence.get("prs_closed_unmerged") or []
    if state == "open":
        return list(open_prs)
    if state == "merged":
        return list(merged_prs)
    if state == "closed":
        return list(merged_prs) + list(closed_unmerged)
    if state == "all":
        return list(open_prs) + list(merged_prs) + list(closed_unmerged)
    return []


@register("github.pr.count")
def pr_count(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    state = str(args.get("state", "all")).lower()
    if state not in ("open", "closed", "merged", "all"):
        return CriterioResult(
            False, 0, peso, f"args.state invalido: {state!r} (esperado open|closed|merged|all)"
        )
    try:
        minimum = int(args.get("min", 1))
    except (TypeError, ValueError):
        return CriterioResult(False, 0, peso, f"args.min invalido: {args.get('min')!r}")
    prs = _pr_pool(evidence, state)
    count = len(prs)
    if count >= minimum:
        return CriterioResult(True, peso, peso, f"{count} PRs ({state}) >= {minimum}")
    return CriterioResult(False, 0, peso, f"{count} PRs ({state}) < {minimum}")


@register("github.pr.has_descriptive_title")
def pr_has_descriptive_title(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    try:
        min_chars = int(args.get("min_chars", 10))
    except (TypeError, ValueError):
        return CriterioResult(False, 0, peso, f"args.min_chars invalido: {args.get('min_chars')!r}")
    prs = _pr_pool(evidence, "all")
    if not prs:
        return CriterioResult(False, 0, peso, "nenhum PR para avaliar titulo")
    for pr in prs:
        title = str((pr or {}).get("title") or "").strip()
        if len(title) < min_chars:
            continue
        if title.lower() in _PR_PLACEHOLDER_TITLES:
            continue
        return CriterioResult(True, peso, peso, f"PR com titulo descritivo: {title!r}")
    return CriterioResult(
        False,
        0,
        peso,
        f"nenhum PR com titulo descritivo (>= {min_chars} chars, nao-placeholder)",
    )


@register("github.commits.last_within")
def commits_last_within(args: dict, evidence: dict) -> CriterioResult:
    peso = _peso(args)
    duration_raw = args.get("duration", "")
    try:
        delta = _parse_duration(str(duration_raw))
    except ValueError as exc:
        return CriterioResult(False, 0, peso, str(exc))
    commits = evidence.get("commits") or []
    if not commits:
        return CriterioResult(False, 0, peso, "sem commits para verificar")
    cutoff = _now_utc() - delta
    for c in commits:
        ts_raw = (c or {}).get("committed_at")
        if not ts_raw:
            continue
        try:
            ts = _parse_iso(ts_raw)
        except ValueError:
            continue
        if ts >= cutoff:
            return CriterioResult(True, peso, peso, f"commit dentro de {duration_raw}")
    return CriterioResult(False, 0, peso, f"nenhum commit nas ultimas {duration_raw}")
