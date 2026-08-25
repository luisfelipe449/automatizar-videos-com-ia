"""Tests for `app.services.script_service` — no real Groq API is ever contacted."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------- #
# Fake Groq SDK
# --------------------------------------------------------------------------- #

class _FakeCompletions:
    """Mimics `client.chat.completions` with a scripted sequence of outputs.

    Each element of `responses` is either:
      - a str: treated as the `message.content` to return
      - an Exception instance: raised on that call
    """

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):  # noqa: D401 — SDK-shaped signature
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeCompletions exhausted")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        message = SimpleNamespace(content=item)
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice])


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class FakeGroq:
    """Captures constructor args and forwards chat calls to `_FakeCompletions`."""

    last_instance: "FakeGroq | None" = None

    def __init__(self, api_key: str):
        self.api_key = api_key
        # `responses` is pulled from class-level attribute set per test.
        self._completions = _FakeCompletions(type(self).responses)
        self.chat = _FakeChat(self._completions)
        FakeGroq.last_instance = self

    @property
    def calls(self) -> list[dict]:
        return self._completions.calls


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_VALID_PAYLOAD = {
    "title": "5 fatos insanos sobre buracos negros",
    "description": "Descoberta que vai te surpreender!",
    "tags": ["espaco", "ciencia", "curiosidades"],
    "narration": "Hoje você vai descobrir cinco fatos incríveis sobre buracos negros.",
    "scenes": [
        {"id": 1, "scene_text": "cena 1", "image_prompt": "prompt cinematográfico 1", "duration_seconds": 10},
        {"id": 2, "scene_text": "cena 2", "image_prompt": "prompt cinematográfico 2", "duration_seconds": 10},
        {"id": 3, "scene_text": "cena 3", "image_prompt": "prompt cinematográfico 3", "duration_seconds": 10},
    ],
}

_OVERLONG_PAYLOAD = {
    # Totals 75s — exceeds the 58s budget; should fail Script validation.
    **_VALID_PAYLOAD,
    "scenes": [
        {"id": 1, "scene_text": "a", "image_prompt": "p", "duration_seconds": 25},
        {"id": 2, "scene_text": "b", "image_prompt": "p", "duration_seconds": 25},
        {"id": 3, "scene_text": "c", "image_prompt": "p", "duration_seconds": 25},
    ],
}


@pytest.fixture
def patched_env(monkeypatch, tmp_path: Path):
    """Fake API key + redirected generated dir so tests stay hermetic.

    Also pins the provider to 'groq' so the test suite doesn't depend on
    whatever the developer has in their local .env.
    """
    monkeypatch.setenv("GROQ_API_KEY", "fake-key")
    monkeypatch.setattr("app.services.script_service.config.LLM_PROVIDER", "groq")
    generated = tmp_path / "generated"
    rendered = tmp_path / "rendered"
    # filepaths imports GENERATED_DIR / RENDERED_DIR at module load → patch there.
    monkeypatch.setattr("app.utils.filepaths.GENERATED_DIR", generated)
    monkeypatch.setattr("app.utils.filepaths.RENDERED_DIR", rendered)
    return generated


def _install_fake_groq(monkeypatch, responses: list):
    FakeGroq.responses = responses
    FakeGroq.last_instance = None
    monkeypatch.setattr("app.services.script_service.Groq", FakeGroq)


def _patch_no_backoff(monkeypatch):
    """Disable tenacity's sleep so retry-based tests run instantly."""
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_happy_path_writes_script_json(monkeypatch, patched_env):
    from app.services.script_service import generate_script

    _install_fake_groq(monkeypatch, [json.dumps(_VALID_PAYLOAD)])

    script = generate_script("buracos negros")

    assert script.title == _VALID_PAYLOAD["title"]
    assert len(script.scenes) == 3
    # job_id is injected by the service, not produced by the model.
    assert script.job_id.startswith("20")  # YYYY-...
    # File was persisted under the redirected generated dir.
    script_file = patched_env / script.job_id / "script.json"
    assert script_file.exists()
    on_disk = json.loads(script_file.read_text(encoding="utf-8"))
    assert on_disk["job_id"] == script.job_id
    # Exactly one call to Groq.
    assert len(FakeGroq.last_instance.calls) == 1


def test_video_title_forwarded_to_user_message(monkeypatch, patched_env):
    from app.services.script_service import generate_script

    _install_fake_groq(monkeypatch, [json.dumps(_VALID_PAYLOAD)])

    generate_script("topic text", video_title="My Awesome Title")

    messages = FakeGroq.last_instance.calls[0]["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")
    assert "Topic for the Short: topic text" in user_msg["content"]
    assert "VIDEO_TITLE: My Awesome Title" in user_msg["content"]


def test_video_title_defaults_to_prompt(monkeypatch, patched_env):
    from app.services.script_service import generate_script

    _install_fake_groq(monkeypatch, [json.dumps(_VALID_PAYLOAD)])

    generate_script("fallback prompt text")

    messages = FakeGroq.last_instance.calls[0]["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")
    assert "VIDEO_TITLE: fallback prompt text" in user_msg["content"]


