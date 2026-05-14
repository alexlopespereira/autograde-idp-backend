"""Validator for the ``shell_evidence`` payload (US-14).

Receives the raw list submitted by the CLI (``shell_evidence`` in
``GradeRequestBody``) and produces a :class:`ShellEvidenceContext` consumed by
the ``evidence.shell.*`` primitives. Validation enforces:

* ``tool`` must be ``"shell"``.
* ``cmd_joined`` must match the per-exercise whitelist.
* ``captured_at`` must lie in ``[exercise.disponivel_a_partir_de,
  submitted_at + 30min]`` (30min of forward tolerance for clock skew).

On any violation the validator raises :class:`InvalidShellEvidence` so the
HTTP layer can short-circuit with a 400 before invoking the grader.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.curriculum import Exercise

CLOCK_SKEW_TOLERANCE = timedelta(minutes=30)

_GH_VERSION_RE = re.compile(r"gh version (\d+\.\d+(?:\.\d+)?)")
_GH_AUTH_USER_RE = re.compile(
    r"Logged in to github\.com (?:account|as) ([A-Za-z0-9][A-Za-z0-9-]*)"
)

# Per-exercise whitelist. cmd_joined must match exactly one of the patterns.
_GH_BASIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^gh --version$"),
    re.compile(r"^gh auth status(?:\s+(?:-h|--hostname github\.com))?$"),
    re.compile(
        r"^gh repo view [A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+"
        r"(?:\s+--json\s+[A-Za-z]+(?:,[A-Za-z]+)*)?$"
    ),
)
_WHITELIST: dict[str, tuple[re.Pattern[str], ...]] = {
    "1.2": _GH_BASIC_PATTERNS,
    "1.3": _GH_BASIC_PATTERNS,
    "1.4": _GH_BASIC_PATTERNS,
}


class InvalidShellEvidence(Exception):
    """Raised when a CommandResult violates the validator contract."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ShellEvidenceContext:
    gh_version: str | None = None
    gh_auth_ok: bool = False
    gh_auth_user: str | None = None
    gh_repo_view: dict[str, Any] | None = None
    commands_seen: tuple[str, ...] = field(default_factory=tuple)

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "gh_version": self.gh_version,
            "gh_auth_ok": self.gh_auth_ok,
            "gh_auth_user": self.gh_auth_user,
            "gh_repo_view": self.gh_repo_view,
            "commands_seen": list(self.commands_seen),
        }


def _parse_captured_at(raw: Any) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise InvalidShellEvidence("captured_at ausente ou nao-string")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidShellEvidence(f"captured_at invalido: {exc}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _ensure_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _match_whitelist(exercise_id: str, cmd_joined: str) -> bool:
    patterns = _WHITELIST.get(exercise_id, ())
    return any(p.match(cmd_joined) for p in patterns)


def _extract_repo_view(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text or not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def validate_shell_evidence(
    evidence: Iterable[Any],
    exercise: Exercise,
    *,
    expected_github_user: str,
    submitted_at: datetime,
) -> ShellEvidenceContext:
    """Validate ``shell_evidence`` and produce a parsed context.

    Empty evidence is allowed and yields an all-default context (the grader
    will then fail any ``evidence.shell.*`` criterion the exercise requires).
    """
    items = list(evidence or [])
    if not items:
        return ShellEvidenceContext()

    lower_bound = _ensure_aware_utc(exercise.disponivel_a_partir_de)
    upper_bound = _ensure_aware_utc(submitted_at) + CLOCK_SKEW_TOLERANCE

    gh_version: str | None = None
    gh_auth_user: str | None = None
    gh_auth_ok = False
    gh_repo_view: dict[str, Any] | None = None
    commands_seen: list[str] = []

    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise InvalidShellEvidence(
                f"shell_evidence[{idx}] precisa ser objeto, recebido {type(raw).__name__}"
            )
        tool = raw.get("tool")
        if tool != "shell":
            raise InvalidShellEvidence(
                f"shell_evidence[{idx}].tool={tool!r} (esperado 'shell')"
            )
        cmd_joined = raw.get("cmd_joined")
        if not isinstance(cmd_joined, str) or not cmd_joined:
            raise InvalidShellEvidence(
                f"shell_evidence[{idx}].cmd_joined ausente"
            )
        if not _match_whitelist(exercise.id, cmd_joined):
            raise InvalidShellEvidence(
                f"shell_evidence[{idx}].cmd_joined fora do whitelist: {cmd_joined!r}"
            )

        captured_at = _parse_captured_at(raw.get("captured_at"))
        if captured_at < lower_bound:
            raise InvalidShellEvidence(
                f"shell_evidence[{idx}].captured_at anterior a disponivel_a_partir_de"
            )
        if captured_at > upper_bound:
            raise InvalidShellEvidence(
                f"shell_evidence[{idx}].captured_at posterior a submitted_at + 30min"
            )

        stdout = raw.get("stdout") or ""
        if not isinstance(stdout, str):
            raise InvalidShellEvidence(
                f"shell_evidence[{idx}].stdout precisa ser string"
            )

        commands_seen.append(cmd_joined)

        if cmd_joined == "gh --version":
            m = _GH_VERSION_RE.search(stdout)
            if m:
                gh_version = m.group(1)
        elif cmd_joined.startswith("gh auth status"):
            m = _GH_AUTH_USER_RE.search(stdout)
            if m:
                gh_auth_user = m.group(1)
                gh_auth_ok = gh_auth_user.lower() == expected_github_user.lower()
        elif cmd_joined.startswith("gh repo view"):
            gh_repo_view = _extract_repo_view(stdout)

    return ShellEvidenceContext(
        gh_version=gh_version,
        gh_auth_ok=gh_auth_ok,
        gh_auth_user=gh_auth_user,
        gh_repo_view=gh_repo_view,
        commands_seen=tuple(commands_seen),
    )
