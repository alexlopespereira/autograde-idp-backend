import pytest

from app import roster
from app.roster import (
    RosterEntry,
    RosterValidationError,
    fetch_roster,
    parse_roster,
)

HAPPY_CSV = (
    "email,nome,turma,github_username\n"
    "ana@idp.edu.br,Ana Silva,TD-2026-01,anasilva\n"
    "beto@idp.edu.br,Beto Souza,TD-2026-01,betosouza\n"
)


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    roster._clear_cache()
    yield
    roster._clear_cache()


def test_parse_roster_happy_path():
    result = parse_roster(HAPPY_CSV)
    assert len(result) == 2
    assert result["ana@idp.edu.br"] == RosterEntry(
        email="ana@idp.edu.br",
        nome="Ana Silva",
        turma="TD-2026-01",
        github_username="anasilva",
    )
    assert result["beto@idp.edu.br"].github_username == "betosouza"


def test_parse_roster_empty_raises():
    with pytest.raises(RosterValidationError, match="vazio"):
        parse_roster("")


def test_parse_roster_duplicate_email_raises():
    csv_text = (
        "email,nome,turma,github_username\n"
        "ana@idp.edu.br,Ana Silva,TD-2026-01,anasilva\n"
        "ana@idp.edu.br,Ana Outra,TD-2026-02,outraana\n"
    )
    with pytest.raises(RosterValidationError, match="duplicado.*ana@idp.edu.br"):
        parse_roster(csv_text)


def test_parse_roster_missing_required_column_raises():
    csv_text = "email,nome,turma\nana@idp.edu.br,Ana Silva,TD-2026-01\n"
    with pytest.raises(RosterValidationError, match="github_username"):
        parse_roster(csv_text)


def test_parse_roster_aceita_github_username_vazio():
    csv_text = (
        "email,nome,turma,github_username\n"
        "ana@idp.edu.br,Ana Silva,TD-2026-01,\n"
    )
    result = parse_roster(csv_text)
    assert result["ana@idp.edu.br"] == RosterEntry(
        email="ana@idp.edu.br",
        nome="Ana Silva",
        turma="TD-2026-01",
        github_username="",
    )


def test_parse_roster_aceita_nome_vazio():
    csv_text = (
        "email,nome,turma,github_username\n"
        "ana@idp.edu.br,,TD-2026-01,anasilva\n"
    )
    result = parse_roster(csv_text)
    assert result["ana@idp.edu.br"] == RosterEntry(
        email="ana@idp.edu.br",
        nome="",
        turma="TD-2026-01",
        github_username="anasilva",
    )


def test_parse_roster_rejeita_email_vazio():
    csv_text = (
        "email,nome,turma,github_username\n"
        ",Ana Silva,TD-2026-01,anasilva\n"
    )
    with pytest.raises(RosterValidationError, match="row 2.*email.*vazio"):
        parse_roster(csv_text)


def test_parse_roster_rejeita_turma_vazia():
    csv_text = (
        "email,nome,turma,github_username\n"
        "ana@idp.edu.br,Ana Silva,,anasilva\n"
    )
    with pytest.raises(RosterValidationError, match="row 2.*turma.*vazio"):
        parse_roster(csv_text)


def test_fetch_roster_caches_within_ttl(monkeypatch):
    calls = {"n": 0}

    def fetcher(url: str) -> str:
        calls["n"] += 1
        return HAPPY_CSV

    fake_clock = [1000.0]
    monkeypatch.setattr(roster, "_now", lambda: fake_clock[0])

    url = "https://example.com/roster.csv"

    # 1st call: fetcher invoked
    r1 = fetch_roster(url, fetcher=fetcher)
    assert calls["n"] == 1
    assert "ana@idp.edu.br" in r1

    # within TTL: cached
    fake_clock[0] += 100
    fetch_roster(url, fetcher=fetcher)
    assert calls["n"] == 1

    # just before TTL: still cached
    fake_clock[0] += 199  # total elapsed = 299
    fetch_roster(url, fetcher=fetcher)
    assert calls["n"] == 1

    # past TTL (300s): refetched
    fake_clock[0] += 2  # total elapsed = 301
    fetch_roster(url, fetcher=fetcher)
    assert calls["n"] == 2


# ---------- coluna `turma` com mais de uma turma -----------------------------


