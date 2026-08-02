"""Curso como dimensão de primeira classe, derivada do id do exercício.

Motivação: o autograder passou a servir mais de um curso (Transformação
Digital e Agentes de IA) contra a MESMA Submissions Sheet. Os dois cursos
têm exercícios com a mesma numeração (``1.1``, ``1.2``…), então o id precisa
ser globalmente único — senão ``/me/grades``, que agrega por ``exercicio``,
funde a nota do ``1.1`` de um curso com a do outro.

Solução: o id carrega o curso como prefixo (``ia-1.1``). Isso resolve três
coisas de uma vez:

1. Agregação: ``ia-1.1`` != ``1.1``, então o rollup por exercício já fica
   correto sem tocar na lógica.
2. Roteamento: o prefixo diz de qual repositório de YAMLs buscar o
   exercício (:func:`exercises_base_url`).
3. Auditoria: a coluna ``curso`` na Sheet é derivada daqui, não digitada.

Compatibilidade: id SEM prefixo é o curso legado ``td`` (Transformação
Digital). Todas as submissões históricas da Sheet — e todos os YAMLs em
``idp_governodigital`` — continuam válidos sem renomeação.
"""

from __future__ import annotations

import os
import re

# Curso implícito quando o id não tem prefixo. Preserva o histórico:
# `1.1` sempre foi Transformação Digital.
CURSO_DEFAULT = "td"

# Prefixo: 2-8 letras minúsculas seguidas de hífen. O id base precisa começar
# com dígito, o que torna o split não-ambíguo (nenhum id base pode ser
# confundido com prefixo de curso).
_EXERCISE_ID_RE = re.compile(r"^(?:(?P<curso>[a-z]{2,8})-)?(?P<base>\d[\w.]*)$")


class CursoError(Exception):
    """Id de exercício malformado ou curso sem base URL configurada."""


def split_exercise_id(exercicio_id: str) -> tuple[str, str]:
    """``"ia-1.1"`` → ``("ia", "1.1")``; ``"1.1"`` → ``("td", "1.1")``.

    Levanta :class:`CursoError` se o id não casar com o formato — melhor
    falhar explícito do que buscar uma URL montada com lixo.
    """
    match = _EXERCISE_ID_RE.match((exercicio_id or "").strip())
    if match is None:
        raise CursoError(f"id de exercício inválido: {exercicio_id!r}")
    return (match.group("curso") or CURSO_DEFAULT), match.group("base")


def curso_of(exercicio_id: str) -> str:
    """Só o curso — açúcar para quem não precisa do id base."""
    return split_exercise_id(exercicio_id)[0]


def qualify_exercise_id(curso: str, base_id: str) -> str:
    """Inverso de :func:`split_exercise_id`: ``("ia", "1.2")`` → ``"ia-1.2"``.

    O curso default sai SEM prefixo, preservando os ids legados.
    """
    return base_id if curso == CURSO_DEFAULT else f"{curso}-{base_id}"


def exercises_base_url(curso: str) -> str:
    """Base URL dos YAMLs do ``curso``, via env var.

    Precedência: ``EXERCISES_BASE_URL_<CURSO>`` (ex.: ``EXERCISES_BASE_URL_IA``)
    e, se ausente, o ``EXERCISES_BASE_URL`` genérico. O genérico sozinho
    mantém o deployment atual funcionando sem nenhuma env var nova.

    O arquivo buscado usa o id COMPLETO (``ia-1.1.yaml``), então apontar os
    dois cursos para a mesma base também funciona — a separação por repo é
    conveniência, não requisito.
    """
    specific = os.environ.get(f"EXERCISES_BASE_URL_{curso.upper()}")
    if specific:
        return specific.rstrip("/")
    generic = os.environ.get("EXERCISES_BASE_URL")
    if generic:
        return generic.rstrip("/")
    raise CursoError(
        f"nem EXERCISES_BASE_URL_{curso.upper()} nem EXERCISES_BASE_URL definidas"
    )
