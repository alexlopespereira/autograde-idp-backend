#!/usr/bin/env python3
"""audit_planilhas.py — audita roster + calendários contra os parsers reais.

Por que existe: as duas fontes de verdade deste serviço não são código e não
passam por PR. O roster é uma Google Sheet editada à mão; os calendários são
YAMLs buscados da `main` a cada submissão. Nenhum teste do repo carrega
qualquer um dos dois, então um erro de digitação numa célula só aparece na tela
do aluno — e como `403` costuma parecer culpa dele, chega ao professor por
email, dias depois, se chegar.

Dois incidentes de setembro/2026 medem o custo:

- 21 alunos de IA-2026-01 colados em CAIXA ALTA tomaram `403 not_in_roster`
  vendo o próprio email correto na mensagem. Nove falhas ficaram no Cloud
  Logging sem ninguém olhar; o caso só andou quando um aluno escreveu duas
  vezes.
- Um arraste de autofill no Sheets incrementou a coluna `turma` de 29 alunos de
  TD (`IA-2026-02`, `-03`, ... `-30`) e apagou uma linha. A planilha ficou 13h
  nesse estado. Ninguém de TD tentou submeter nessas 13h — o dano foi zero por
  sorte, não por controle.

O que este script NÃO é: uma reimplementação das checagens. Ele importa
`app.roster.parse_roster` e `app.calendario.parse_calendario` — os mesmos
parsers que o request usa. Se o parser aceita aqui, aceita em produção; se
recusa aqui, a turma inteira levaria 502. É essa igualdade que faz o resultado
valer alguma coisa.

Contrato (consumido pelo workflow `audit-planilhas.yml`):
  exit 0  -> nada fatal (avisos podem existir e vão para stdout)
  exit 1  -> achado FATAL: alguém está bloqueado agora, ou vai estar
  exit 2  -> o próprio audit não conseguiu rodar (env faltando, rede)

Env: `ROSTER_URL` (obrigatório), `EXERCISES_BASE_URL` e
`EXERCISES_BASE_URL_<CURSO>` (ao menos um) — as mesmas do serviço, para o
audit ler exatamente o que produção lê.

Uso:
  python scripts/audit_planilhas.py             # roster + calendarios
  python scripts/audit_planilhas.py roster
  python scripts/audit_planilhas.py calendario
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.calendario import (  # noqa: E402
    CalendarioValidationError,
    calendario_url,
    parse_calendario,
)
from app.roster import (  # noqa: E402
    RosterValidationError,
    normalize_email,
    parse_roster,
)

TIMEOUT = 30


class Achados:
    """Coletor de achados. `fatal` decide o exit code, nada mais."""

    def __init__(self) -> None:
        self.fatais: list[str] = []
        self.avisos: list[str] = []

    def fatal(self, msg: str, conserto: str = "") -> None:
        self.fatais.append(msg)
        print(f"  [FATAL] {msg}")
        if conserto:
            print(f"          conserto: {conserto}")

    def aviso(self, msg: str, conserto: str = "") -> None:
        self.avisos.append(msg)
        print(f"  [AVISO] {msg}")
        if conserto:
            print(f"          conserto: {conserto}")

    def ok(self, msg: str, detalhe: str = "") -> None:
        print(f"  [OK]    {msg}{('  ' + detalhe) if detalhe else ''}")


def secao(titulo: str) -> None:
    print("\n" + "=" * 70)
    print(f"== {titulo}")
    print("=" * 70)


def http_get(url: str) -> tuple[int, str]:
    """``(status, corpo)``. 404 volta como ``(404, "")`` em vez de estourar.

    A distinção é a mesma que `app.calendario` faz: "não existe" é resposta
    legítima (turma de TD não tem calendário no repo de IA), enquanto rede fora
    é "não sei" e não pode virar achado contra a planilha.
    """
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def bases_por_curso() -> dict[str, str]:
    """``{curso: base_url}`` a partir das env vars do serviço.

    `EXERCISES_BASE_URL` é o curso default (`td`, legado sem prefixo);
    `EXERCISES_BASE_URL_<CURSO>` é um curso nomeado. Lido do ambiente, e não de
    uma lista no código, porque é assim que o backend descobre a mesma coisa —
    curso novo entra no audit sem ninguém lembrar de editar este arquivo.
    """
    bases: dict[str, str] = {}
    default = os.environ.get("EXERCISES_BASE_URL", "").strip()
    if default:
        bases["td"] = default
    prefixo = "EXERCISES_BASE_URL_"
    for chave, valor in os.environ.items():
        if chave.startswith(prefixo) and valor.strip():
            curso = chave[len(prefixo) :].lower()
            if curso:
                bases[curso] = valor.strip()
    return bases


# --------------------------------------------------------------------------
# roster


def audit_roster(a: Achados, bases: dict[str, str]) -> dict[str, object] | None:
    """Audita a Roster Sheet. Devolve as entradas parseadas, ou ``None``."""
    secao("Roster (CSV publicado -> parser do backend)")

    url = os.environ.get("ROSTER_URL", "").strip()
    if not url:
        print("  [ERRO] ROSTER_URL nao definida no ambiente")
        sys.exit(2)

    status, csv_texto = http_get(url)
    if status != 200 or not csv_texto:
        print(f"  [ERRO] CSV do roster nao baixou (HTTP {status})")
        sys.exit(2)
    a.ok("CSV baixou", f"HTTP {status}, {len(csv_texto)} bytes")

    # Fatal de verdade: `parse_roster` e all-or-nothing. Recusado aqui =
    # `502 roster_unavailable` para TODA a turma, nao so para a linha ruim.
    try:
        entradas = parse_roster(csv_texto)
    except RosterValidationError as exc:
        a.fatal(
            f"o parser RECUSA o CSV: {exc}",
            "a planilha inteira para de valer -> 502 roster_unavailable para "
            "TODOS os alunos. Conserte a linha citada; contas repetidas se "
            "resolvem mesclando as linhas (a coluna `turma` aceita varias "
            "separadas por `;`, e a `email` tambem).",
        )
        return None

    alunos = {id(e): e for e in entradas.values()}
    a.ok(
        "parser aceitou o CSV",
        f"{len(alunos)} aluno(s) em {len(entradas)} conta(s) Google",
    )

    # O `dict.get` exato do middleware, reproduzido conta por conta. Contar
    # linhas nunca responde isto — foi por isso que "tudo verde" conviveu com
    # 21 alunos tomando 403.
    import csv as _csv
    import io as _io

    nao_resolvem: list[str] = []
    for linha in _csv.DictReader(_io.StringIO(csv_texto)):
        for parte in (linha.get("email") or "").replace(",", ";").replace("|", ";").split(";"):
            conta = normalize_email(parte)
            if conta and conta not in entradas:
                nao_resolvem.append(conta)
    if nao_resolvem:
        a.fatal(
            f"{len(nao_resolvem)} conta(s) da planilha NAO resolvem no lookup "
            f"do middleware: {', '.join(nao_resolvem[:5])}",
            "estes alunos levam 403 not_in_roster vendo o email certo na tela.",
        )
    else:
        a.ok(
            "toda conta da planilha resolve no lookup do middleware",
            f"{len(entradas)}/{len(entradas)}",
        )

    # --- turmas: existe calendario para elas? ------------------------------
    # Pos-0.5.0 a matricula mora em `turmas/<TURMA>.yaml`. Turma do roster sem
    # calendario em curso NENHUM e a assinatura exata do autofill: a celula
    # parece plausivel (`IA-2026-30`) e nao existe em lugar nenhum.
    turmas_no_roster: dict[str, int] = {}
    for entrada in alunos.values():
        for turma in entrada.turmas:  # type: ignore[attr-defined]
            turmas_no_roster[turma] = turmas_no_roster.get(turma, 0) + 1

    com_calendario: set[str] = set()
    for turma in sorted(turmas_no_roster):
        for base in bases.values():
            status, _ = http_get(calendario_url(base, turma))
            if status == 200:
                com_calendario.add(turma)
                break

    sem_calendario = sorted(set(turmas_no_roster) - com_calendario)
    print(f"\n  turmas no roster: {len(turmas_no_roster)}")
    for turma in sorted(turmas_no_roster):
        marca = "ok" if turma in com_calendario else "SEM CALENDARIO"
        print(f"    {turma:<16} {turmas_no_roster[turma]:>3} aluno(s)  {marca}")

    if sem_calendario:
        # Aviso e nao fatal: o `prazo:` legado do YAML do exercicio ainda
        # atende turma sem calendario, de proposito (a janela entre publicar o
        # backend e publicar os calendarios e real).
        a.aviso(
            f"{len(sem_calendario)} turma(s) do roster sem calendario em curso "
            f"nenhum: {', '.join(sem_calendario)}",
            "confira se nao e autofill do Sheets na coluna `turma` (ele "
            "incrementa o numero final linha a linha). Se a turma e real, "
            "crie `exercicios/turmas/<TURMA>.yaml` no repo do curso.",
        )

    # Este e o dano de verdade, e o que a contagem por turma nao mostra: aluno
    # cujas turmas TODAS sao desconhecidas nao consegue fazer exercicio nenhum.
    encalhados = [
        entrada.emails[0]  # type: ignore[attr-defined]
        for entrada in alunos.values()
        if entrada.turmas and not (set(entrada.turmas) & com_calendario)  # type: ignore[attr-defined]
    ]
    if encalhados:
        a.fatal(
            f"{len(encalhados)} aluno(s) sem NENHUMA turma com calendario: "
            f"{', '.join(encalhados[:5])}",
            "eles levam 403 turma_not_eligible em qualquer exercicio dos dois "
            "cursos. Corrija a coluna `turma` da linha deles.",
        )
    else:
        a.ok("todo aluno tem ao menos uma turma com calendario", f"{len(alunos)}/{len(alunos)}")

    return entradas


# --------------------------------------------------------------------------
# calendarios


def audit_calendarios(a: Achados, bases: dict[str, str], turmas: list[str]) -> None:
    """Audita os `turmas/<TURMA>.yaml` de cada curso.

    A suíte que estes arquivos não têm: nenhum teste do repo os carrega, e eles
    são buscados a cada submissão. Um que não parseia derruba a turma INTEIRA
    com `502 calendario_unavailable`.
    """
    secao("Calendarios das turmas (turmas/<TURMA>.yaml na main)")

    agora = datetime.now(tz=timezone.utc)
    achou_algum = False

    for curso, base in sorted(bases.items()):
        for turma in turmas:
            url = calendario_url(base, turma)
            status, texto = http_get(url)
            if status == 404:
                continue  # turma nao cursa este curso — ausencia legitima
            if status != 200 or not texto:
                a.aviso(f"{curso}/{turma}: calendario respondeu HTTP {status}")
                continue
            achou_algum = True

            try:
                calendario = parse_calendario(texto)
            except CalendarioValidationError as exc:
                a.fatal(
                    f"{curso}/{turma}: calendario NAO parseia: {exc}",
                    "a turma inteira leva 502 calendario_unavailable a cada "
                    "submissao. Atencao nos ids numericos de TD: `1.1:` sem "
                    "aspas e lido como float e derruba o arquivo — use "
                    '`"1.1":`.',
                )
                continue

            if calendario.turma != turma:
                a.fatal(
                    f"{curso}/{turma}: campo `turma` diz {calendario.turma!r} "
                    f"mas o arquivo se chama {turma}.yaml",
                    "o backend acha o arquivo pelo NOME e confia no campo; "
                    "divergindo, a matricula vai para a turma errada.",
                )

            a.ok(
                f"{curso}/{turma}: calendario parseia",
                f"{len(calendario.janelas)} exercicio(s)",
            )

            for eid, janela in sorted(calendario.janelas.items()):
                # Listar exercicio que responde 404 matricula a turma em algo
                # que nao existe: o aluno recebe `exercise_not_found` num id
                # que o calendario afirma ser dele.
                status_yaml, _ = http_get(f"{base.rstrip('/')}/{eid}.yaml")
                if status_yaml != 200:
                    a.fatal(
                        f"{curso}/{turma}/{eid}: o calendario lista o "
                        f"exercicio mas o YAML dele responde HTTP "
                        f"{status_yaml} na main",
                        "publique o YAML antes de listar o exercicio, ou "
                        "remova a linha do calendario.",
                    )
                if janela.fecha is not None and janela.fecha < agora:
                    dias = (agora - janela.fecha).days
                    # Aviso: prazo vencido e estado legitimo depois da aula.
                    # Reportado porque prazo do semestre ANTERIOR num exercicio
                    # aberto foi o que marcou toda a aula 1 de IA com 107 dias
                    # de atraso sem disparar nada.
                    a.aviso(
                        f"{curso}/{turma}/{eid}: prazo venceu ha {dias} dia(s) "
                        f"({janela.fecha.isoformat()})",
                        "toda submissao daqui pra frente e gravada como "
                        "`late`, e o campo nunca e recalculado.",
                    )

    if not achou_algum:
        a.aviso(
            "nenhum calendario encontrado para as turmas do roster",
            "confira EXERCISES_BASE_URL* e se os `turmas/*.yaml` estao na main.",
        )


def main(argv: list[str]) -> int:
    alvo = argv[1] if len(argv) > 1 else "tudo"
    if alvo not in {"tudo", "roster", "calendario"}:
        print(__doc__)
        return 2

    bases = bases_por_curso()
    if not bases:
        print("  [ERRO] nenhuma EXERCISES_BASE_URL* definida no ambiente")
        return 2
    print("bases por curso:")
    for curso, base in sorted(bases.items()):
        print(f"  {curso:<6} {base}")

    a = Achados()
    entradas = None
    if alvo in {"tudo", "roster"}:
        entradas = audit_roster(a, bases)

    if alvo in {"tudo", "calendario"}:
        if entradas is None and alvo == "tudo":
            # Roster recusado: sem a lista de turmas não há o que auditar, e
            # inventar uma lista esconderia que a causa está no roster.
            print("\n  (calendarios nao auditados: o roster nao parseou)")
        else:
            if entradas is None:
                status, csv_texto = http_get(os.environ.get("ROSTER_URL", ""))
                entradas = parse_roster(csv_texto) if status == 200 else {}
            turmas = sorted(
                {t for e in entradas.values() for t in e.turmas}  # type: ignore[attr-defined]
            )
            audit_calendarios(a, bases, turmas)

    secao("Resumo")
    print(f"  fatais: {len(a.fatais)}   avisos: {len(a.avisos)}")
    if a.fatais:
        print("\n  Ha aluno bloqueado (ou a caminho de ficar). Achados fatais:")
        for msg in a.fatais:
            print(f"    - {msg}")
        return 1
    print("  Nada fatal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
