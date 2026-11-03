# autograde-idp-backend

Backend FastAPI do **Autograder IDP-TD** — juiz independente, stateless, escreve resultados em Google Sheets. Roda no Cloud Run.

CLI cliente: [autograde-idp](https://github.com/alexlopespereira/autograde-idp).

---

## Endpoints principais

| Método | Path | Descrição |
|---|---|---|
| GET | `/healthz` | Health check (público). |
| POST | `/grade-preview` | Avalia exercício sem persistir. Requer Google ID Token. |
| POST | `/submissions` | Idempotente; persiste nota na Submissions Sheet. |
| GET | `/me/grades` | Lista notas do aluno autenticado. |

Detalhes em `app/endpoints.py` e `autograder-design.md` (no [assistente-aulas](https://github.com/alexlopespereira/assistente-aulas/blob/main/autograde/autograder-design.md)).

---

## Desenvolvimento

```bash
pip install -e ".[dev]"
pytest -q --ignore=tests/e2e
ruff check .
```

CI (matrix Linux/macOS/Windows) roda os 174 testes unit em PRs. O job e2e (`pytest tests/e2e`) instala adicionalmente o CLI [autograde-idp](https://github.com/alexlopespereira/autograde-idp) e roda smoke ponta-a-ponta — só em Ubuntu.

### Build local Docker

```bash
docker build -t autograde-backend:local .
docker run --rm -p 8080:8080 \
  -e GOOGLE_OAUTH_CLIENT_ID=... \
  -e ROSTER_URL=... \
  -e SHEET_ID=... \
  -e EXERCISES_BASE_URL=https://raw.githubusercontent.com/alexlopespereira/assistente-aulas/main/autograde/exercicios \
  autograde-backend:local
curl http://localhost:8080/healthz
```

---

## Deploy

Detalhes completos em [`docs/setup.md`](docs/setup.md) (provisionamento OAuth, Sheets, GitHub PAT, Service Account).

```bash
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=_GOOGLE_OAUTH_CLIENT_ID=...,_ROSTER_URL=...,_SHEET_ID=...
```

CI deploy automático em push pra `main` está em `.github/workflows/cloud-run-deploy.yml`. Hoje é `workflow_dispatch` apenas — habilitar `on: push` quando Workload Identity Federation estiver configurado (instruções no setup.md).

---

## Variáveis de ambiente

| Variável | Origem | Sensível |
|---|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | OAuth Client (Device Flow) | não |
| `GOOGLE_OAUTH_CLIENT_SECRET` | OAuth Client | sim — secret |
| `OPENAI_API_KEY` | Secret Manager `OPENAI_API_KEY:latest` (avaliação de respostas e artefatos) | sim — secret |
| `GEMINI_API_KEY` | Secret Manager `gemini-api-key:latest` (geração SQL) | sim — secret |
| `GITHUB_PAT` | GitHub PAT classic | sim — secret |
| `ROSTER_URL` | Roster Sheet pública | não |
| `SHEET_ID` | Submissions Sheet | não |
| `EXERCISES_BASE_URL` | URL base dos `*.yaml` (fallback de qualquer curso) | não |
| `EXERCISES_BASE_URL_IA` | URL base dos `*.yaml` do curso de Agentes de IA | não |

`EXERCISES_BASE_URL` aponta para o raw GitHub onde os exercícios moram:
`https://raw.githubusercontent.com/alexlopespereira/idp_governodigital/main/exercicios`.

### Multi-curso

O id do exercício carrega o curso como prefixo: `ia-1.1` → curso `ia`; sem
prefixo → curso `td` (Transformação Digital, o legado). O backend resolve a
base URL por curso, na ordem `EXERCISES_BASE_URL_<CURSO>` → `EXERCISES_BASE_URL`
(ver `app/curso.py`). Para adicionar um curso novo:

1. Escolher um prefixo de 2–8 letras minúsculas (ex.: `ia`).
2. Nomear os YAMLs com o id completo — `ia-1.1.yaml`, com `exercicio: "ia-1.1"`.
3. Setar `EXERCISES_BASE_URL_<PREFIXO_MAIÚSCULO>` apontando pro repo dos YAMLs
   (3 lugares: `cloudbuild.yaml`, `.github/workflows/cloud-run-deploy.yml` e
   esta tabela).
4. Se o curso reaproveitar exercícios com evidência shell, registrar os ids
   qualificados em `app/evidence/shell.py:_WHITELIST`.

Por que o prefixo e não só uma coluna `curso`: `/me/grades` agrega por
`exercicio`, então dois cursos com um `1.1` cada fundiriam as notas do aluno.
A coluna `curso` (T) na Submissions Sheet é derivada do prefixo e serve pra
filtro/pivot — não é a fonte da unicidade.

---

### Modelo dos avaliadores

Respostas discursivas e artefatos por rubrica usam `gpt-6-luna` na OpenAI
Responses API, com raciocínio `low`, JSON Schema estrito, `store=false`,
limite de 4096 tokens de saída (incluindo raciocínio) e timeout de 30s.
`gpt-6.1-luna` não é o identificador publicado do modelo.
A geração de SQL continua usando `gemini-2.5-flash-lite`.

O módulo `app/gemini.py` e o tipo `GeminiResult` mantêm seus nomes por
compatibilidade. Falhas do avaliador continuam sinalizadas como degradadas,
com nota máxima provisória; incompletude e recusa nunca viram nota definitiva.
Não há fallback automático para outro modelo.

Antes de ativar: validar a chave e acesso ao modelo, testar ambos os avaliadores
com respostas sintéticas e confirmar `ok=True`; só então publicar no Cloud Run.
A service account do Cloud Run precisa de `roles/secretmanager.secretAccessor`
no secret `OPENAI_API_KEY`.

## Licença

MIT.
