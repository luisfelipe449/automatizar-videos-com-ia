"""Tests for `app.services.caption_service` — no real Groq API is ever contacted."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------- #
# Fake Groq SDK
# --------------------------------------------------------------------------- #

def _make_transcription(words: list[dict] | None):
    """Build a SimpleNamespace that mimics the Groq Transcription object.

    `words=None` → Transcription object WITHOUT a `.words` attribute at all.
    """
    if words is None:
        return SimpleNamespace(text="whatever")
    word_objs = [
        SimpleNamespace(word=w["word"], start=w["start"], end=w["end"])
        for w in words
    ]
    return SimpleNamespace(text=" ".join(w["word"] for w in words), words=word_objs)


class _FakeTranscriptions:
    """Mimics `client.audio.transcriptions` with a scripted output queue."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):  # noqa: D401 — SDK-shaped signature
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeTranscriptions exhausted")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeAudio:
    def __init__(self, transcriptions: _FakeTranscriptions):
        self.transcriptions = transcriptions


class FakeGroq:
    """Class-level shared queue so every client instance drains the same list."""

    responses: list = []
    calls: list[dict] = []
    last_instance: "FakeGroq | None" = None

    def __init__(self, api_key: str):
        self.api_key = api_key
        transcriptions = _FakeTranscriptions.__new__(_FakeTranscriptions)
        transcriptions._responses = type(self).responses
        transcriptions.calls = type(self).calls
        self.audio = _FakeAudio(transcriptions)
        FakeGroq.last_instance = self


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def patched_env(monkeypatch, tmp_path: Path):
    """Fake API key + redirected generated/rendered dirs."""
    monkeypatch.setenv("GROQ_API_KEY", "fake-key")
    generated = tmp_path / "generated"
    rendered = tmp_path / "rendered"
    monkeypatch.setattr("app.utils.filepaths.GENERATED_DIR", generated)
    monkeypatch.setattr("app.utils.filepaths.RENDERED_DIR", rendered)
    return generated


def _install_fake_groq(monkeypatch, responses: list) -> None:
    FakeGroq.responses = list(responses)
    FakeGroq.calls = []
    FakeGroq.last_instance = None
    monkeypatch.setattr("app.services.caption_service.Groq", FakeGroq)


def _patch_no_backoff(monkeypatch):
    """Disable tenacity's sleep so retry-based tests finish instantly."""
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)


