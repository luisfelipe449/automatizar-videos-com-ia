"""Groq Llama chat → validated `Script` (Phase 3 of the pipeline).

Single responsibility: take a user prompt, call Groq's chat.completions with
JSON structured output, validate into a `Script`, and return it. Transient
network/API failures are handled by `default_retry`; a Pydantic/JSON parse
failure triggers exactly one regeneration attempt with a corrective system
message before giving up.
"""

from __future__ import annotations

import json
from pathlib import Path

from groq import Groq
from pydantic import ValidationError

from app.config import GROQ_MODEL, require
from app.models import Script
from app.utils import filepaths
from app.utils.retries import default_retry

_LOG_PREFIX = "[script_service]"

# Keep the schema tight — the model works best with a compact, explicit contract.
_SYSTEM_PROMPT = """Você é um roteirista especializado em YouTube Shorts em português (PT-BR).

Responda APENAS com JSON válido, sem texto adicional, seguindo EXATAMENTE este schema:

{
  "title": "string (≤100 caracteres)",
  "description": "string terminando com #Shorts",
  "tags": ["3 a 8 tags relevantes, 1-30 chars cada"],
  "narration": "string com a narração completa em português",
  "scenes": [
    {
      "id": 1,
      "scene_text": "texto curto da cena",
      "image_prompt": "prompt visual detalhado e cinematográfico",
      "duration_seconds": 4
    }
  ]
}

Regras obrigatórias:
- Entre 3 e 8 cenas, com ids contíguos começando em 1.
- sum(duration_seconds) deve ficar entre 40 e 58 segundos (nunca acima de 58).
- narration em português, ≤150 palavras, tom dinâmico para maximizar retenção.
- title ≤100 caracteres, atrativo, sem clickbait enganoso.
- description termina obrigatoriamente com #Shorts.
- tags: lista com 3 a 8 itens, cada um entre 1 e 30 caracteres.
- image_prompt: detalhado, cinematográfico, formato vertical 9:16, SEM texto ou legendas na imagem, SEM rostos reconhecíveis de pessoas reais.
- NÃO inclua o campo job_id no JSON; ele é injetado pelo código.
"""


class ScriptGenerationError(RuntimeError):
    """Script generation failed after the validation retry."""


@default_retry
def _call_groq(client: Groq, messages: list[dict]) -> str:
    """Invoke Groq chat completions and return the raw JSON string content.

    Wrapped in `default_retry` so transient HTTP/5xx errors are retried with
    backoff. Deterministic failures (e.g. 400 bad request) propagate as-is.
    """
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        response_format={"type": "json_object"},
        messages=messages,
        temperature=0.7,
        max_tokens=2000,
    )
    return response.choices[0].message.content or ""


def _parse_and_validate(raw_json: str, job_id: str) -> Script:
    """Parse `raw_json`, inject `job_id`, return a validated `Script`.

    Raises `json.JSONDecodeError` or `pydantic.ValidationError` on failure.
    """
    payload = json.loads(raw_json)
    if not isinstance(payload, dict):
        # Treat non-object JSON as a parse failure so the retry path kicks in.
        raise json.JSONDecodeError(
            f"Expected top-level JSON object, got {type(payload).__name__}",
            raw_json,
            0,
        )
    payload["job_id"] = job_id
    return Script.model_validate(payload)


def _persist(script: Script) -> None:
    """Write `script.json` to disk so the orchestrator can cache-skip later."""
    filepaths.ensure_job_dirs(script.job_id)
    path: Path = filepaths.script_path(script.job_id)
    path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
    print(f"{_LOG_PREFIX} wrote {path}")


def generate_script(prompt: str, *, job_id: str | None = None) -> Script:
    """Generate a YouTube Shorts script from a user prompt.

    If `job_id` is None, derives one via `filepaths.make_job_id(prompt)`.
    Raises `ScriptGenerationError` on unrecoverable failure.
    """
    resolved_job_id = job_id or filepaths.make_job_id(prompt)
    client = Groq(api_key=require("GROQ_API_KEY"))

    base_messages: list[dict] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Tema do Short: {prompt}"},
    ]

    # Attempt 1 — pristine prompt.
    first_error: Exception | None = None
    try:
        raw = _call_groq(client, base_messages)
        script = _parse_and_validate(raw, resolved_job_id)
        _persist(script)
        return script
    except (ValidationError, json.JSONDecodeError) as exc:
        first_error = exc
        print(f"{_LOG_PREFIX} first attempt invalid ({type(exc).__name__}); retrying once")

    # Attempt 2 — feed the error back so the model can self-correct.
    retry_messages = base_messages + [
        {
            "role": "system",
            "content": (
                "A tentativa anterior foi inválida. Erro: "
                f"{first_error}. Gere novamente, respeitando ESTRITAMENTE o "
                "schema e as regras. Responda APENAS com JSON válido."
            ),
        }
    ]
    try:
        raw = _call_groq(client, retry_messages)
        script = _parse_and_validate(raw, resolved_job_id)
        _persist(script)
        return script
    except (ValidationError, json.JSONDecodeError) as exc:
        print(f"{_LOG_PREFIX} retry also invalid ({type(exc).__name__}); aborting")
        raise ScriptGenerationError(
            f"Script generation failed twice. first_error={first_error!r}; "
            f"second_error={exc!r}"
        ) from exc
