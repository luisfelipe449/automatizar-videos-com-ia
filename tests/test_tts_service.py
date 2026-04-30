"""Tests for `app.services.tts_service` — no real Gemini API is ever contacted."""

from __future__ import annotations

import wave
from pathlib import Path
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------- #
# Constants / fake payloads
# --------------------------------------------------------------------------- #

# 1 second of silence at 24 kHz, 16-bit mono = 24000 frames × 2 bytes.
_ONE_SECOND_SILENCE_PCM = b"\x00\x00" * 24_000

_REAL_FFMPEG = (
    "C:/Users/1luis/AppData/Local/Microsoft/WinGet/Packages/"
    "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/"
    "ffmpeg-8.1-full_build/bin/ffmpeg.exe"
)


# --------------------------------------------------------------------------- #
# Fake Gemini SDK
# --------------------------------------------------------------------------- #

def _make_response(pcm_bytes: bytes):
    """Build a SimpleNamespace that mimics the nested Gemini response shape."""
    inline_data = SimpleNamespace(data=pcm_bytes)
    part = SimpleNamespace(inline_data=inline_data)
    content = SimpleNamespace(parts=[part])
    candidate = SimpleNamespace(content=content)
    return SimpleNamespace(candidates=[candidate])


class _FakeModels:
    """Mimics `client.models` with a scripted queue of responses/exceptions."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):  # noqa: D401 — SDK-shaped signature
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeModels exhausted")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, bytes):
            return _make_response(item)
        # Pre-built response object.
        return item


class FakeClient:
    """Captures constructor args; exposes `.models` sharing a class-level queue.

    The queue is shared across every instance so tests that trigger multiple
    client constructions (e.g. cache-miss re-runs) still drain the same
    scripted sequence instead of each fresh client re-starting from element 0.
    """

    responses: list = []
    calls: list[dict] = []
    last_instance: "FakeClient | None" = None

    def __init__(self, *, api_key: str):
        self.api_key = api_key
        # Point the models object at the CLASS-level shared queue/calls list.
        self.models = _FakeModels.__new__(_FakeModels)
        self.models._responses = type(self).responses
        self.models.calls = type(self).calls
        FakeClient.last_instance = self


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def patched_env(monkeypatch, tmp_path: Path):
    """Fake API key, real ffmpeg, redirected generated/rendered dirs."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("FFMPEG_PATH", _REAL_FFMPEG)
    # ffmpeg_helpers snapshots FFMPEG_PATH at import time → patch it too.
    monkeypatch.setattr("app.utils.ffmpeg_helpers.FFMPEG_PATH", _REAL_FFMPEG)

    generated = tmp_path / "generated"
    rendered = tmp_path / "rendered"
    monkeypatch.setattr("app.utils.filepaths.GENERATED_DIR", generated)
    monkeypatch.setattr("app.utils.filepaths.RENDERED_DIR", rendered)
    return generated


def _install_fake_client(monkeypatch, responses: list) -> None:
    # Reset the class-level shared queue/calls list (see FakeClient docstring).
    FakeClient.responses = list(responses)
    FakeClient.calls = []
    FakeClient.last_instance = None
    monkeypatch.setattr("app.services.tts_service.genai.Client", FakeClient)


def _patch_no_backoff(monkeypatch):
    """Disable tenacity's sleep so retry-based tests finish instantly."""
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)


def _require_real_ffmpeg():
    if not Path(_REAL_FFMPEG).exists():
        pytest.skip(f"Real ffmpeg not available at {_REAL_FFMPEG}")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_happy_path_writes_wav_with_expected_duration(monkeypatch, patched_env):
    _require_real_ffmpeg()
    from app.services.tts_service import synthesize_narration
    from app.utils import ffmpeg_helpers

    _install_fake_client(monkeypatch, [_ONE_SECOND_SILENCE_PCM])

    wav_path = synthesize_narration("olá mundo", job_id="job-happy")

    assert wav_path.exists()
    assert wav_path.name == "narration.wav"
    # WAV structural sanity — stdlib can re-open it.
    with wave.open(str(wav_path), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 24_000
        assert w.getnframes() == 24_000
    # Real ffprobe reports duration ~1s.
    duration = ffmpeg_helpers.probe_duration(wav_path)
    assert 0.9 <= duration <= 1.1
    # API called exactly once.
    assert len(FakeClient.last_instance.calls) == 1
    # Hash file exists.
    hash_path = wav_path.with_suffix("").with_name("narration.hash")
    assert hash_path.exists()


def test_cache_hit_skips_second_api_call(monkeypatch, patched_env):
    _require_real_ffmpeg()
    from app.services.tts_service import synthesize_narration

    # Provide only ONE response — a second API call would raise "exhausted".
    _install_fake_client(monkeypatch, [_ONE_SECOND_SILENCE_PCM])

    narration = "mesmo texto"
    first = synthesize_narration(narration, job_id="job-cache")
    assert first.exists()
    assert len(FakeClient.last_instance.calls) == 1

    second = synthesize_narration(narration, job_id="job-cache")
    assert second == first
    # Still exactly one call — cache short-circuited.
    assert len(FakeClient.last_instance.calls) == 1


def test_cache_miss_on_text_change_reinvokes_client(monkeypatch, patched_env):
    _require_real_ffmpeg()
    from app.services.tts_service import synthesize_narration
    from app.utils import cache, filepaths

    # Two different PCM payloads → different WAV sizes so we can tell them apart.
    payload_a = b"\x00\x00" * 24_000           # 1s silence
    payload_b = b"\x01\x00" * (24_000 * 2)     # 2s of low-amplitude tone
    _install_fake_client(monkeypatch, [payload_a, payload_b])

    job_id = "job-miss"
    path_a = synthesize_narration("texto A", job_id=job_id)
    size_a = path_a.stat().st_size
    hash_a = cache.read_hash_file(filepaths.narration_hash_path(job_id))

    path_b = synthesize_narration("texto B (diferente)", job_id=job_id)
    size_b = path_b.stat().st_size
    hash_b = cache.read_hash_file(filepaths.narration_hash_path(job_id))

    assert path_a == path_b  # same job → same wav path
    assert size_b != size_a  # re-rendered from new payload
    assert hash_a != hash_b  # hash file was refreshed
    # Two calls — the change invalidated the cache.
    assert len(FakeClient.last_instance.calls) == 2


def test_api_error_propagates_after_retries(monkeypatch, patched_env):
    _patch_no_backoff(monkeypatch)
    from app.services.tts_service import synthesize_narration

    # default_retry attempts 3 times, reraise=True → ConnectionError propagates.
    _install_fake_client(
        monkeypatch,
        [
            ConnectionError("network down 1"),
            ConnectionError("network down 2"),
            ConnectionError("network down 3"),
        ],
    )

    with pytest.raises(ConnectionError, match="network down"):
        synthesize_narration("qualquer", job_id="job-err")

    # All three attempts were consumed.
    assert len(FakeClient.last_instance.calls) == 3


def test_empty_payload_raises_tts_error(monkeypatch, patched_env):
    _patch_no_backoff(monkeypatch)
    from app.services.tts_service import TTSError, synthesize_narration

    # Empty bytes on every attempt → default_retry can't help (TTSError isn't
    # transient). With reraise=True tenacity surfaces the last exception.
    _install_fake_client(monkeypatch, [b"", b"", b""])

    with pytest.raises(TTSError, match="empty audio payload"):
        synthesize_narration("qualquer", job_id="job-empty")
