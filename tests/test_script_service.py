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
