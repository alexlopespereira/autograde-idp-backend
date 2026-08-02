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

Ao adicionar curso: prefixo de 2–8 letras minúsculas, YAMLs nomeados com o id completo (`ia-1.1.yaml`, `exercicio: "ia-1.1"`), env var nova nos 3 lugares de sempre, e — se reaproveitar exercício com evidência shell — registrar o id qualificado em `app/evidence/shell.py:_WHITELIST`. O CLI espelha tudo em `autograde_idp/curso.py`.

Deploy: `gcloud builds submit --config=cloudbuild.yaml --substitutions=...`. CI workflow `cloud-run-deploy.yml` é **`workflow_dispatch` only** — não habilitar `on: push` sem confirmação (turma ativa). Quando atualizar deploy, manter `cloudbuild.yaml` e `.github/workflows/cloud-run-deploy.yml` em sync (env vars + secrets espelhados).

## Arquitetura

### Pipeline de avaliação (endpoints.py)

`/grade-preview` e `/submissions` compartilham `_validate_and_grade`:

```
load_exercise(id) → curso.split_exercise_id(id) → base URL do curso → fetch YAML → parse_exercise_yaml
  → checa janela (disponivel_a_partir_de) e turma
  → parse_repo_url + checa owner == user.github_username
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
2. Carrega roster (CSV publicado da Roster Sheet, cache TTL 300s em `app.roster._CACHE`).
3. Email não no roster → `403 not_in_roster`.
4. Anexa `request.state.user = AuthenticatedUser(google, roster)` e `request.state.correlation_id`. Toda resposta inclui header `X-Correlation-Id`.

Endpoints downstream sempre leem `request.state.user.email` / `.github_username` / `.turma` / `.roster.nome` — **nunca** confiar em campos do body pra identidade.

### OAuth proxy (app/oauth_proxy.py)

Backend faz proxy do `/token` do Google (Device Flow) pra não vazar `GOOGLE_OAUTH_CLIENT_SECRET` nas máquinas dos alunos. CLI bate em `/device/code` direto no Google (só client_id) e em `/oauth/exchange` + `/oauth/refresh` aqui no backend.

### Persistência (app/sheets_writer.py, app/roster_writer.py)

Dois writers, contratos diferentes — **não unificar** (decisão consciente no prd.json US-02).

- `SheetsWriter` → tabs `submissoes` (20 colunas, schema em `COLUMNS`) e `previews` (3 colunas, pra rate-limit do preview-com-Gemini). Idempotência por `submission_id` (lê coluna B antes de append). Telemetria de row-count antes/depois detecta `SHEETS_DROP_DETECTED` (Sheets API às vezes silenciosamente perde appends). `asyncio.Lock` module-level serializa appends — funciona porque Cloud Run roda `--max-instances=1` (um único event loop).
- `RosterWriter` → escreve `nome` e `github_username` na Roster Sheet via `POST /me/profile`. **Anti-hijacking**: só atualiza célula se está vazia. `valueInputOption='RAW'` (string `=BAR()` não vira fórmula). Retorna `ProfileUpdateResult(updated, skipped)`. Endpoint invalida `app.roster._clear_cache()` pós-update.

Auth pra Sheets: `google.auth.default()` (ADC). Em produção usa SA do Cloud Run; em dev usa `gcloud auth application-default login`.

### Rate limit (endpoints.py)

3 tentativas/dia + 30s cooldown por (email, exercicio). Aplicado em `/submissions` (sempre) e em `/grade-preview` (somente quando vem com respostas — disparar Gemini é caro). Reset à **meia-noite local America/Sao_Paulo** (pedagogicamente intuitivo; daí `tzdata` no `pyproject.toml` pra CI Windows). Emails em `RATE_LIMIT_BYPASS_EMAILS` (CSV, lido a cada call → hot-update) pulam tudo.

## Princípio: conteúdo de exercício mora no YAML

Qualquer estrutura específica de exercício (paths de artefatos, comandos shell, rubrica) vive no YAML em `idp_governodigital/exercicios/<id>.yaml` — **nunca** em tabelas hardcoded no backend ou CLI. Mudança no exercício acontece via PR no `idp_governodigital`, sem release de backend nem CLI. Seções já consumidas:

- `criterios:` — rubrica (despachada por `app/grader.py` ao primitive registrado em `app/primitives/`).
- `perguntas:` — perguntas open-ended graded pelo Gemini (`app/gemini.py`).
- `artefatos:` — `[{role, path, required}]`. CLI lê pra saber quais arquivos coletar (`autograde_idp/evidence/artifacts.py:specs_from_yaml`); backend referencia `role` em `criterios.args` dos primitives `evidence.artifacts.*` e `judge.artifacts.*`.
- `comandos_shell:` — `[[cmd, arg, ...]]` com placeholder `{owner_repo}`. CLI executa (`autograde_idp/evidence/shell.py:commands_from_yaml`); backend valida `cmd_joined` por string-equality contra a lista substituída (`app/evidence/shell.py:_expand_whitelist`).

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
