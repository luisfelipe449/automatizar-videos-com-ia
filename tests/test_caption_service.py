"""Tests for `app.services.caption_service` -- no real Groq API is ever contacted."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------- #
# Fake Groq SDK
# --------------------------------------------------------------------------- #

def _make_transcription(words: list[dict] | None):
    """Build a SimpleNamespace that mimics the Groq Transcription object.

    `words=None` -> Transcription object WITHOUT a `.words` attribute at all.
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

    def create(self, **kwargs):  # noqa: D401 -- SDK-shaped signature
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
    """Create a tiny non-empty placeholder file; contents don't matter -- the SDK is mocked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF0000WAVEfake")
    return path


# --------------------------------------------------------------------------- #
# Pure-helper unit tests (no mocks needed)
# --------------------------------------------------------------------------- #

def test_format_ass_time_zero():
    from app.services.caption_service import _format_ass_time

    assert _format_ass_time(0.0) == "0:00:00.00"


def test_format_ass_time_mixed_units():
    from app.services.caption_service import _format_ass_time

    # 1h 1m 1.456s -> 1:01:01.46 (centisecond rounding)
    assert _format_ass_time(3661.456) == "1:01:01.46"


def test_format_ass_time_sub_second():
    from app.services.caption_service import _format_ass_time

    assert _format_ass_time(0.5) == "0:00:00.50"


def test_format_ass_time_clamps_negative():
    from app.services.caption_service import _format_ass_time

    assert _format_ass_time(-1.0) == "0:00:00.00"


def test_group_words_basic_chunking_carries_word_list():
    from app.services.caption_service import _group_words

    words = [
        {"word": "a", "start": 0.0, "end": 0.3},
        {"word": "b", "start": 0.3, "end": 0.6},
        {"word": "c", "start": 0.6, "end": 0.9},
    ]
    cues = _group_words(words, chunk_size=2)

    assert len(cues) == 2
    # Cue 1: words a, b (both carried through with timings).
    assert cues[0]["index"] == 1
    assert cues[0]["start"] == 0.0
    assert cues[0]["end"] == 0.6
    assert [w["word"] for w in cues[0]["words"]] == ["a", "b"]
    # Cue 2: word c.
    assert cues[1]["index"] == 2
    assert cues[1]["start"] == 0.6
    assert cues[1]["end"] == 0.9
    assert [w["word"] for w in cues[1]["words"]] == ["c"]


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
    assert [w["word"] for w in cues[0]["words"]] == ["hello", "world"]
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


def test_build_dialogue_line_three_words_equal_timing():
    from app.services.caption_service import _build_dialogue_line

    cue = {
        "index": 1,
        "start": 0.0,
        "end": 1.2,
        "words": [
            {"word": "word1", "start": 0.0, "end": 0.4},
            {"word": "word2", "start": 0.4, "end": 0.8},
            {"word": "word3", "start": 0.8, "end": 1.2},
        ],
    }
    line = _build_dialogue_line(cue)

    # Dialogue prefix + timestamps + default style + karaoke text.
    assert line.startswith("Dialogue: 0,0:00:00.00,0:00:01.20,Default,,0,0,0,,")
    assert r"{\k40}word1 {\k40}word2 {\k40}word3" in line


def test_build_dialogue_line_zero_duration_clamps_to_one_cs():
    from app.services.caption_service import _build_dialogue_line

    cue = {
        "index": 1,
        "start": 0.0,
        "end": 0.1,
        "words": [{"word": "oi", "start": 0.0, "end": 0.0}],
    }
    line = _build_dialogue_line(cue)
    # Minimum 1cs so a zero-length word still renders.
    assert r"{\k1}oi" in line


def test_build_dialogue_line_escapes_braces_and_backslash():
    from app.services.caption_service import _build_dialogue_line

    cue = {
        "index": 1,
        "start": 0.0,
        "end": 0.5,
        "words": [{"word": "a{b}\\c", "start": 0.0, "end": 0.5}],
    }
    line = _build_dialogue_line(cue)
    # Curly braces get swapped for parens; backslashes get doubled.
    assert "(b)" in line
    assert "\\\\c" in line


def test_render_ass_contains_required_sections_and_style():
    from app.services.caption_service import _render_ass

    cue = {
        "index": 1,
        "start": 0.0,
        "end": 0.5,
        "words": [{"word": "hi", "start": 0.0, "end": 0.5}],
    }
    out = _render_ass([cue])

    # All three mandatory ASS sections present.
    assert "[Script Info]" in out
    assert "[V4+ Styles]" in out
    assert "[Events]" in out
    # Play resolution authored for 1080x1920 Shorts.
    assert "PlayResX: 1080" in out
    assert "PlayResY: 1920" in out
    # Style line with Bebas Neue + the exact color constants.
    assert "Style: Default,Bebas Neue," in out
    assert "&H0000D7FF" in out  # yellow (PrimaryColour)
    assert "&H00FFFFFF" in out  # white (SecondaryColour)
    assert "&H00000000" in out  # black (outline + back)
    # At least one Dialogue line with a karaoke tag.
    assert "Dialogue:" in out
    assert r"{\k" in out
    # No BOM, no CRLF.
    assert "\r" not in out
    assert not out.startswith("﻿")


def test_render_ass_emits_one_dialogue_per_cue():
    from app.services.caption_service import _render_ass

    cues = [
        {
            "index": 1, "start": 0.0, "end": 0.4,
            "words": [{"word": "one", "start": 0.0, "end": 0.4}],
        },
        {
            "index": 2, "start": 0.4, "end": 0.8,
            "words": [{"word": "two", "start": 0.4, "end": 0.8}],
        },
    ]
    out = _render_ass(cues)
    assert out.count("Dialogue:") == 2


# --------------------------------------------------------------------------- #
# End-to-end tests (Groq client mocked)
# --------------------------------------------------------------------------- #

def test_happy_path_writes_valid_ass(monkeypatch, patched_env, tmp_path):
    from app.services.caption_service import transcribe
    from app.utils import filepaths

    wav = _touch_wav(tmp_path / "narration.wav")
    words = [
        {"word": "olá", "start": 0.0, "end": 0.4},
        {"word": "mundo", "start": 0.4, "end": 0.9},
        {"word": "feliz", "start": 0.9, "end": 1.4},
    ]
    _install_fake_groq(monkeypatch, [_make_transcription(words)])

    ass_path = transcribe(wav, job_id="job-happy")

    assert ass_path == filepaths.captions_path("job-happy")
    assert ass_path.suffix == ".ass"
    assert ass_path.exists()

    contents = ass_path.read_text(encoding="utf-8")
    # Sections present.
    assert "[Events]" in contents
    # Exactly 1 Dialogue line because 3 words <= default chunk_size=6.
    assert contents.count("Dialogue:") == 1
    # The words are still visible as text (no escape mangling).
    for w in ("olá", "mundo", "feliz"):
        assert w in contents
    # Karaoke tags appear.
    assert r"{\k" in contents
    # Cue timing covers 0 -> 1.4s (single-digit hour, cs format).
    assert "0:00:00.00," in contents
    assert ",0:00:01.40," in contents

    # Exactly one Groq call with the right kwargs.
    assert len(FakeGroq.last_instance.calls) == 1
    call_kwargs = FakeGroq.last_instance.calls[0]
    assert call_kwargs["model"] == "whisper-large-v3"
    assert call_kwargs["response_format"] == "verbose_json"
    assert call_kwargs["timestamp_granularities"] == ["word"]
    assert call_kwargs["language"] == "pt"


def test_empty_transcription_raises_caption_error(monkeypatch, patched_env, tmp_path):
    _patch_no_backoff(monkeypatch)
    from app.services.caption_service import CaptionError, transcribe

    wav = _touch_wav(tmp_path / "narration.wav")
    # Transcription has a `.words` attribute, but it's empty.
    _install_fake_groq(monkeypatch, [_make_transcription([])])

    with pytest.raises(CaptionError, match="empty word list"):
        transcribe(wav, job_id="job-empty")


def test_missing_words_attribute_raises_caption_error(monkeypatch, patched_env, tmp_path):
    _patch_no_backoff(monkeypatch)
    from app.services.caption_service import CaptionError, transcribe

    wav = _touch_wav(tmp_path / "narration.wav")
    # `words=None` -> the fake Transcription has NO `.words` attribute at all.
    _install_fake_groq(monkeypatch, [_make_transcription(None)])

    with pytest.raises(CaptionError, match="empty word list"):
        transcribe(wav, job_id="job-no-words")


def test_missing_wav_raises_caption_error(monkeypatch, patched_env, tmp_path):
    from app.services.caption_service import CaptionError, transcribe

    missing = tmp_path / "does_not_exist.wav"
    # No SDK call should happen, but install a Fake anyway to be sure nothing hits real Groq.
    _install_fake_groq(monkeypatch, [])

    with pytest.raises(CaptionError, match="narration WAV not found"):
        transcribe(missing, job_id="job-missing")


def test_transient_connection_error_is_retried(monkeypatch, patched_env, tmp_path):
    _patch_no_backoff(monkeypatch)
    from app.services.caption_service import transcribe

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

    ass_path = transcribe(wav, job_id="job-retry")

    assert ass_path.exists()
    # First attempt failed, second succeeded -> exactly 2 create() invocations.
    assert len(FakeGroq.last_instance.calls) == 2
    contents = ass_path.read_text(encoding="utf-8")
    for w in ("oi", "tudo", "bem"):
        assert w in contents


def test_caption_cache_hit_with_matching_hash(monkeypatch, patched_env, tmp_path):
    """A pre-existing ASS + matching `captions.hash` must skip the Groq call."""
    from app.services.caption_service import transcribe
    from app.utils import cache, filepaths

    wav = _touch_wav(tmp_path / "narration.wav")
    narration_hash = cache.hash_text("some narration text")

    # Pre-create the artifact + hash so the service sees a fresh cache.
    filepaths.ensure_job_dirs("job-cache-hit")
    ass = filepaths.captions_path("job-cache-hit")
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\\k100}olá\n",
        encoding="utf-8",
    )
    cache.write_hash_file(filepaths.captions_hash_path("job-cache-hit"), narration_hash)

    # Install an empty queue -- any API call would blow up.
    _install_fake_groq(monkeypatch, [])

    returned = transcribe(wav, job_id="job-cache-hit", narration_hash=narration_hash)

    assert returned == ass
    # Zero Groq invocations thanks to the cache.
    assert FakeGroq.last_instance is None


def test_caption_cache_miss_on_hash_change(monkeypatch, patched_env, tmp_path):
    """A stale ASS whose hash no longer matches must trigger a fresh transcription."""
    from app.services.caption_service import transcribe
    from app.utils import cache, filepaths

    wav = _touch_wav(tmp_path / "narration.wav")
    old_hash = cache.hash_text("old narration")
    new_hash = cache.hash_text("brand new narration")

    filepaths.ensure_job_dirs("job-cache-miss")
    ass = filepaths.captions_path("job-cache-miss")
    ass.write_text("STALE-CONTENT", encoding="utf-8")
    cache.write_hash_file(filepaths.captions_hash_path("job-cache-miss"), old_hash)

    words = [
        {"word": "novo", "start": 0.0, "end": 0.4},
        {"word": "conteúdo", "start": 0.4, "end": 0.9},
    ]
    _install_fake_groq(monkeypatch, [_make_transcription(words)])

    returned = transcribe(wav, job_id="job-cache-miss", narration_hash=new_hash)

    assert returned == ass
    # Groq WAS called because the narration hash changed.
    assert len(FakeGroq.last_instance.calls) == 1
    # The captions file was rewritten (no longer contains the stale marker).
    contents = ass.read_text(encoding="utf-8")
    assert "STALE-CONTENT" not in contents
    for w in ("novo", "conteúdo"):
        assert w in contents
    # The hash file now stores the NEW hash.
    assert cache.read_hash_file(filepaths.captions_hash_path("job-cache-miss")) == new_hash


def test_chunk_boundary_13_words_chunk_size_6(monkeypatch, patched_env, tmp_path):
    from app.services.caption_service import transcribe

    wav = _touch_wav(tmp_path / "narration.wav")
    # 13 words with 0.1s duration each, 0.1s gap = start at 0.0, next at 0.2, ...
    words = []
    for i in range(13):
        start = i * 0.2
        words.append({"word": f"w{i}", "start": start, "end": start + 0.1})
    _install_fake_groq(monkeypatch, [_make_transcription(words)])

    ass_path = transcribe(wav, job_id="job-13", words_per_chunk=6)
    contents = ass_path.read_text(encoding="utf-8")

    # 13 words at chunk_size=6 -> 6 + 6 + 1 = 3 Dialogue lines.
    assert contents.count("Dialogue:") == 3
    # Every word is present.
    for i in range(13):
        assert f"w{i}" in contents
