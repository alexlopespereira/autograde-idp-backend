# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

FastAPI backend (`autograde-backend`) que serve de juiz independente para o autograder IDP-TD. Stateless por design — não tem banco; todo estado vai pra Google Sheets. Roda no Cloud Run (`southamerica-east1`, `--max-instances=1 --concurrency=200`). Cliente CLI vive em [autograde-idp](https://github.com/alexlopespereira/autograde-idp).

## Comandos

```bash
pip install -e ".[dev]"                      # instala backend + dev extras
pytest -q --ignore=tests/e2e                 # 174 testes unit (Linux/macOS/Windows)
pytest tests/test_roster.py -v               # roda um único módulo
pytest tests/test_roster.py::test_xyz        # roda um único teste
pytest tests/e2e                             # smoke E2E — exige instalar autograde-idp CLI
ruff check .                                 # lint (line-length=100, py311)
docker build -t autograde-backend:local .    # build container local
```

Variáveis de ambiente obrigatórias (ver README.md): `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GITHUB_PAT`, `ROSTER_URL`, `SHEET_ID`, `EXERCISES_BASE_URL`. Opcionais: `ROSTER_SHEET_ID` (necessário pra `POST /me/profile`), `RATE_LIMIT_BYPASS_EMAILS` (CSV), `EXERCISES_BASE_URL_<CURSO>` (ex.: `EXERCISES_BASE_URL_IA` — base por curso, ver "Multi-curso").

## Multi-curso (app/curso.py)

Um deployment e uma Submissions Sheet servem N cursos. O curso é derivado do **prefixo do id do exercício**: `ia-1.1` → curso `ia`; sem prefixo → `td` (Transformação Digital, legado). Isso resolve de uma vez a agregação (`/me/grades` agrega por `exercicio`, e `ia-1.1` != `1.1`), o roteamento (`EXERCISES_BASE_URL_<CURSO>` → `EXERCISES_BASE_URL`) e a coluna `curso` (T) da Sheet, que é derivada — não digitada.

Ao adicionar curso: prefixo de 2–8 letras minúsculas, YAMLs nomeados com o id completo (`ia-1.1.yaml`, `exercicio: "ia-1.1"`), env var nova nos 3 lugares de sempre, e — se reaproveitar exercício **legado** com evidência shell — registrar o id qualificado em `app/evidence/shell.py:_WHITELIST`. Exercício novo não precisa: declare `comandos_shell:` no YAML e a whitelist sai dali (`_WHITELIST` é só o fallback dos que já estão no ar). O CLI espelha tudo em `autograde_idp/curso.py`.

Deploy: `gcloud builds submit --config=cloudbuild.yaml --substitutions=...`. CI workflow `cloud-run-deploy.yml` é **`workflow_dispatch` only** — não habilitar `on: push` sem confirmação (turma ativa). Quando atualizar deploy, manter `cloudbuild.yaml` e `.github/workflows/cloud-run-deploy.yml` em sync (env vars + secrets espelhados).

## Arquitetura

### Pipeline de avaliação (endpoints.py)

`/grade-preview` e `/submissions` compartilham `_validate_and_grade`:

```
load_exercise(id) → curso.split_exercise_id(id) → base URL do curso → fetch YAML → parse_exercise_yaml
  → resolve cronograma: calendário da turma (app/calendario.py) ou, se não houver, o YAML legado
  → checa janela (abre) e matrícula
  → se requer_repositorio (exceção, declarada): parse_repo_url + checa owner == user.github_username
    (default false: owner_repo="" e nada do GitHub abaixo roda)
  → validate_shell_evidence (whitelist por exercício, clock-skew ±30min)
  → github_client.collect_evidence (PyGithub + retry rate-limit)
  → grader.grade → percorre exercise.criterios e despacha cada um pro primitive registrado
```

Se exercício tem `perguntas` (open-ended), `_grade_with_gemini` chama `app.gemini.grade_respostas` e os resultados viram `CriterioResult` extras anexados ao `Bulletin` via `_append_gemini_to_bulletin`. `/submissions` exige `respostas` se há perguntas; `/grade-preview` retorna o array de `perguntas` pra CLI quando vem sem respostas (prompt flow).

### Primitives (app/primitives/)

Sistema de plugin via registry: `register("name")` decora `(args, evidence) → CriterioResult`. Import de `app.primitives.__init__` dispara registro dos 4 módulos: `evidence_artifacts`, `evidence_shell`, `github`, `judge_llm`. YAML do exercício referencia primitives por `check:` string. `grader.grade` injeta `_peso` em `args` automaticamente. `CriterioResult.degraded=True` sinaliza fallback (ex: Gemini fora → nota cheia provisória) e propaga pra `bulletin.judge_degraded`.

### Auth (app/auth.py)

`AuthMiddleware` (Starlette) intercepta tudo exceto `PUBLIC_PATHS = {"/healthz", "/oauth/exchange", "/oauth/refresh"}`. Fluxo:

1. Verifica `Authorization: Bearer <id_token>` via `google.oauth2.id_token` com audience = `GOOGLE_OAUTH_CLIENT_ID`.
2. Carrega roster (CSV publicado da Roster Sheet, cache TTL 300s em `app.roster._CACHE`). O dict é indexado pela **conta**, não pelo aluno: a coluna `email` aceita N contas separadas por `;` (mesmo separador da coluna `turma`) e cada uma aponta pro **mesmo** `RosterEntry`.
3. Email não no roster → `403 not_in_roster`. Foi a falha mais frequente de setembro/2026 (5 alunos em 4 dias) e nunca por email errado: o aluno estava cadastrado com a conta **institucional** e logava com a **pessoal**, ou o contrário. Daí o multi-conta — o conserto não é escolher uma, é listar as duas. A **primeira** conta da célula é a canônica: é ela que vai pro `user.email`, pra Submissions Sheet, pro `reqctx` e pro log. Consequência operacional: ao acrescentar conta, **anexe no fim**; reordenar a célula reescreve a identidade do aluno e órfã as notas já gravadas (`/me/grades` e o rate-limit somam todas as contas, então elas continuam sendo lidas — mas a coluna `email` da planilha passa a divergir). `user.emails` expõe a tupla completa pra quem precisa comparar contra dado histórico.
4. Anexa `request.state.user = AuthenticatedUser(google, roster)` e `request.state.correlation_id`. Toda resposta inclui header `X-Correlation-Id`.

Endpoints downstream sempre leem `request.state.user.email` / `.github_username` / `.turma` / `.roster.nome` — **nunca** confiar em campos do body pra identidade.

### OAuth proxy (app/oauth_proxy.py)

Backend faz proxy do `/token` do Google (Device Flow) pra não vazar `GOOGLE_OAUTH_CLIENT_SECRET` nas máquinas dos alunos. CLI bate em `/device/code` direto no Google (só client_id) e em `/oauth/exchange` + `/oauth/refresh` aqui no backend.

### Persistência (app/sheets_writer.py, app/roster_writer.py)

Dois writers, contratos diferentes — **não unificar** (decisão consciente no prd.json US-02).

- `SheetsWriter` → tabs `submissoes` (20 colunas, schema em `COLUMNS`) e `previews` (3 colunas, pra rate-limit do preview-com-Gemini). Idempotência por `submission_id` (lê coluna B antes de append). Telemetria de row-count antes/depois detecta `SHEETS_DROP_DETECTED` (Sheets API às vezes silenciosamente perde appends). `asyncio.Lock` module-level serializa appends — funciona porque Cloud Run roda `--max-instances=1` (um único event loop).
- `RosterWriter` → escreve `nome` e `github_username` na Roster Sheet via `POST /me/profile`. **Anti-hijacking**: só atualiza célula se está vazia. `get_row_index` casa a conta contra `split_emails(row[0])`, não contra a célula inteira: comparando a célula, `autograde perfil` levantaria `UserNotInRoster` exatamente para o aluno que precisou do multi-conta. `valueInputOption='RAW'` (string `=BAR()` não vira fórmula). Retorna `ProfileUpdateResult(updated, skipped)`. Endpoint invalida `app.roster._clear_cache()` pós-update.

Auth pra Sheets: `google.auth.default()` (ADC). Em produção usa SA do Cloud Run; em dev usa `gcloud auth application-default login`.

### Rate limit (endpoints.py)

3 tentativas/dia + 30s cooldown por (email, exercicio). Aplicado em `/submissions` (sempre) e em `/grade-preview` (somente quando vem com respostas — disparar Gemini é caro). Reset à **meia-noite local America/Sao_Paulo** (pedagogicamente intuitivo; daí `tzdata` no `pyproject.toml` pra CI Windows). Emails em `RATE_LIMIT_BYPASS_EMAILS` (CSV, lido a cada call → hot-update) pulam tudo.

## Calendário por turma (app/calendario.py)

**Prazo e matrícula moram na turma, não no exercício.** O YAML do exercício é
conteúdo pedagógico; quem diz "esta turma cursa este exercício, com estas
datas" é `<base do curso>/turmas/<TURMA>.yaml`:

```yaml
turma: IA-2026-01
padrao:
  abre:  2026-09-01T00:00:00-03:00
  fecha: 2026-10-20T23:59:59-03:00
exercicios:
  ia-1.1:                              # linha vazia = herda o padrão
  ia-3.1:
    fecha: 2026-10-27T23:59:59-03:00   # exceção explícita
```

O schema antigo tinha `turmas:` (plural) com `disponivel_a_partir_de` e
`prazo:` (singulares) no mesmo arquivo. Reaproveitar um exercício numa turma
nova obrigava a **sobrescrever** as datas da anterior — dez arquivos por
semestre. Em 2026-09 a conta chegou: aulas 3-5 foram atualizadas, aula 1 ficou
no calendário anterior, e toda submissão de `ia-1.1` foi gravada com 107 dias
de atraso até um aluno escrever. Abrir turma agora custa um arquivo;
reaproveitar exercício, uma linha; e o calendário da turma que terminou fica
imutável.

Estar listado em `exercicios:` **é** a matrícula — por isso a entrada vazia é a
forma canônica. `abre` bloqueia (`exercise_not_open_yet`); `fecha` só marca
`late`. Sem `abre` (nem no `padrao`) o arquivo é recusado: assumir "abre
sempre" seria inventar a intenção do professor num campo que tranca aluno.

`_resolver_cronograma` (endpoints.py) tenta o calendário e cai no YAML legado
quando ele existe — a janela entre o deploy do backend e a publicação dos
calendários é real, e um backend novo precisa atender exercício não migrado.
Calendário indisponível **sem** legado vira `502 calendario_unavailable`, nunca
`403 turma_not_eligible`: falha nossa não pode chegar ao aluno como "você não
está na turma". Cache de 300s por URL (`get_or_fetch`, o mesmo do roster), o
que torna uma turma inteira submetendo um GET e não duzentos.

Ao abrir turma: crie `turmas/<TURMA>.yaml` nos repos de conteúdo dos cursos
que ela cursa e preencha a coluna `turma` do roster com o mesmo id (aceita
mais de um, separado por `;`). `driver.py roster` mostra o que o backend lê.

## Princípio: conteúdo de exercício mora no YAML

Qualquer estrutura específica de exercício (paths de artefatos, comandos shell, rubrica) vive no YAML em `idp_governodigital/exercicios/<id>.yaml` — **nunca** em tabelas hardcoded no backend ou CLI. Mudança no exercício acontece via PR no `idp_governodigital`, sem release de backend nem CLI. Seções já consumidas:

- `criterios:` — rubrica (despachada por `app/grader.py` ao primitive registrado em `app/primitives/`).
- `perguntas:` — perguntas open-ended graded pelo Gemini (`app/gemini.py`).
- `artefatos:` — `[{role, path, required}]`. CLI lê pra saber quais arquivos coletar (`autograde_idp/evidence/artifacts.py:specs_from_yaml`); backend referencia `role` em `criterios.args` dos primitives `evidence.artifacts.*` e `judge.artifacts.*`.
- `requer_repositorio:` — booleano, **default `false`**: a regra é que o aluno não versiona a solução e não ganha ponto por versionar. Com `false` o CLI não lê `remote.origin.url` nem manda `repo_url`, e o backend pula `parse_repo_url`, a checagem `repo_owner_mismatch`, o `collect_evidence` e o `_collect_first_commits`. **`true` é a exceção e precisa ser declarada** — use só onde o repo **é** o objeto de aprendizado (aula 1; hoje também ia-3.1). `parse_exercise_yaml` recusa o YAML que depende do repo (`check: github.*` ou `{owner_repo}`) sem declarar `true`, e a mensagem diz qual dos dois consertos se aplica — a contradição viraria nota zero silenciosa. Sem repo, a identidade vem do login Google + roster; para amarrar também à conta GitHub, declare `gh auth status` em `comandos_shell:` e um critério `evidence.shell.gh_auth_ok`, que compara o usuário do `gh` com o `github_username` do roster sem precisar de repositório.
- `comandos_shell:` — `[[cmd, arg, ...]]` ou `[{cmd, extract, timeout}]`, com placeholder `{owner_repo}`. `extract` é a chave em `evidence['shell']['commands']`; `timeout` é em segundos (teto: `MAX_TIMEOUT_SECONDS`). CLI executa (`autograde_idp/evidence/shell.py:commands_from_yaml`), e só binários da allowlist `YAML_BINARIOS_PERMITIDOS` — o YAML vem da internet e alimenta `subprocess.run`; backend valida `cmd_joined` por string-equality contra a lista substituída (`app/evidence/shell.py:_expand_whitelist`).

CLI baixa o YAML direto do raw GitHub via `autograde_idp/exercicio_spec.py:fetch_exercise_spec` (env `AUTOGRADE_EXERCISES_BASE_URL`, default = idp_governodigital/main/exercicios). Mesmo URL que o backend já usa em `EXERCISES_BASE_URL` — fonte única, sem mediação.

Quando precisar adicionar nova categoria de "conteúdo específico do exercício", primeira pergunta: **isso pode ser uma nova seção do YAML?** Se sim, estender o schema (`curriculum.py:_parse_*`) e expor pro CLI via mesmo padrão. Não criar tabela `dict[exercise_id, ...]` em código.

## Convenções

- Idioma: docstrings/comentários em PT-BR no domínio (grader, primitives, roster, endpoints); módulos puramente técnicos (auth, oauth_proxy, sheets_writer) misturam EN. Mensagens de erro pra cliente em PT-BR. Códigos `error:` machine-readable em snake_case EN.
- Validação YAML: `CurriculumValidationError` é a exceção do domínio de exercícios; primitives nunca raise — capturam exceção e devolvem `CriterioResult(passed=False, ..., degraded=False)`.
- Dataclasses frozen pra tudo que é "modelo" (`Exercise`, `Criterio`, `Pergunta`, `RosterEntry`, `Bulletin`, `CriterioResult`, `SubmissionRow`, `GoogleUser`, `AuthenticatedUser`).
- Bumps de versão: contrato HTTP muda → minor bump em `pyproject.toml` (ex: `0.2.0 → 0.3.0` adicionou `/me/profile` e campo `github_username` em `/me/identity`).
- Quando adicionar env var nova: atualizar **3 lugares** — `cloudbuild.yaml` (`substitutions` + `--set-env-vars`), `.github/workflows/cloud-run-deploy.yml` (mesmo `--set-env-vars`), e seção "Variáveis de ambiente" do `README.md`.

## Testes

`pytest-asyncio` com `asyncio_mode = "auto"`. Testes E2E vivem em `tests/e2e/` (excluídos do ruff e da matrix unit). CI matrix Linux/macOS/Windows roda só os unit; E2E é Ubuntu-only e instala o CLI a partir de `git+https://github.com/alexlopespereira/autograde-idp.git@main`.

Mocks de Sheets API: usar `Mock(spec=Resource)` do `googleapiclient.discovery`. Pra HTTP externo (roster fetch, exercise YAML): injetar `fetcher: Callable[[str], str]` em `fetch_roster` / `fetch_exercise` — não monkey-patchar requests.
