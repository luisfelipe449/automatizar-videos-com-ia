"""LLM → validated `Script` (Phase 3 of the pipeline).

Single responsibility: take a user prompt, call an LLM provider with JSON
structured output, validate into a `Script`, and return it. Provider is
selectable via `LLM_PROVIDER` (`groq` default, or `bedrock` for Anthropic
Claude on AWS Bedrock — useful when you want more creative scripts).
Transient network/API failures are handled by `default_retry`; a
Pydantic/JSON parse failure triggers exactly one regeneration attempt with a
corrective system message before giving up.
"""

from __future__ import annotations

import json
from pathlib import Path

from groq import Groq
from pydantic import ValidationError

from app import config
from app.config import BEDROCK_MODEL, GROQ_MODEL, require
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
      "duration_seconds": 5,
      "rank": null,
      "rank_title": null
    }
  ]
}

Each scene MAY optionally have `rank` (integer 1-100) and `rank_title` (string, ≤40 chars) — use these ONLY when the topic is a ranking (e.g. "Top 10 X", "Greatest Y of all time", "Strongest Z"). Count DOWN: the first ranked scene is the highest number, the last ranked scene is #1 (creates suspense). Only the fact scenes get ranks — the hook, intro line, CTAs, and closing are NOT ranked (leave both fields null). If the topic is NOT a ranking (e.g. "weird facts", "crazy curiosities"), leave `rank` and `rank_title` null in ALL scenes.

Example for a "Top 5 Greatest Brazilians of all time" topic (6-10 scenes total for rankings):
- Scene 1 (hook): rank=null, rank_title=null
- Scene 2 (intro "Here are the 5 greatest Brazilians ever..."): rank=null, rank_title=null
- Scene 3 (#5): rank=5, rank_title="Ayrton Senna"
- Scene 4 (#4): rank=4, rank_title="..."
- Scene 5 (#3): rank=3, rank_title="..."
- Scene 6 (#2): rank=2, rank_title="..."
- Scene 7 (#1): rank=1, rank_title="Pelé"
- Scene 8 (closing): rank=null, rank_title=null

Pick a rank count (3, 5, 7, 10) that fits the 6-10 total scene budget. For a "Top 10" request, prefer cutting to 7 ranked items to respect the duration budget rather than forcing all 10.

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

Hard numeric constraints (check these BEFORE returning your JSON):
- sum(duration_seconds) must be between 45 and 58. NEVER above 58. If your draft sums to more than 58, remove scenes or shorten durations until it fits. Add them up yourself before emitting the JSON.
- If the topic IS a ranking (Top N, Greatest, etc.) use 6 to 8 scenes total (hook + intro + 3-5 ranked facts + closing), with each scene 4 to 7 seconds. IMPORTANT: even if the user asks for "Top 10", you will narrow to the best 3-5 ranked items to fit the duration budget — quality over quantity. Reflect the narrowed count in the title and intro line (e.g. title "5 Greatest Brazilians" if you pick 5 ranked items).
- If the topic is NOT a ranking, use 9 to 13 scenes, each 3 to 8 seconds.
- Scene ids contiguous starting at 1.
- narration: English, 80 to 120 words total, casual conversational pacing.
- title ≤ 100 chars.
- description must end with "#Shorts".
- tags: 3 to 8 items, each 1 to 30 chars.
- DO NOT include a job_id field — it is injected by the pipeline.
"""


class ScriptGenerationError(RuntimeError):
    """Script generation failed after the validation retry."""


# Shared budget for all providers. 9-13 scenes × (scene_text + cinematic
# image_prompt) + narration + tags/title/description fits comfortably in 4096.
_MAX_TOKENS = 4096
_TEMPERATURE = 0.9  # bumped from 0.7 — "more creative scripts" was the trigger.


@default_retry
def _call_groq(client, messages: list[dict]) -> str:
    """Invoke Groq chat completions and return the raw JSON string content."""
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        response_format={"type": "json_object"},
        messages=messages,
        temperature=_TEMPERATURE,
        max_tokens=_MAX_TOKENS,
    )
    return response.choices[0].message.content or ""


@default_retry
def _call_bedrock(client, messages: list[dict]) -> str:
    """Invoke Anthropic Claude on AWS Bedrock and return the raw text content.

    Anthropic's API takes `system` as a top-level parameter, not a chat turn,
    so we extract the system messages from `messages` and merge them.
    """
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    user_turns = [m for m in messages if m["role"] != "system"]
    # Claude won't accept an empty messages list; defensive fallback.
    if not user_turns:
        user_turns = [{"role": "user", "content": "Generate the Short now."}]

    response = client.messages.create(
        model=BEDROCK_MODEL,
        system="\n\n".join(system_parts),
        messages=user_turns,
        max_tokens=_MAX_TOKENS,
        temperature=_TEMPERATURE,
    )
    # Claude responses are a list of content blocks; we want the text ones.
    parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    return "".join(parts)


def _build_client():
    """Construct the provider-specific SDK client once per generate_script call."""
    provider = config.LLM_PROVIDER
    if provider == "groq":
        return "groq", Groq(api_key=require("GROQ_API_KEY"))
    if provider == "bedrock":
        from anthropic import AnthropicBedrock

        # Bedrock SDK reads AWS_BEARER_TOKEN_BEDROCK from env automatically;
        # we require() here so the error is a clear MissingEnvVar if absent.
        require("AWS_BEARER_TOKEN_BEDROCK")
        return "bedrock", AnthropicBedrock(aws_region=config.AWS_REGION)
    raise ScriptGenerationError(
        f"Unknown LLM_PROVIDER={provider!r}. Set LLM_PROVIDER=groq or bedrock."
    )


def _call_llm(provider: str, client, messages: list[dict]) -> str:
    """Dispatch to the right provider call using a pre-built client."""
    if provider == "groq":
        return _call_groq(client, messages)
    if provider == "bedrock":
        return _call_bedrock(client, messages)
    raise ScriptGenerationError(f"Unknown provider={provider!r}")


def _strip_code_fences(text: str) -> str:
    """Return the inner JSON when the model wraps it in ```json fences.

    Claude on Bedrock sometimes adds ```json ... ``` even when asked not to.
    Groq's `response_format=json_object` prevents this, so this is a no-op
    for Groq output.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    # Drop the opening fence line and the closing fence.
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def _parse_and_validate(raw_json: str, job_id: str) -> Script:
    """Parse `raw_json`, inject `job_id`, return a validated `Script`.

    Raises `json.JSONDecodeError` or `pydantic.ValidationError` on failure.
    """
    payload = json.loads(_strip_code_fences(raw_json))
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
    provider, client = _build_client()
    print(f"{_LOG_PREFIX} provider={provider}")

    base_messages: list[dict] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Topic for the Short: {prompt}"},
    ]

    # Attempt 1 — pristine prompt.
    first_error: Exception | None = None
    try:
        raw = _call_llm(provider, client, base_messages)
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
        raw = _call_llm(provider, client, retry_messages)
        script = _parse_and_validate(raw, resolved_job_id)
        _persist(script)
        return script
    except (ValidationError, json.JSONDecodeError) as exc:
        print(f"{_LOG_PREFIX} retry also invalid ({type(exc).__name__}); aborting")
        raise ScriptGenerationError(
            f"Script generation failed twice. first_error={first_error!r}; "
            f"second_error={exc!r}"
        ) from exc
