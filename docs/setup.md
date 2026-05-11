# Setup do professor — Autograder IDP-TD (backend)

Esta página documenta as etapas que o **professor** precisa executar uma única vez antes da disciplina começar. Aluno não precisa de nada disso.

Decisões de arquitetura estão em [`autograder-design.md`](https://github.com/alexlopespereira/assistente-aulas/blob/main/autograde/autograder-design.md). Requisitos originais em [`autograder-requirements.md`](https://github.com/alexlopespereira/assistente-aulas/blob/main/autograde/autograder-requirements.md).

Status de cada etapa abaixo:

- ✅ = feito (evidência local existe)
- ⏳ = pendente (responsabilidade humana, não automatizável)

---

## 1. ✅ Criar GCP project

Projeto criado: **`autograde-314802`**.

Para reproduzir do zero:

1. Acessar [console.cloud.google.com](https://console.cloud.google.com).
2. Criar novo projeto chamado `autograde` (ou nome equivalente).
3. Anotar o `PROJECT_ID` e o `PROJECT_NUMBER`.

---

## 2. ✅ Criar OAuth Client (Device Flow)

Cliente criado: **`Autograde IDP-TD CLI`** tipo *TVs and Limited Input devices* (Device Code Flow).

> ⚠️ **Nunca commitar `.env`/`.env.local`** com secrets. Salvar credenciais em variável de ambiente local ou Secret Manager.

Para reproduzir do zero:

1. Em [console.cloud.google.com](https://console.cloud.google.com) → APIs & Services → Credentials → Create Credentials → OAuth Client ID.
2. Application type = **TVs and Limited Input devices**.
3. Anotar `client_id` e `client_secret`.
4. Em OAuth consent screen, configurar app como **External** + **Production** (necessário para Device Flow funcionar fora do test-user list).
5. Adicionar escopos: `openid`, `email`, `profile`.

---

## 3. ⏳ Criar GitHub PAT classic

Token de leitura para o backend bater na API do GitHub (sobe rate limit de 60 → 5000 req/h por hora; decisão em `autograder-design.md §3`).

1. Acessar [github.com/settings/tokens](https://github.com/settings/tokens) → *Generate new token (classic)*.
2. Nome: `autograde-backend-readonly`.
3. Expiration: 1 ano (renovar a cada semestre).
4. **Scopes**: somente `public_repo` (suficiente porque repos dos alunos são públicos — §2 do design).
5. Copiar o token (formato `ghp_…`) e **salvar como secret no Cloud Run** (`GITHUB_PAT`). **Não** commitar.

---

## 4. ⏳ Criar Roster Sheet pública

Planilha do Google Sheets contendo o roster da turma. Acesso público por link (decisão LGPD em `autograder-design.md §3`).

Schema obrigatório (uma linha por aluno, primeira linha é header):

| email                       | nome     | turma         | github_username |
|-----------------------------|----------|---------------|-----------------|
| ana.silva@aluno.idp.edu.br  | Ana Silva | TD-2026-01    | anasilva        |

1. Criar nova Sheet chamada `Autograde Roster TD-2026`.
2. Compartilhar como *anyone with the link can view*.
3. Copiar o `SHEET_ID` da URL (`docs.google.com/spreadsheets/d/<SHEET_ID>/edit`).
4. Salvar como variável de ambiente do backend: `ROSTER_URL=https://docs.google.com/spreadsheets/d/<SHEET_ID>/export?format=csv&gid=0`.

---

## 5. ⏳ Criar Submissions Sheet

Planilha onde o backend grava cada submissão (append-only, audit trail das notas).

Schema sugerido:

| timestamp_iso | email | exercicio | nota | bulletin_json |
|---|---|---|---|---|

1. Criar nova Sheet chamada `Autograde Submissions TD-2026`.
2. **Não pública**: só Service Account terá Editor (passo 6).
3. Copiar o `SHEET_ID` para `SHEET_ID` no Cloud Run.

---

## 6. ⏳ Criar Service Account com Editor na Submissions Sheet

A SA é quem escreve na Submissions Sheet sem expor credenciais.

1. GCP Console → IAM & Admin → Service Accounts → Create service account.
2. Nome: `autograde-submissions-writer`.
3. Não atribuir roles no projeto (a permissão é granular na planilha).
4. Copiar o email da SA (`autograde-submissions-writer@autograde-314802.iam.gserviceaccount.com`).
5. Na Submissions Sheet → Share → adicionar a SA como **Editor**.

---

## 7. ⏳ Anexar Service Account ao Cloud Run service

Faz o backend autenticar como a SA sem precisar de JSON key no disco.

```bash
gcloud run services update autograde-backend \
  --region=southamerica-east1 \
  --service-account=autograde-submissions-writer@autograde-314802.iam.gserviceaccount.com
```

Ao subir Cloud Run a primeira vez, use o cloudbuild.yaml deste repo (já no root):

```bash
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=_GOOGLE_OAUTH_CLIENT_ID=...,_ROSTER_URL=...,_SHEET_ID=...
```

---

## 8. ⏳ (Opcional) Workload Identity Federation para CI/CD

Para habilitar deploy automático via `.github/workflows/cloud-run-deploy.yml` em push pra `main`:

1. Criar Workload Identity Pool + Provider:
   ```bash
   gcloud iam workload-identity-pools create github --location=global
   gcloud iam workload-identity-pools providers create-oidc github-provider \
     --location=global --workload-identity-pool=github \
     --issuer-uri=https://token.actions.githubusercontent.com \
     --attribute-mapping=google.subject=assertion.sub,attribute.repository=assertion.repository
   ```

2. Permitir o repo a impersonar a SA de deploy:
   ```bash
   gcloud iam service-accounts add-iam-policy-binding \
     autograde-deploy@autograde-314802.iam.gserviceaccount.com \
     --role=roles/iam.workloadIdentityUser \
     --member="principalSet://iam.googleapis.com/projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github/attribute.repository/alexlopespereira/autograde-idp-backend"
   ```

3. Adicionar GitHub secrets ao repo:
   - `GCP_WORKLOAD_IDENTITY_PROVIDER`: `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github/providers/github-provider`
   - `GCP_SERVICE_ACCOUNT`: `autograde-deploy@autograde-314802.iam.gserviceaccount.com`
   - `GCP_PROJECT_ID`: `autograde-314802`
   - `GCP_REGION`: `southamerica-east1`
   - `GOOGLE_OAUTH_CLIENT_ID`, `ROSTER_URL`, `SHEET_ID`: valores reais.

4. Editar `.github/workflows/cloud-run-deploy.yml` e habilitar `on: push: branches: [main]`.

---

## Resumo: variáveis que o backend precisa em runtime

| Variável                        | Origem (passo) | Sensível |
|---------------------------------|----------------|----------|
| `GOOGLE_OAUTH_CLIENT_ID`        | 2              | não      |
| `GOOGLE_OAUTH_CLIENT_SECRET`    | 2              | sim — secret |
| `GITHUB_PAT`                    | 3              | sim — secret |
| `ROSTER_URL`                    | 4              | não      |
| `SHEET_ID`                      | 5              | não      |
| `EXERCISES_BASE_URL`            | curriculum     | não — aponta pro raw do `assistente-aulas` |

Credenciais sensíveis ficam em **Cloud Run secrets** (Secret Manager), nunca em `.env` versionado.

---

## Checklist final de provisionamento

Use esta lista no dia em que for ligar a turma. Marque (`[x]`) cada item à medida que conclui — a presença do artefato indicado é a prova. Se algum item permanecer `[ ]`, o backend não sobe ou rejeita login.

- [x] **GCP project** `autograde-314802` criado (passo 1).
- [x] **OAuth Client (Device Flow)** tipo *TVs and Limited Input devices* configurado em estado **Production** (passo 2).
- [ ] **GitHub PAT classic** com escopo `public_repo` criado e salvo como secret `GITHUB_PAT` no Cloud Run (passo 3).
- [ ] **Roster Sheet pública** criada e `ROSTER_URL` (formato `…/export?format=csv&gid=0`) anotado como env var do Cloud Run (passo 4).
- [ ] **Submissions Sheet privada** criada e `SHEET_ID` anotado (passo 5).
- [ ] **Service Account** `autograde-submissions-writer@autograde-314802.iam.gserviceaccount.com` criada e adicionada como **Editor** na Submissions Sheet (passo 6).
- [ ] **Service Account anexada ao Cloud Run service** via `--service-account=...` (passo 7).
- [ ] **`gcloud builds submit --config=cloudbuild.yaml`** executado com substitutions completas e `--update-secrets=GITHUB_PAT=...` (passo 7).
  *Prova:* `curl https://autograde-backend-<hash>.a.run.app/healthz` responde `{"status":"ok"}`.

> **Smoke test pós-deploy:** depois do último item, peça a um aluno do roster para rodar `autograde login` + `autograde validar 1.1 --auto-submit` num repo de teste. Se a Submissions Sheet receber a row e `autograde notas` listar a tentativa, o pipeline está saudável ponta-a-ponta. O test E2E local (`tests/e2e/test_smoke.py`) valida o mesmo fluxo sem custo de cloud.
