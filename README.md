# autograde-idp-backend

Backend FastAPI do **Autograder IDP-TD** — juiz independente, stateless, escreve resultados em Google Sheets. Roda no Cloud Run.

CLI cliente: [autograde-idp](https://github.com/alexlopespereira/autograde-idp).

---

## Endpoints principais

| Método | Path | Descrição |
|---|---|---|
| GET | `/healthz` | Health check (público). |
| GET | `/turmas` | Lista turmas disponíveis pra auto-registro. Só Google ID Token (não exige roster). |
| POST | `/me/register` | Auto-registro: aluno autenticado fora do roster informa `github_username` + `turma`. |
| POST | `/grade-preview` | Avalia exercício sem persistir. Requer Google ID Token + roster. |
| POST | `/submissions` | Idempotente; persiste nota na Submissions Sheet. |
| GET | `/me/grades` | Lista notas do aluno autenticado. |
| GET | `/me/identity` | Identidade do aluno (email, nome, turma). |

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
| `GITHUB_PAT` | GitHub PAT classic | sim — secret |
| `ROSTER_URL` | Roster Sheet (CSV export público) — leitura | não |
| `ROSTER_SHEET_ID` | Roster Sheet ID — escrita via Sheets API (auto-registro) | não |
| `ROSTER_SHEET_TAB` | Nome da aba do roster (default `roster`) | não |
| `TURMAS_DISPONIVEIS` | Turmas vírgula-separadas (ex: `TD-2026-01,TD-2026-02`) — habilita auto-registro | não |
| `SHEET_ID` | Submissions Sheet | não |
| `EXERCISES_BASE_URL` | URL base dos `*.yaml` | não |

`EXERCISES_BASE_URL` aponta para o raw GitHub onde os exercícios moram. Default sugerido:
`https://raw.githubusercontent.com/alexlopespereira/assistente-aulas/main/autograde/exercicios`.

### Auto-registro de alunos (fluxo emergencial)

Para habilitar que alunos fora do roster façam login e se cadastrem sozinhos:

1. Set `ROSTER_SHEET_ID` (ID da planilha Sheet — escrita) e `TURMAS_DISPONIVEIS` (lista vírgula-separada).
2. Service Account do Cloud Run precisa ter permissão de **Editor** na Roster Sheet (compartilhar manualmente).
3. Aba do roster deve ter header `email, nome, turma, github_username` em A1:D1 (ou o que for `ROSTER_SHEET_TAB`).
4. CLI/cliente chama `GET /turmas` → exibe lista → aluno escolhe → `POST /me/register` com `{github_username, turma}`. Email e nome vêm do ID Token Google.

Limites: rejeita re-registro de email já presente (409), valida formato GitHub
username (alfanumérico + hífen, 1-39 chars), exige `turma` ∈ `TURMAS_DISPONIVEIS`.
Após append, cache do roster (TTL 5min) é invalidado pra próxima request resolver o usuário.

---

## Licença

MIT.
