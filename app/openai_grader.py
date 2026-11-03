"""Contrato HTTP dos avaliadores GPT-6 Luna, sem SDK adicional."""

from typing import Any

OPENAI_MODEL = "gpt-6-luna"
OPENAI_API_URL = "https://api.openai.com/v1/responses"
OPENAI_TIMEOUT_SECONDS = 30


def build_payload(prompt: str, name: str, properties: dict[str, Any]) -> dict[str, Any]:
    """Saída curta e raciocínio low para respeitar o prazo do autograder."""
    return {
        "model": OPENAI_MODEL,
        "input": prompt,
        "store": False,
        "reasoning": {"effort": "low"},
        "max_output_tokens": 4096,
        "text": {"format": {
            "type": "json_schema",
            "name": name,
            "strict": True,
            "schema": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        }},
    }


def response_text(body: dict[str, Any]) -> str:
    """Recusa incompletude/refusal; ignora itens de raciocínio na resposta REST."""
    if body.get("status") != "completed":
        raise ValueError("resposta OpenAI incompleta")
    parts = []
    for item in body.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "refusal":
                raise ValueError("recusa OpenAI")
            if part.get("type") == "output_text":
                parts.append(part["text"])
    if not parts:
        raise ValueError("resposta OpenAI sem texto")
    return "".join(parts)
