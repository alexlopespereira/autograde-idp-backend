# HANDOFF — Mitigação do timeout do `/grade-preview` (deploy pendente)

> Sessão de 2026-05-30. Continue daqui em outro host. **Nenhum segredo neste
> doc** — substitutions e secrets são recuperados localmente (ver §4).

## 1. Problema que originou a sessão

`autograde validar 2.1` falhava com:

```
erro de rede em /grade-preview: HTTPSConnectionPool(... run.app, port=443):
Read timed out. (read timeout=30)
```

**Diagnóstico (confirmado):** não era rede nem cold start (`/healthz` responde em
~135ms). O `/grade-preview` roda o grader completo; para o exercício 2.1 são ~7
judges LLM (`judge.artifacts.*`) sobre artefatos grandes, **executados em série**
→ 40–90s, acima do read-timeout de 30s hardcoded no CLI.

## 2. O que já foi feito e MERGEADO em `main`

| Repo | PR | commit em main | Conteúdo |
|---|---|---|---|
| `autograde-idp-backend` | [#19](https://github.com/alexlopespereira/autograde-idp-backend/pull/19) | `aecdac2` | **Cura de raiz:** `grade()` avalia criterios concorrentemente (`ThreadPoolExecutor`). Latência cai de Σ(judges) para ~max(judge). Ordem do boletim e contrato `Bulletin` preservados. Teto via env `GRADER_MAX_WORKERS` (default 8). |
| `autograde-idp` (CLI) | [#16](https://github.com/alexlopespereira/autograde-idp/pull/16) | `1927d08` | **Paliativo:** `_post` usa `timeout=(10, 180)` + override `AUTOGRADE_HTTP_TIMEOUT`. |

Verificação: backend `tests/test_grader.py` 8/8 (TDD red→green com `threading.Barrier`);
CLI 155/155; ruff limpo nos dois.

**Estado prático:**
- CLI (paliativo) já está **ativo** onde o `autograde` é install editable apontando
  pro working tree em `main` — então `validar` já aguenta os ~40–90s.
- Backend (cura de raiz) está em `main` mas **NÃO deployado** → prod ainda roda
  judges em série.

## 3. TAREFA PENDENTE — deploy do backend no Cloud Run

Sem o deploy, o backend em produção continua sequencial (mitigado só pelo timeout
maior do cliente). Para publicar a paralelização:

```bash
cd autograde-idp-backend           # já em main com aecdac2
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=\
_GOOGLE_OAUTH_CLIENT_ID=<...>,\
_ROSTER_URL=<...>,\
_SHEET_ID=<...>,\
_ROSTER_SHEET_ID=<...>,\
_RATE_LIMIT_BYPASS_EMAILS=<...>
# _EXERCISES_BASE_URL já tem default no cloudbuild.yaml
```

- **Secrets** (`GITHUB_PAT`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GEMINI_API_KEY`) já vêm
  do **Secret Manager** via `--update-secrets` no `cloudbuild.yaml` — não precisa
  passá-los. Não os coloque na linha de comando.
- Região: `southamerica-east1`. Serviço: `autograde-backend`.
- Referência completa: `docs/setup.md` (§ deploy, linhas ~150–160 e checklist ~271).

### 3.1 Cuidado importante
As substitutions de `cloudbuild.yaml` têm **default vazio** (`""`). Se rodar o build
**sem** passar `_GOOGLE_OAUTH_CLIENT_ID`/`_ROSTER_URL`/`_SHEET_ID`/`_ROSTER_SHEET_ID`,
o `--set-env-vars` vai **apagar** essas envs no serviço. Sempre passe os 4.

## 4. Como obter as substitutions no outro host (sem segredo no git)

Os valores estão no seu `.env` local (gitignored) **ou** podem ser lidos do serviço
que já está no ar:

```bash
gcloud run services describe autograde-backend --region=southamerica-east1 \
  --format='value(spec.template.spec.containers[0].env)'
```

Isso devolve `GOOGLE_OAUTH_CLIENT_ID`, `ROSTER_URL`, `SHEET_ID`, `ROSTER_SHEET_ID`,
`EXERCISES_BASE_URL`, `RATE_LIMIT_BYPASS_EMAILS` da revisão atual — reuse-os como
substitutions (`_`-prefixadas) no comando do §3.

## 5. Validação pós-deploy

```bash
# 1. healthz vivo
curl -s -o /dev/null -w "http=%{http_code} total=%{time_total}s\n" \
  https://autograde-backend-1065810445001.southamerica-east1.run.app/healthz
# (nota: /healthz hoje responde 404 rápido — server up; rota a confirmar)

# 2. preview cronometrado do 2.1 (deve completar < ~35s agora)
cd <repo-do-exercicio-2.1> && time autograde validar 2.1
```

Se houver muitos alunos simultâneos e o Gemini retornar 429, baixar o teto de
threads sem redeploy de código: `gcloud run services update autograde-backend
--region=southamerica-east1 --update-env-vars=GRADER_MAX_WORKERS=4`.

## 6. Limites conhecidos (Feynman)

1. Paralelizar **não** remove o cap de 30s por chamada (`GEMINI_TIMEOUT_SECONDS`).
   Um judge isolado >30s ainda degrada (score 1.0 provisório).
2. Rate-limit do Gemini é o novo gargalo sob concorrência alta → `GRADER_MAX_WORKERS`.
3. Cloud Run `--concurrency=200` + até 8 threads/req: threads são I/O-bound, ok,
   mas vigie o uso de memória sob pico.

## 7. Outras pendências desta sessão (fora do deploy)

- **Repo `exercicio2-1`** (submissão do aluno) — dois riscos de rubrica detectados,
  **não corrigidos**:
  - `.autograde-exercise` AUSENTE (tutorial exige marcador com `2.1`).
  - `B_relatorio_assistente_v2.md` ≡ `v3.md` byte-a-byte (sha256 igual) →
    critério **B1** zera e tende a derrubar **B12** (−10 pts). A v3 parece ter sido
    salva como cópia da v2.
- **`test/Ex21/`** (no container `~/Projects/idp`, não é repo git) — gerados nesta
  sessão: `C_mapa_atores.md` (mermaid + RACI, 12 atores, propósito = demanda falha
  do canal telefônico) e `C_grill_transcript.md` (9 rodadas). Já commitados/pushados
  no repo `exercicio2-1` em commit anterior (`ff7cfb2`).

## 8. Arquivos tocados (referência)

- `app/grader.py` — paralelização (este repo, em main).
- `tests/test_grader.py` — testes de concorrência + ordem.
- `autograde-idp/autograde_idp/validar.py` — timeout configurável (outro repo, em main).