def test_system_prompt_contains_viral_facts_structure(monkeypatch, patched_env):
    """Sanity check: the new system prompt exposes the 7-step structure
    and the VIDEO_TITLE hook so the LLM has clear instructions."""
    from app.services.script_service import _SYSTEM_PROMPT

    assert "VIDEO_TITLE" in _SYSTEM_PROMPT
    assert "Opening Hook" in _SYSTEM_PROMPT
    assert "Intro Line" in _SYSTEM_PROMPT
    assert "Fact List" in _SYSTEM_PROMPT
    assert "CTA" in _SYSTEM_PROMPT or "Call-to-Action" in _SYSTEM_PROMPT
    assert "Emotional Hooks" in _SYSTEM_PROMPT
    assert "Closing" in _SYSTEM_PROMPT
    # Ranking logic is gone.
    assert "rank_title" not in _SYSTEM_PROMPT
    assert "Top " not in _SYSTEM_PROMPT


def test_malformed_json_triggers_validation_retry(monkeypatch, patched_env):
    from app.services.script_service import generate_script

    _install_fake_groq(
        monkeypatch,
        ["not valid json {{{", json.dumps(_VALID_PAYLOAD)],
    )

    script = generate_script("tema qualquer", job_id="2026-04-30_tema_abc123")

    assert script.job_id == "2026-04-30_tema_abc123"
    # First call + exactly one validation retry = 2 calls.
    assert len(FakeGroq.last_instance.calls) == 2
    # The retry message should contain a corrective system turn.
    retry_messages = FakeGroq.last_instance.calls[1]["messages"]
    assert len(retry_messages) == 3
    assert retry_messages[-1]["role"] == "system"
    assert "invalid" in retry_messages[-1]["content"].lower()


def test_two_invalid_attempts_raise_script_generation_error(monkeypatch, patched_env):
    from app.services.script_service import ScriptGenerationError, generate_script

    _install_fake_groq(
        monkeypatch,
        [json.dumps(_OVERLONG_PAYLOAD), json.dumps(_OVERLONG_PAYLOAD)],
    )

    with pytest.raises(ScriptGenerationError, match="failed twice"):
        generate_script("tema ruim")

    assert len(FakeGroq.last_instance.calls) == 2


def test_transient_connection_error_is_retried_by_default_retry(monkeypatch, patched_env):
    _patch_no_backoff(monkeypatch)
    from app.services.script_service import generate_script

    # First network call raises → tenacity retries → second call succeeds.
    _install_fake_groq(
        monkeypatch,
        [ConnectionError("temporary DNS blip"), json.dumps(_VALID_PAYLOAD)],
    )

    script = generate_script("curiosidades")

    assert script.title == _VALID_PAYLOAD["title"]
    # Two underlying create() invocations: 1 failure + 1 success, no validation retry needed.
    assert len(FakeGroq.last_instance.calls) == 2


# --------------------------------------------------------------------------- #
# generate_angles tests
# --------------------------------------------------------------------------- #


def _angles_payload(n: int = 5) -> str:
    return json.dumps(
        {
            "angles": [
                {
                    "title": f"Angle {i} title",
                    "prompt": f"Prompt for angle {i} on the given theme.",
                }
                for i in range(1, n + 1)
            ]
        }
    )


def test_generate_angles_returns_n_distinct_angles(monkeypatch, patched_env):
    from app.services.script_service import generate_angles

    _install_fake_groq(monkeypatch, [_angles_payload(5)])

    angles = generate_angles("buracos negros", n=5)

    assert isinstance(angles, list)
    assert len(angles) == 5
    titles = {a["title"] for a in angles}
    assert len(titles) == 5  # all distinct
    for angle in angles:
        assert angle["title"]
        assert angle["prompt"]
    # Only one Groq call needed — the first response was valid.
    assert len(FakeGroq.last_instance.calls) == 1


def test_generate_angles_retries_on_malformed_json(monkeypatch, patched_env):
    from app.services.script_service import generate_angles

    _install_fake_groq(
        monkeypatch,
        ["not json at all {{{", _angles_payload(5)],
    )

    angles = generate_angles("tema x", n=5)

    assert len(angles) == 5
    assert len(FakeGroq.last_instance.calls) == 2
    retry_messages = FakeGroq.last_instance.calls[1]["messages"]
    # base system + user + corrective system = 3 turns
    assert retry_messages[-1]["role"] == "system"
    assert "invalid" in retry_messages[-1]["content"].lower()


def test_generate_angles_rejects_wrong_count(monkeypatch, patched_env):
    from app.services.script_service import AnglesGenerationError, generate_angles

    # Model returns 3 angles but caller asked for 5 — both attempts fail.
    _install_fake_groq(monkeypatch, [_angles_payload(3), _angles_payload(3)])

    with pytest.raises(AnglesGenerationError, match="failed twice"):
        generate_angles("tema", n=5)


