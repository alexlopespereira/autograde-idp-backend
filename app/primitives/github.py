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


# ---------------------------------------------------------------------------
# Checagens sobre o repositório que não cabiam nas primitives de arquivo único
# ---------------------------------------------------------------------------


@register("github.repo.files_matching_min")
def repo_files_matching_min(args: dict, evidence: dict) -> CriterioResult:
    """Conta arquivos do repo cujo path casa o glob ``args.pattern``.

    args: ``{pattern, min=1, descricao=""}``

    Existe porque ``github.repo.has_file`` exige o path exato, e boa parte do
    enunciado pede "uma pasta ``tests/`` com pelo menos 4 casos" — o nome dos
    arquivos é escolha do aluno.
    """
    peso = _peso(args)
    pattern = str(args.get("pattern") or "")
    if not pattern:
        return CriterioResult(False, 0, peso, "args.pattern obrigatorio")
    try:
        minimo = int(args.get("min", 1))
    except (TypeError, ValueError):
        return CriterioResult(False, 0, peso, f"args.min invalido: {args.get('min')!r}")
    descricao = str(args.get("descricao") or f"arquivos casando '{pattern}'")
    files = evidence.get("files_list") or []
    hits = [f for f in files if isinstance(f, str) and fnmatch.fnmatch(f, pattern)]
    if len(hits) >= minimo:
        return CriterioResult(True, peso, peso, f"{descricao}: {len(hits)}")
    return CriterioResult(
        False, 0, peso, f"{descricao}: {len(hits)}, esperado >= {minimo}"
    )


@register("github.commits.message_pattern_count")
def commits_message_pattern_count(args: dict, evidence: dict) -> CriterioResult:
    """Conta commits cuja mensagem casa ``args.pattern``.

    args: ``{pattern, min=1, descricao=""}``

    Só enxerga os ``MAX_COMMITS_COLLECTED`` commits mais recentes (ver
    ``github_client``) — o suficiente para "pelo menos N commits 'ralph: iter
    <n>'", que é o uso previsto.
    """
    peso = _peso(args)
    pattern = str(args.get("pattern") or "")
    if not pattern:
        return CriterioResult(False, 0, peso, "args.pattern obrigatorio")
    try:
        regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        return CriterioResult(False, 0, peso, f"regex invalida: {exc}")
    try:
        minimo = int(args.get("min", 1))
    except (TypeError, ValueError):
        return CriterioResult(False, 0, peso, f"args.min invalido: {args.get('min')!r}")
    descricao = str(args.get("descricao") or f"commits casando {pattern!r}")
    commits = evidence.get("commits") or []
    hits = [
        c
        for c in commits
        if isinstance(c, dict) and regex.search(str(c.get("message") or ""))
    ]
    if len(hits) >= minimo:
        return CriterioResult(True, peso, peso, f"{descricao}: {len(hits)}")
    return CriterioResult(
        False,
        0,
        peso,
        f"{descricao}: {len(hits)}, esperado >= {minimo} "
        f"({len(commits)} commits inspecionados)",
    )


# Nomes de arquivo que quase sempre carregam segredo. Checagem por NOME, não
# por conteúdo: o evidence traz a árvore do repo, não os bytes de cada blob.
# Um `.env` versionado é o erro comum de verdade; chave colada dentro do
# `config.py` escapa daqui e fica pro code review humano.
_SECRET_FILE_PATTERNS: tuple[str, ...] = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*id_rsa",
    "*id_dsa",
    "*id_ecdsa",
    "*id_ed25519",
    ".env",
    "*/.env",
    ".env.*",
    "*/.env.*",
    "*credentials.json",
    "*service-account*.json",
    "*secrets.y*ml",
    "*.pypirc",
    "*.npmrc",
)
# Falsos positivos frequentes e inofensivos: exemplos e templates.
_SECRET_ALLOW_PATTERNS: tuple[str, ...] = (
    "*.env.example",
    "*.env.sample",
    "*.env.template",
    "*.example",
    "*.sample",
    "*.template",
)