def _touch_wav(path: Path) -> Path:
    """Create a tiny non-empty placeholder file; contents don't matter — the SDK is mocked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF0000WAVEfake")
    return path


# --------------------------------------------------------------------------- #
# Pure-helper unit tests (no mocks needed)
# --------------------------------------------------------------------------- #

def test_format_srt_time_zero():
    from app.services.caption_service import _format_srt_time

    assert _format_srt_time(0.0) == "00:00:00,000"


def test_format_srt_time_mixed_units():
    from app.services.caption_service import _format_srt_time

    # 1h 1m 1.456s
    assert _format_srt_time(3661.456) == "01:01:01,456"


def test_format_srt_time_sub_second():
    from app.services.caption_service import _format_srt_time

    assert _format_srt_time(0.5) == "00:00:00,500"


def test_group_words_basic_chunking():
    from app.services.caption_service import _group_words

    words = [
        {"word": "a", "start": 0.0, "end": 0.3},
        {"word": "b", "start": 0.3, "end": 0.6},
        {"word": "c", "start": 0.6, "end": 0.9},
    ]
    cues = _group_words(words, chunk_size=2)

    assert len(cues) == 2
    assert cues[0] == {"index": 1, "start": 0.0, "end": 0.6, "text": "a b"}
    assert cues[1] == {"index": 2, "start": 0.6, "end": 0.9, "text": "c"}


def test_group_words_empty_input_yields_no_cues():
    from app.services.caption_service import _group_words

    assert _group_words([], chunk_size=6) == []


def test_group_words_skips_blank_word_tokens():
    from app.services.caption_service import _group_words

    words = [
        {"word": "  ", "start": 0.0, "end": 0.1},
        {"word": "hello", "start": 0.1, "end": 0.4},
        {"word": "", "start": 0.4, "end": 0.5},
        {"word": "world", "start": 0.5, "end": 0.9},
    ]
    cues = _group_words(words, chunk_size=6)

    assert len(cues) == 1
    assert cues[0]["text"] == "hello world"
    assert cues[0]["start"] == 0.1
    assert cues[0]["end"] == 0.9


def test_group_words_bumps_non_positive_duration():
    from app.services.caption_service import _group_words

    # Degenerate case: a single word whose end equals its start.
    words = [{"word": "x", "start": 1.0, "end": 1.0}]
    cues = _group_words(words, chunk_size=6)

    assert len(cues) == 1
    assert cues[0]["start"] == 1.0
    assert cues[0]["end"] == pytest.approx(1.1)


def test_render_srt_produces_spec_compliant_block():
    from app.services.caption_service import _render_srt

    out = _render_srt([{"index": 1, "start": 0.0, "end": 0.5, "text": "hi"}])

    # The exact block the spec asks us to verify.
    assert "1\n00:00:00,000 --> 00:00:00,500\nhi\n" in out
    # Should end with a trailing newline (SRT blocks are newline-terminated).
    assert out.endswith("\n")
    # No BOM, no CRLF.
    assert "\r" not in out
    assert not out.startswith("﻿")


def test_render_srt_separates_multiple_cues_with_blank_line():
    from app.services.caption_service import _render_srt

    out = _render_srt(
        [
            {"index": 1, "start": 0.0, "end": 0.5, "text": "one"},
            {"index": 2, "start": 0.5, "end": 1.0, "text": "two"},
        ]
    )

    # Mandatory blank line between cues in SRT format.
    assert "one\n\n2\n" in out


# --------------------------------------------------------------------------- #
# End-to-end tests (Groq client mocked)
# --------------------------------------------------------------------------- #

def test_happy_path_writes_valid_srt(monkeypatch, patched_env, tmp_path):
    from app.services.caption_service import transcribe_to_srt
    from app.utils import filepaths

    wav = _touch_wav(tmp_path / "narration.wav")
    words = [
        {"word": "olá", "start": 0.0, "end": 0.4},
        {"word": "mundo", "start": 0.4, "end": 0.9},
        {"word": "feliz", "start": 0.9, "end": 1.4},
    ]
    _install_fake_groq(monkeypatch, [_make_transcription(words)])

    srt_path = transcribe_to_srt(wav, job_id="job-happy")

    assert srt_path == filepaths.captions_path("job-happy")
    assert srt_path.exists()

    contents = srt_path.read_text(encoding="utf-8")
    # Exactly 1 cue because 3 words ≤ default chunk_size=6.
    assert "1\n" in contents
    assert "2\n" not in contents.split("\n\n")[0]
    # Text is the joined words.
    assert "olá mundo feliz" in contents
    # Timestamps came from the first/last word.
    assert "00:00:00,000 --> 00:00:01,400" in contents
    # One API call.
    assert len(FakeGroq.last_instance.calls) == 1
    call_kwargs = FakeGroq.last_instance.calls[0]
    assert call_kwargs["model"] == "whisper-large-v3"
    assert call_kwargs["response_format"] == "verbose_json"
    assert call_kwargs["timestamp_granularities"] == ["word"]
    assert call_kwargs["language"] == "pt"


def test_empty_transcription_raises_caption_error(monkeypatch, patched_env, tmp_path):
    _patch_no_backoff(monkeypatch)
    from app.services.caption_service import CaptionError, transcribe_to_srt

    wav = _touch_wav(tmp_path / "narration.wav")
    # Transcription has a `.words` attribute, but it's empty.
    _install_fake_groq(monkeypatch, [_make_transcription([])])

    with pytest.raises(CaptionError, match="empty word list"):
        transcribe_to_srt(wav, job_id="job-empty")


def test_missing_words_attribute_raises_caption_error(monkeypatch, patched_env, tmp_path):
    _patch_no_backoff(monkeypatch)
    from app.services.caption_service import CaptionError, transcribe_to_srt

    wav = _touch_wav(tmp_path / "narration.wav")
    # `words=None` → the fake Transcription has NO `.words` attribute at all.
    _install_fake_groq(monkeypatch, [_make_transcription(None)])

    with pytest.raises(CaptionError, match="empty word list"):
        transcribe_to_srt(wav, job_id="job-no-words")


def test_missing_wav_raises_caption_error(monkeypatch, patched_env, tmp_path):
    from app.services.caption_service import CaptionError, transcribe_to_srt

    missing = tmp_path / "does_not_exist.wav"
    # No SDK call should happen, but install a Fake anyway to be sure nothing hits real Groq.
    _install_fake_groq(monkeypatch, [])

    with pytest.raises(CaptionError, match="narration WAV not found"):
        transcribe_to_srt(missing, job_id="job-missing")


def test_transient_connection_error_is_retried(monkeypatch, patched_env, tmp_path):
    _patch_no_backoff(monkeypatch)
    from app.services.caption_service import transcribe_to_srt

    wav = _touch_wav(tmp_path / "narration.wav")
    words = [
        {"word": "oi", "start": 0.0, "end": 0.5},
        {"word": "tudo", "start": 0.5, "end": 0.9},
        {"word": "bem", "start": 0.9, "end": 1.3},
    ]
    _install_fake_groq(
        monkeypatch,
        [ConnectionError("temporary blip"), _make_transcription(words)],
    )

    srt_path = transcribe_to_srt(wav, job_id="job-retry")

    assert srt_path.exists()
    # First attempt failed, second succeeded → exactly 2 create() invocations.
    assert len(FakeGroq.last_instance.calls) == 2
    assert "oi tudo bem" in srt_path.read_text(encoding="utf-8")


def test_chunk_boundary_13_words_chunk_size_6(monkeypatch, patched_env, tmp_path):
    from app.services.caption_service import transcribe_to_srt

    wav = _touch_wav(tmp_path / "narration.wav")
    # 13 words with 0.1s duration each, 0.1s gap = start at 0.0, next at 0.2, ...
    words = []
    for i in range(13):
        start = i * 0.2
        words.append({"word": f"w{i}", "start": start, "end": start + 0.1})
    _install_fake_groq(monkeypatch, [_make_transcription(words)])

    srt_path = transcribe_to_srt(wav, job_id="job-13", words_per_chunk=6)
    contents = srt_path.read_text(encoding="utf-8")

    # 13 words at chunk_size=6 → 6 + 6 + 1 = 3 cues.
    # Split on blank lines (SRT cue separator).
    blocks = [b for b in contents.strip().split("\n\n") if b]
    assert len(blocks) == 3

    # Cue 1: w0..w5 (6 words).
    cue1_text_line = blocks[0].split("\n")[2]
    assert cue1_text_line.split(" ") == [f"w{i}" for i in range(6)]
    # Cue 2: w6..w11 (6 words).
    cue2_text_line = blocks[1].split("\n")[2]
    assert cue2_text_line.split(" ") == [f"w{i}" for i in range(6, 12)]
    # Cue 3: w12 (1 word).
    cue3_text_line = blocks[2].split("\n")[2]
    assert cue3_text_line.split(" ") == ["w12"]

    # Indices are 1, 2, 3.
    assert blocks[0].startswith("1\n")
    assert blocks[1].startswith("2\n")
    assert blocks[2].startswith("3\n")
