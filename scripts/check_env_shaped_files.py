#!/usr/bin/env python3
"""check_env_shaped_files.py — veta arquivos "env-shaped" no commit/PR.

Por que existe: o vazamento de credenciais deste repo veio de arquivos de
ambiente *dotless* (`env`, `env.local`, `env.sandbox`) — que escaparam da
intuição "procure por `.env`" e do `.gitignore` da época. gitleaks pega
*conteúdo* de segredo; este guard pega o *padrão de nome de arquivo*, fechando
o modo de falha exato, independente do conteúdo.

Quando usar:
  - pre-commit (hook local) e CI (gate server-side, não-burlável) recebem a
    lista de arquivos staged/alterados como argumentos.

Contrato (consumido por pre-commit e CI):
  exit 0  -> nenhum arquivo env-shaped proibido
  exit 1  -> há arquivo env-shaped; nomes vão para stderr (acionável)

Uso:
  python ops/secrets/check_env_shaped_files.py FILE [FILE ...]
"""
import os
import re
import sys

# Sufixos de template são versionáveis (não contêm valores reais).
TEMPLATE_SUFFIXES = {"example", "sample", "template", "dist"}
# Extensões de código/config: um arquivo `env.py`/`env.json` não é um .env.
CODE_EXTS = {
    "py", "js", "ts", "tsx", "jsx", "go", "rb", "sh", "md", "txt",
    "json", "yaml", "yml", "toml", "cfg", "ini", "lock", "xml", "html", "css",
}

_ENV_DOTTED = re.compile(r"^\.?env\.([A-Za-z0-9_.-]+)$")


def is_env_shaped(basename: str) -> bool:
    """True se o basename for um arquivo de ambiente que NÃO deve ser versionado."""
    if basename in ("env", ".env"):
        return True
    m = _ENV_DOTTED.match(basename)
    if not m:
        return False
    last = m.group(1).rsplit(".", 1)[-1].lower()
    if last in TEMPLATE_SUFFIXES:   # .env.example, env.sample
        return False
    if last in CODE_EXTS:           # env.py, env.json
        return False
    return True                     # env.local, env.sandbox, .env.production, .env.local.bak


def find_offenders(paths):
    """Filtra os caminhos cujo basename é env-shaped proibido."""
    return [p for p in paths if is_env_shaped(os.path.basename(p))]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    offenders = find_offenders(argv)
    if offenders:
        sys.stderr.write(
            "ERRO: arquivo(s) de ambiente não podem ser versionados "
            "(use Secret Manager / .env local gitignored):\n"
        )
        for p in offenders:
            sys.stderr.write(f"  - {p}\n")
        sys.stderr.write(
            "Se for um template, renomeie para '*.example'. "
            "Valores reais -> ops/secrets/secrets_push.sh.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