@register("github.repo.no_secret_files")
def repo_no_secret_files(args: dict, evidence: dict) -> CriterioResult:
    """Reprova se a árvore do repo tiver arquivo com cara de segredo.

    args: ``{extra=[], allow=[]}`` — globs somados às listas padrão.
    """
    peso = _peso(args)
    extra = [str(p) for p in (args.get("extra") or [])]
    allow = [str(p) for p in (args.get("allow") or [])]
    padroes = _SECRET_FILE_PATTERNS + tuple(extra)
    permitidos = _SECRET_ALLOW_PATTERNS + tuple(allow)
    files = [f for f in (evidence.get("files_list") or []) if isinstance(f, str)]
    if not files:
        return CriterioResult(True, peso, peso, "repo sem arquivos para inspecionar")
    suspeitos = []
    for f in files:
        if any(fnmatch.fnmatch(f, ok) for ok in permitidos):
            continue
        if any(fnmatch.fnmatch(f, pat) for pat in padroes):
            suspeitos.append(f)
    if suspeitos:
        preview = ", ".join(suspeitos[:5])
        mais = f" (+{len(suspeitos) - 5})" if len(suspeitos) > 5 else ""
        return CriterioResult(
            False,
            0,
            peso,
            f"arquivo(s) com cara de segredo versionado(s): {preview}{mais}. "
            f"Remova do historico e adicione ao .gitignore.",
        )
    return CriterioResult(
        True, peso, peso, f"nenhum arquivo suspeito entre {len(files)} do repo"
    )


@register("github.file.first_commit_before")
def file_first_commit_before(args: dict, evidence: dict) -> CriterioResult:
    """Garante que ``args.path_a`` entrou no repo ANTES de ``args.path_b``.

    args: ``{path_a, path_b, descricao=""}``

    Serve para checar ordem de trabalho, não só existência: "o transcript do
    grill-me foi commitado antes do protocolo" prova que o protocolo saiu da
    sessão, e não o contrário. Consome ``evidence['file_first_commit']``, que
    o endpoint popula só para os paths citados aqui.
    """
    peso = _peso(args)
    path_a = str(args.get("path_a") or "")
    path_b = str(args.get("path_b") or "")
    if not path_a or not path_b:
        return CriterioResult(False, 0, peso, "args.path_a e args.path_b obrigatorios")
    descricao = str(args.get("descricao") or f"'{path_a}' antes de '{path_b}'")
    mapa = evidence.get("file_first_commit")
    if not isinstance(mapa, dict):
        return CriterioResult(
            False, 0, peso, "datas de primeiro commit nao coletadas para este exercicio"
        )
    raw_a = mapa.get(path_a)
    raw_b = mapa.get(path_b)
    if not raw_a:
        return CriterioResult(
            False, 0, peso, f"'{path_a}' nao tem commit no historico do repo"
        )
    if not raw_b:
        return CriterioResult(
            False, 0, peso, f"'{path_b}' nao tem commit no historico do repo"
        )
    try:
        dt_a = _parse_iso(str(raw_a))
        dt_b = _parse_iso(str(raw_b))
    except ValueError as exc:
        return CriterioResult(False, 0, peso, f"data de commit invalida: {exc}")
    if dt_a < dt_b:
        return CriterioResult(
            True, peso, peso, f"{descricao}: OK ({dt_a.date()} < {dt_b.date()})"
        )
    if dt_a == dt_b:
        return CriterioResult(
            False,
            0,
            peso,
            f"{descricao}: os dois entraram no MESMO commit — "
            f"commite o primeiro antes de escrever o segundo",
        )
    return CriterioResult(
        False,
        0,
        peso,
        f"{descricao}: fora de ordem ({dt_a.date()} veio depois de {dt_b.date()})",
    )
