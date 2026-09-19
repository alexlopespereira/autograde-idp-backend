from __future__ import annotations

import csv
import io
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

REQUIRED_COLUMNS = ("email", "nome", "turma", "github_username")
# Separadores aceitos na coluna `turma`. Um aluno pode cursar mais de uma
# disciplina servida pelo mesmo autograder (ex.: Transformação Digital E
# Agentes de IA), e o roster tem uma linha por email — então a coluna precisa
# comportar N turmas. `TD-2026-01;IA-2026-01` é o formato canônico.
TURMA_SEPARATORS = ";,|"
# Mesmos separadores para a coluna `email`. Um aluno tem UMA linha, mas pode
# ter mais de uma conta Google: o email institucional que o professor recebe da
# secretaria (`ricardo.c.costa@caixa.gov.br`) e o pessoal com que ele de fato
# faz `autograde login` (`rick.palmeiras@uol.com.br`). Cadastrar so um dos dois
# e apostar em qual deles o aluno vai usar — e a aposta errada sai como
# `403 not_in_roster` com o email certo na tela do aluno, que e o modo de falha
# mais confuso deste sistema (ver `normalize_email`). Aceitar os dois na mesma
# celula tira a aposta do caminho: `institucional@x;pessoal@y` resolve pelos
# dois. O PRIMEIRO e a identidade canonica — ver `RosterEntry.emails`.
EMAIL_SEPARATORS = TURMA_SEPARATORS
# Subset que precisa estar PREENCHIDO em cada linha. 'nome' e 'github_username'
# podem chegar vazios no paste manual e ser completados depois via /me/profile.
REQUIRED_NONEMPTY_COLUMNS = ("email", "turma")
ROSTER_TTL_SECONDS = 300

T = TypeVar("T")


class RosterValidationError(Exception):
    """Raised when a roster CSV violates schema or uniqueness constraints."""


def split_turmas(raw: str) -> tuple[str, ...]:
    """``"TD-2026-01;IA-2026-01"`` -> ``("TD-2026-01", "IA-2026-01")``.

    Uma turma só continua funcionando sem mudança nenhuma na planilha —
    é o caso de uma lista de um elemento. Dedupe preservando a ordem para
    que a primeira turma continue sendo a "principal" (a que vai pra coluna
    `turma` da Submissions Sheet quando nada mais desempata).
    """
    pattern = "[" + re.escape(TURMA_SEPARATORS) + "]"
    out: list[str] = []
    for part in re.split(pattern, raw or ""):
        turma = part.strip()
        if turma and turma not in out:
            out.append(turma)
    return tuple(out)


def normalize_email(raw: str) -> str:
    """Forma canônica de um email para casar planilha com login Google.

    O Google sempre manda a claim `email` do id_token em minúsculas; a
    planilha é digitada/colada à mão e volta e meia vem em CAIXA ALTA. O
    lookup do middleware é um `dict.get` exato, então `IGO@GMAIL.COM` na
    planilha e `igo@gmail.com` no token não se encontram, e o aluno toma
    `403 not_in_roster` com o email aparentemente certo na tela — o modo de
    falha mais confuso que esse sistema tem. Normalizar aqui, na fronteira
    de leitura, faz a planilha parar de importar para o casamento.

    Só `strip` + `lower`: NÃO removemos pontos nem sufixo `+tag`, porque
    `a.b@gmail.com` e `ab@gmail.com` serem a mesma caixa é regra do Gmail,
    não de email em geral, e o roster tem domínio institucional (`.gov.br`,
    `.org.br`) onde apagar ponto junta pessoas diferentes.
    """
    return (raw or "").strip().lower()


def split_emails(raw: str) -> tuple[str, ...]:
    """``"Inst@x.br;pessoal@y.com"`` -> ``("inst@x.br", "pessoal@y.com")``.

    Mesma forma de `split_turmas`, com `normalize_email` aplicado em cada
    pedaco — a celula e digitada a mao e volta e meia vem com espaco ou CAIXA
    ALTA no meio da lista, nao so nas pontas.

    Dedupe preservando a ordem, porque a ORDEM carrega significado: o primeiro
    email e a identidade canonica do aluno (`RosterEntry.emails`), o que vai
    para a planilha de submissoes e para o log. Os demais sao apelidos de
    login. Uma lista de um elemento e o caso normal e continua funcionando sem
    tocar na planilha.
    """
    pattern = "[" + re.escape(EMAIL_SEPARATORS) + "]"
    out: list[str] = []
    for part in re.split(pattern, raw or ""):
        email = normalize_email(part)
        if email and email not in out:
            out.append(email)
    return tuple(out)