@pytest.mark.parametrize(
    "raw,esperado",
    [
        ("TD-2026-01", ("TD-2026-01",)),
        ("TD-2026-01;IA-2026-01", ("TD-2026-01", "IA-2026-01")),
        ("TD-2026-01, IA-2026-01", ("TD-2026-01", "IA-2026-01")),
        ("TD-2026-01|IA-2026-01", ("TD-2026-01", "IA-2026-01")),
        ("  TD-2026-01 ;; IA-2026-01  ", ("TD-2026-01", "IA-2026-01")),
        ("TD-2026-01;TD-2026-01", ("TD-2026-01",)),  # dedupe
        ("", ()),
    ],
)
def test_split_turmas(raw, esperado):
    assert roster.split_turmas(raw) == esperado


def test_roster_entry_exposes_turmas():
    entries = roster.parse_roster(
        "email,nome,turma,github_username\n"
        "aluno@idp.edu.br,Aluno,TD-2026-01;IA-2026-01,fulano\n"
    )
    entry = entries["aluno@idp.edu.br"]
    assert entry.turma == "TD-2026-01;IA-2026-01"  # coluna crua preservada
    assert entry.turmas == ("TD-2026-01", "IA-2026-01")


def test_roster_still_rejects_empty_turma():
    with pytest.raises(roster.RosterValidationError):
        roster.parse_roster(
            "email,nome,turma,github_username\naluno@idp.edu.br,Aluno,,fulano\n"
        )


# --- normalização de caixa ---------------------------------------------
# Regressão real: 21 dos 23 alunos de IA-2026-01 foram colados na planilha em
# CAIXA ALTA. O Google manda a claim `email` sempre minúscula, o middleware
# fazia `roster.get(email)` exato, e os 21 tomavam `403 not_in_roster` vendo o
# próprio email correto na mensagem de erro.


def test_parse_roster_normaliza_email_maiusculo():
    csv_text = (
        "email,nome,turma,github_username\n"
        "IGO@GMAIL.COM,Igo Costa,IA-2026-01,\n"
    )
    result = parse_roster(csv_text)
    assert "igo@gmail.com" in result
    assert "IGO@GMAIL.COM" not in result
    assert result["igo@gmail.com"].email == "igo@gmail.com"


def test_parse_roster_duplicata_difere_so_na_caixa_raises():
    """Antes do fix isto passava batido e criava DUAS entradas para a mesma
    pessoa — a segunda invisível para o login, a primeira invisível para a
    turma nova. Tem que estourar e forçar o merge na planilha."""
    csv_text = (
        "email,nome,turma,github_username\n"
        "ana@idp.edu.br,Ana Silva,TD-2026-01,anasilva\n"
        "ANA@IDP.EDU.BR,Ana Silva,IA-2026-01,\n"
    )
    with pytest.raises(RosterValidationError, match="duplicado.*ana@idp.edu.br"):
        parse_roster(csv_text)


@pytest.mark.parametrize(
    "raw,esperado",
    [
        ("  Ana@IDP.edu.br  ", "ana@idp.edu.br"),
        ("ANA@IDP.EDU.BR", "ana@idp.edu.br"),
        ("", ""),
    ],
)
def test_normalize_email(raw, esperado):
    from app.roster import normalize_email

    assert normalize_email(raw) == esperado


def test_normalize_email_preserva_ponto_e_plus():
    """Ponto e +tag NÃO são removidos: isso é regra do Gmail, não de email.
    Em `.gov.br` apagar ponto junta pessoas diferentes."""
    from app.roster import normalize_email

    assert normalize_email("A.B+turma@Presidencia.gov.br") == "a.b+turma@presidencia.gov.br"


# --- coluna `email` com mais de uma conta Google ----------------------------
# Incidente real (IA-2026-01, 16-18/09/2026): 5 alunos levaram
# `403 not_in_roster` com o email certo na tela. Estavam TODOS na planilha —
# com a conta institucional que a secretaria mandou, enquanto o `autograde
# login` deles saiu da conta pessoal. Exemplos medidos no histórico da
# planilha: `ricardo.c.costa@caixa.gov.br` vs `rick.palmeiras@uol.com.br`,
# `giovanni.cardoso@presidencia.gov.br` vs `giovannibrigido@gmail.com`,
# `tiago.cardoso@embrapii.org.br` vs `tiagocardosos@gmail.com`. Cadastrar uma
# conta só é apostar em qual delas o aluno vai usar.