def test_generate_angles_rejects_duplicate_titles(monkeypatch, patched_env):
    from app.services.script_service import AnglesGenerationError, generate_angles

    dup = json.dumps(
        {
            "angles": [
                {"title": "Same Title", "prompt": "a"},
                {"title": "Same Title", "prompt": "b"},
            ]
        }
    )
    _install_fake_groq(monkeypatch, [dup, dup])

    with pytest.raises(AnglesGenerationError, match="failed twice"):
        generate_angles("tema", n=2)


def test_generate_angles_rejects_empty_theme():
    from app.services.script_service import generate_angles

    with pytest.raises(ValueError, match="non-empty"):
        generate_angles("   ", n=5)


def test_angles_prompt_file_overrides_system_prompt(monkeypatch, patched_env, tmp_path):
    """ANGLES_PROMPT_FILE swaps the meta-prompt, mirroring SCRIPT_PROMPT_FILE.

    Without this, a non-English channel gets English titles even when the
    scriptwriter prompt is overridden.
    """
    from app.services.script_service import generate_angles

    override = tmp_path / "angles-pt.md"
    override.write_text("Você propõe ângulos virais em português.", encoding="utf-8")
    monkeypatch.setenv("ANGLES_PROMPT_FILE", str(override))
    _install_fake_groq(monkeypatch, [_angles_payload(3)])

    generate_angles("orçamento de serviço", n=3)

    messages = FakeGroq.last_instance.calls[0]["messages"]
    system_msg = next(m for m in messages if m["role"] == "system")
    assert system_msg["content"] == "Você propõe ângulos virais em português."


def test_angles_prompt_file_pointing_nowhere_falls_back_to_default(
    monkeypatch, patched_env, tmp_path
):
    """A stale/typo'd path must not crash the run — the built-in prompt wins.

    Guards the `.is_file()` check: reading the path unconditionally would
    raise FileNotFoundError here.
    """
    from app.services.script_service import _ANGLES_SYSTEM_PROMPT, generate_angles

    monkeypatch.setenv("ANGLES_PROMPT_FILE", str(tmp_path / "does-not-exist.md"))
    _install_fake_groq(monkeypatch, [_angles_payload(3)])

    generate_angles("tema qualquer", n=3)

    messages = FakeGroq.last_instance.calls[0]["messages"]
    system_msg = next(m for m in messages if m["role"] == "system")
    assert system_msg["content"] == _ANGLES_SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# Token budget / reasoning effort
#
# Reasoning models (Groq's openai/gpt-oss-*) spend the completion budget on
# hidden reasoning before emitting a token of JSON. With the old fixed 4096 and
# default effort, generation died with "max completion tokens reached before
# generating a valid document" — or, worse, silently returned a script whose
# scenes stopped halfway through the narration. Both knobs must be reachable
# from config, and `reasoning_effort` must NOT be sent unless configured: Groq
# rejects it with a 400 on models that don't support it.
# --------------------------------------------------------------------------- #

def test_uses_max_completion_tokens_from_config(monkeypatch, patched_env):
    """`max_tokens` is the legacy field and excludes reasoning on gpt-oss."""
    monkeypatch.setattr("app.services.script_service.GROQ_MAX_TOKENS", 6000)
    from app.services.script_service import generate_script

    _install_fake_groq(monkeypatch, [json.dumps(_VALID_PAYLOAD)])
    generate_script("tema", job_id="job-budget")

    kwargs = FakeGroq.last_instance.calls[0]
    assert kwargs["max_completion_tokens"] == 6000
    assert "max_tokens" not in kwargs


def test_reasoning_effort_omitted_when_unset(monkeypatch, patched_env):
    """Sending it to a non-reasoning model is a hard 400 — omit by default."""
    monkeypatch.setattr("app.services.script_service.GROQ_REASONING_EFFORT", "")
    from app.services.script_service import generate_script

    _install_fake_groq(monkeypatch, [json.dumps(_VALID_PAYLOAD)])
    generate_script("tema", job_id="job-noeffort")

    assert "reasoning_effort" not in FakeGroq.last_instance.calls[0]


def test_reasoning_effort_forwarded_when_configured(monkeypatch, patched_env):
    monkeypatch.setattr("app.services.script_service.GROQ_REASONING_EFFORT", "low")
    from app.services.script_service import generate_script

    _install_fake_groq(monkeypatch, [json.dumps(_VALID_PAYLOAD)])
    generate_script("tema", job_id="job-effort")

    assert FakeGroq.last_instance.calls[0]["reasoning_effort"] == "low"


def test_angles_call_also_honours_reasoning_effort(monkeypatch, patched_env):
    """The angles meta-prompt runs on the same model, so it starves the same way."""
    monkeypatch.setattr("app.services.script_service.GROQ_REASONING_EFFORT", "low")
    from app.services.script_service import generate_angles

    payload = {"angles": [{"title": f"t{i}", "prompt": f"p{i}"} for i in range(5)]}
    _install_fake_groq(monkeypatch, [json.dumps(payload)])
    generate_angles("tema qualquer", n=5)

    kwargs = FakeGroq.last_instance.calls[0]
    assert kwargs["reasoning_effort"] == "low"
    assert "max_completion_tokens" in kwargs
