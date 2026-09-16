"""Identidade do request corrente, para o log saber DE QUEM está falando.

Motivação (incidente de 2026-09, turma IA-2026-01): 21 alunos tomavam
`403 not_in_roster` porque o email estava em CAIXA ALTA na planilha. Os
`auth_error` estavam todos no Cloud Logging — e **sem email**, porque só o
`auth_ok` logava quem era. Resultado: nove falhas registradas, zero
atribuíveis, e o problema só andou quando um aluno escreveu duas vezes.

Consertar o `auth.py` resolve metade. A outra metade é o `endpoints.py`, que
tem 18 pontos de rejeição (`turma_not_eligible`, `repo_owner_mismatch`,
`rate_limit_*`...) e, até aqui, **nenhuma** chamada de logger: toda recusa de
regra de negócio era invisível. Passar `request` pelos 18 call sites seria
ruído; um contextvar carrega a identidade até o helper sem tocar em nenhum.

Por que contextvar e não global: o serviço é async e atende requests
concorrentes. Um global seria corrompido pelo request vizinho e passaria a
atribuir a falha de um aluno a outro — pior que não logar nada.
"""

from __future__ import annotations

from contextvars import ContextVar

# Email do aluno autenticado. Vazio antes da autenticação passar — o que é
# informação legítima: significa "a requisição caiu antes de sabermos quem é".
current_email: ContextVar[str] = ContextVar("current_email", default="")
current_correlation_id: ContextVar[str] = ContextVar("current_correlation_id", default="")
current_path: ContextVar[str] = ContextVar("current_path", default="")


def snapshot() -> dict[str, str]:
    """Campos de identidade prontos para o `extra=` de um log record.

    Só inclui o que está preenchido: um `email: ""` no log sugere que o campo
    foi coletado e veio vazio, quando na verdade não havia o que coletar.
    """
    out: dict[str, str] = {}
    email = current_email.get()
    if email:
        out["email"] = email
    cid = current_correlation_id.get()
    if cid:
        out["correlation_id"] = cid
    path = current_path.get()
    if path:
        out["path"] = path
    return out