@pytest.mark.parametrize(
    "raw,esperado",
    [
        ("ana@idp.edu.br", ("ana@idp.edu.br",)),
        (
            "ricardo.c.costa@caixa.gov.br;rick.palmeiras@uol.com.br",
            ("ricardo.c.costa@caixa.gov.br", "rick.palmeiras@uol.com.br"),
        ),
        ("a@x.br, b@y.com", ("a@x.br", "b@y.com")),
        ("a@x.br|b@y.com", ("a@x.br", "b@y.com")),
        # CAIXA ALTA e espaço NO MEIO da lista, não só nas pontas: a célula é
        # digitada à mão e foi exatamente assim que o incidente do CAIXA ALTA
        # entrou na planilha.
        ("  A@X.BR ;; B@Y.COM  ", ("a@x.br", "b@y.com")),
        ("a@x.br;A@X.BR", ("a@x.br",)),  # dedupe
        ("", ()),
        (";;", ()),
    ],
)
def test_split_emails(raw, esperado):
    assert roster.split_emails(raw) == esperado


def test_parse_roster_indexa_por_todas_as_contas():
    """O bug de 16/09 em uma linha: o aluno loga pela pessoal e tem que achar
    a linha cadastrada pela institucional."""
    result = parse_roster(
        "email,nome,turma,github_username\n"
        "ricardo.c.costa@caixa.gov.br;rick.palmeiras@uol.com.br,"
        "Ricardo Costa,IA-2026-01,rickpalmeiras-design\n"
    )
    institucional = result["ricardo.c.costa@caixa.gov.br"]
    pessoal = result["rick.palmeiras@uol.com.br"]
    # MESMA entrada, não uma cópia: duas identidades para a mesma pessoa é
    # justamente o que parte o histórico de notas em dois.
    assert institucional is pessoal
    assert institucional.turmas == ("IA-2026-01",)
    assert institucional.github_username == "rickpalmeiras-design"


def test_roster_entry_expoe_emails_com_canonica_primeiro():
    result = parse_roster(
        "email,nome,turma,github_username\n"
        "inst@caixa.gov.br;pessoal@uol.com.br,Aluno,IA-2026-01,fulano\n"
    )
    entry = result["pessoal@uol.com.br"]
    assert entry.emails == ("inst@caixa.gov.br", "pessoal@uol.com.br")
    # A canônica é a PRIMEIRA — é ela que vai para a planilha de submissões.
    assert entry.emails[0] == "inst@caixa.gov.br"


def test_parse_roster_conta_repetida_entre_linhas_raises():
    """Apelido de um aluno que é a conta canônica de outro: ninguém sabe qual
    linha manda, e o parser recusa antes de a nota ir para a pessoa errada."""
    csv_text = (
        "email,nome,turma,github_username\n"
        "ana@idp.edu.br,Ana,TD-2026-01,ana\n"
        "beto@idp.edu.br;ANA@IDP.EDU.BR,Beto,TD-2026-01,beto\n"
    )
    with pytest.raises(RosterValidationError, match="duplicado.*ana@idp.edu.br"):
        parse_roster(csv_text)


def test_parse_roster_rejeita_celula_de_email_so_com_separadores():
    """`;;` passa o check de 'não vazio' por strip e não produz conta nenhuma:
    sem este guarda a linha entraria no roster inalcançável por qualquer
    login."""
    csv_text = (
        "email,nome,turma,github_username\n"
        ";;,Ana Silva,TD-2026-01,anasilva\n"
    )
    with pytest.raises(RosterValidationError, match="row 2.*email.*vazio"):
        parse_roster(csv_text)


def test_parse_roster_uma_conta_por_linha_nao_muda_nada():
    """Regressão inversa: a planilha de hoje não tem apelido em linha nenhuma,
    e o `email` da entrada tem que continuar sendo o email simples."""
    result = parse_roster(HAPPY_CSV)
    assert result["ana@idp.edu.br"].email == "ana@idp.edu.br"
    assert result["ana@idp.edu.br"].emails == ("ana@idp.edu.br",)
