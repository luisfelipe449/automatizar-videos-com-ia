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

# Single-pass prompt: LLM returns the full script AND one visual prompt per fact.
# Style: "viral facts" English Shorts — fast, hooky, CTA-peppered, cinematic visuals.
_SYSTEM_PROMPT = """You are a scriptwriter for viral English-language YouTube Shorts.

Your output is a single valid JSON object — no prose, no markdown fences, no trailing text — matching EXACTLY this schema:

{
  "title": "string (≤100 chars, hook-style, no misleading clickbait)",
  "description": "string ending with #Shorts",
  "tags": ["3 to 8 relevant tags, 1-30 chars each, English"],
  "narration": "the full narration, English, casual conversational tone",
  "scenes": [
    {
      "id": 1,
      "scene_text": "the exact line of narration this scene covers (one fact or one structural beat)",
      "image_prompt": "detailed cinematic visual prompt for this beat",
      "duration_seconds": 5
    }
  ]
}

The user will give you a topic. Build a viral facts video around it, following this structure:

1. Opening Hook: attention-grabbing statement or question that sparks curiosity.
2. Intro Line: e.g. "Here are 10 facts to mess your brain" or "Here are 9 crazy facts you didn't know".
3. Fact List: 8 to 12 interesting facts. State each fact clearly and concisely. Occasionally append parentheticals like "(Try this!)" or "(Try it!)" where applicable. Vary fact types across biology, history, geography, psychology, physics, animals, culture — mix well-known with lesser-known.
4. CTA Insertions: sprinkle natural CTAs throughout, e.g. "if you want to be smarter every day — like and subscribe", "studies show that if you like and subscribe, you may become smarter".
5. Emotional Hooks: include dramatic phrases like "the last one amazed me", "the last will blow your mind", "you didn't know this".
6. Variety: cover diverse categories — physical/biological (organs, DNA, animals), geographic (landmarks, locations), historical (alphabet, traditions), interactive challenges, psychological/cultural.
7. Closing: end with an emotional statement or reference to the most impressive fact.

For each scene — one scene per fact plus one scene for each structural beat (hook, intro, CTAs, closing) — build a visual prompt with these rules:
- cinematic, realistic style
- vertical 9:16 framing
- match the emotion of the line
- keep characters/subjects consistent across scenes when they recur
- NO text, captions, watermarks, or logos in the image
- NO recognisable faces of real people

Hard numeric constraints:
- scenes: between 9 and 13 items, ids contiguous starting at 1 (one per fact + structural beats).
- sum(duration_seconds) must be between 45 and 58 (never above 58). Shorts cap is 60; we leave a buffer.
- Each scene duration_seconds between 3 and 8 — fast pace maximises retention.
- narration: English, 90 to 140 words total, casual conversational pacing for quick narration.
- title ≤ 100 chars.
- description must end with "#Shorts".
- tags: 3 to 8 items, each 1 to 30 chars.
- DO NOT include a job_id field — it is injected by the pipeline.
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
        # 9-13 scenes × (scene_text + cinematic image_prompt) + narration
        # + tags/title/description: comfortably inside 4096.
        max_tokens=4096,
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
        {"role": "user", "content": f"Topic for the Short: {prompt}"},
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
                "The previous attempt was invalid. Error: "
                f"{first_error}. Regenerate the JSON, STRICTLY obeying the "
                "schema and the rules. Respond with JSON only."
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