@dataclass(frozen=True)
class RosterEntry:
    email: str
    nome: str
    turma: str
    github_username: str

    @property
    def turmas(self) -> tuple[str, ...]:
        """Turmas do aluno, já separadas. Ver :func:`split_turmas`."""
        return split_turmas(self.turma)

    @property
    def emails(self) -> tuple[str, ...]:
        """Contas Google que resolvem para este aluno. Ver :func:`split_emails`.

        ``emails[0]`` é a identidade canônica: é ela que vai para a coluna
        `email` da Submissions Sheet, para o `reqctx` e para o log. Os demais
        são apelidos de login e nunca são gravados em lugar nenhum — senão o
        histórico de notas de um aluno se parte em duas identidades, e a MAIOR
        nota (que é a que conta) pode ficar na metade invisível.

        Corolário operacional: **não reordene a célula**. Trocar qual email vem
        primeiro renomeia o aluno aos olhos da planilha de submissões e esconde
        o que ele já entregou. Para acrescentar uma conta, acrescente no FIM.
        """
        return split_emails(self.email)


def parse_roster(csv_text: str) -> dict[str, RosterEntry]:
    """CSV do roster -> ``{conta Google: RosterEntry}``.

    A chave e a CONTA, nao o aluno: uma linha com
    `institucional@x;pessoal@y` produz duas chaves apontando para a mesma
    `RosterEntry`. Isso e o que faz o `roster.get(email_do_token)` do
    middleware achar o aluno sem saber com qual das contas dele o Google
    respondeu.
    """
    if not csv_text or not csv_text.strip():
        raise RosterValidationError("CSV vazio")

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise RosterValidationError("CSV sem header")

    missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
    if missing:
        raise RosterValidationError(f"colunas obrigatorias faltantes: {missing}")

    result: dict[str, RosterEntry] = {}
    for idx, row in enumerate(reader, start=2):  # header is line 1
        for col in REQUIRED_NONEMPTY_COLUMNS:
            value = (row.get(col) or "").strip()
            if not value:
                raise RosterValidationError(f"row {idx}: campo '{col}' vazio")
        # A celula pode listar mais de uma conta Google (ver EMAIL_SEPARATORS).
        # O dict e indexado por TODAS elas, apontando para a MESMA entrada: o
        # lookup do middleware continua sendo um `dict.get` exato e O(1), e
        # quem loga por apelido chega na mesma identidade de quem loga pelo
        # canonico. `len(result)` deixa de ser "numero de alunos" e passa a ser
        # "numero de contas" — quem conta aluno tem que contar entradas
        # distintas (e o que o audit faz).
        emails = split_emails(row["email"])
        if not emails:
            raise RosterValidationError(f"row {idx}: campo 'email' vazio")
        entry = RosterEntry(
            email=";".join(emails),
            nome=(row.get("nome") or "").strip(),
            turma=row["turma"].strip(),
            github_username=(row.get("github_username") or "").strip(),
        )
        for email in emails:
            # Colisao entre linhas E entre apelidos da mesma linha: duas contas
            # iguais em lugares diferentes da planilha significam que ninguem
            # sabe qual linha manda, e o parser recusa o CSV inteiro antes de
            # deixar isso virar nota no lugar errado.
            if email in result:
                raise RosterValidationError(f"row {idx}: email duplicado '{email}'")
            result[email] = entry

    if not result:
        raise RosterValidationError("CSV sem linhas de dados")

    return result


_CACHE: dict[str, tuple[float, Any]] = {}


def _now() -> float:
    return time.time()


def get_or_fetch(
    url: str,
    fetcher: Callable[[str], T],
    ttl_seconds: int,
) -> T:
    if ttl_seconds <= 0:
        return fetcher(url)
    now = _now()
    cached = _CACHE.get(url)
    if cached is not None:
        cached_at, cached_value = cached
        if (now - cached_at) < ttl_seconds:
            return cached_value
    value = fetcher(url)
    _CACHE[url] = (now, value)
    return value


def _clear_cache() -> None:
    _CACHE.clear()


def fetch_roster(
    url: str,
    fetcher: Callable[[str], str],
) -> dict[str, RosterEntry]:
    def _fetch_and_parse(u: str) -> dict[str, RosterEntry]:
        return parse_roster(fetcher(u))

    return get_or_fetch(url, _fetch_and_parse, ttl_seconds=ROSTER_TTL_SECONDS)
