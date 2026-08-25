"""Tests for `app.utils.*` primitives."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from app.utils import cache, quota
from app.utils.filepaths import (
    make_job_id,
    scene_image_path,
    scene_video_hash_path,
    scene_video_path,
)
from app.utils.retries import default_retry, is_retryable_http
from tests.ffmpeg_locator import find_ffmpeg


# --------------------------------------------------------------------------- #
# filepaths
# --------------------------------------------------------------------------- #

def test_make_job_id_format_and_idempotency():
    prompt = "3 curiosidades sobre buracos negros"
    now = datetime(2026, 4, 30, 12, 0, 0)
    job_id = make_job_id(prompt, now=now)
    assert re.match(r"^\d{4}-\d{2}-\d{2}_[a-z0-9-]+_[0-9a-f]{6}$", job_id), job_id
    # Same inputs → same id.
    assert make_job_id(prompt, now=now) == job_id


def test_scene_image_path_zero_padded():
    path = scene_image_path("job1", 1)
    assert path.name == "scene_01.png"


def test_scene_video_path_zero_padded():
    path = scene_video_path("job1", 3)
    assert path.name == "scene_03.mp4"


def test_scene_video_hash_path_uses_video_suffix():
    """`.video.hash` lets image- and video-hashes coexist in the same job dir."""
    path = scene_video_hash_path("job1", 5)
    assert path.name == "scene_05.video.hash"


def test_scene_video_path_is_sibling_of_image_path():
    """Image and video artifacts for the same scene share a parent directory."""
    img = scene_image_path("job1", 2)
    vid = scene_video_path("job1", 2)
    assert img.parent == vid.parent


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #

def test_hash_text_is_64_char_lowercase_hex():
    digest = cache.hash_text("hello")
    assert len(digest) == 64
    assert digest == digest.lower()
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_artifact_fresh_lifecycle(tmp_path: Path):
    artifact = tmp_path / "a.bin"
    hash_file = tmp_path / "a.hash"
    expected = cache.hash_text("payload")

    # (a) artifact missing → False
    assert cache.artifact_fresh(artifact, hash_file, expected) is False

    # (b) present + matching hash → True
    artifact.write_bytes(b"payload-bytes")
    cache.write_hash_file(hash_file, expected)
    assert cache.artifact_fresh(artifact, hash_file, expected) is True

    # (c) hash mismatch → False
    assert cache.artifact_fresh(artifact, hash_file, "deadbeef") is False


# --------------------------------------------------------------------------- #
# retries
# --------------------------------------------------------------------------- #

def test_is_retryable_http_matches_transient_codes():
    assert is_retryable_http(RuntimeError("Upstream returned 504 Gateway Timeout"))
    assert is_retryable_http(RuntimeError("connection timeout after 30s"))


def test_is_retryable_http_rejects_deterministic_errors():
    assert is_retryable_http(ValueError("nope")) is False


def test_is_retryable_http_treats_rate_limit_as_transient():
    # Gemini/Groq surface quota pressure as 429 — retry them.
    assert is_retryable_http(ValueError("429 rate limit")) is True
    assert is_retryable_http(RuntimeError("server said rate_limit_exceeded")) is True


def test_is_retryable_http_rejects_4xx_other_than_429():
    assert is_retryable_http(ValueError("400 bad request")) is False
    assert is_retryable_http(ValueError("401 unauthorized")) is False
    assert is_retryable_http(ValueError("404 not found")) is False


def test_is_retryable_http_accepts_builtin_connection_and_timeout_errors():
    assert is_retryable_http(ConnectionError()) is True
    assert is_retryable_http(TimeoutError()) is True


def test_default_retry_does_not_retry_deterministic_errors(monkeypatch):
    """A 400-shaped error must fail fast (single attempt), not burn 3."""
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)

    calls = {"n": 0}

    @default_retry
    def explode_deterministically():
        calls["n"] += 1
        raise ValueError("400 invalid payload")

    with pytest.raises(ValueError, match="400 invalid"):
        explode_deterministically()
    assert calls["n"] == 1


def test_default_retry_retries_transient_connection_error(monkeypatch):
    """ConnectionError is transient → default_retry uses all 3 attempts."""
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)

    calls = {"n": 0}

    @default_retry
    def explode_transiently():
        calls["n"] += 1
        raise ConnectionError("network blip")

    with pytest.raises(ConnectionError):
        explode_transiently()
    assert calls["n"] == 3


# --------------------------------------------------------------------------- #
# quota
# --------------------------------------------------------------------------- #

def test_quota_imagen_is_per_job():
    quota.reset("job1")
    quota.reset("job2")
    quota.mark_imagen_unavailable("job1")
    assert quota.is_imagen_unavailable("job1") is True
    assert quota.is_imagen_unavailable("job2") is False
    quota.reset("job1")
    assert quota.is_imagen_unavailable("job1") is False


def test_quota_video_is_per_job_and_independent_of_imagen():
    """Video/Imagen/Pollinations flags must not leak between providers."""
    quota.reset("vid-job1")
    quota.reset("vid-job2")

    quota.mark_video_unavailable("vid-job1")
    assert quota.is_video_unavailable("vid-job1") is True
    assert quota.is_video_unavailable("vid-job2") is False
    # Marking video must NOT trip the image providers.
    assert quota.is_imagen_unavailable("vid-job1") is False
    assert quota.is_pollinations_unavailable("vid-job1") is False

    quota.reset("vid-job1")
    assert quota.is_video_unavailable("vid-job1") is False


# --------------------------------------------------------------------------- #
# ffmpeg_helpers (need FFMPEG_PATH; tests use monkeypatch to stay hermetic)
# --------------------------------------------------------------------------- #

_FFMPEG_WINGET = find_ffmpeg()


def _import_ffmpeg_fresh(monkeypatch, path: str):
    """Reimport app.utils.ffmpeg_helpers with FFMPEG_PATH patched at module level."""
    import importlib
    import app.config as cfg
    import app.utils.ffmpeg_helpers as ff
    monkeypatch.setenv("FFMPEG_PATH", path)
    monkeypatch.setattr(cfg, "FFMPEG_PATH", path, raising=False)
    monkeypatch.setattr(ff, "FFMPEG_PATH", path, raising=False)
    importlib.reload(ff)
    monkeypatch.setattr(ff, "FFMPEG_PATH", path, raising=False)
    return ff


def test_ffmpeg_bin_resolves_and_runs(monkeypatch):
    if _FFMPEG_WINGET is None:
        pytest.skip("No ffmpeg found (set FFMPEG_PATH or put ffmpeg on PATH)")
    ff = _import_ffmpeg_fresh(monkeypatch, str(_FFMPEG_WINGET))
    binary = ff.ffmpeg_bin()
    result = subprocess.run(
        [binary, "-version"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
    )
    assert result.returncode == 0
    assert "ffmpeg version" in result.stdout.lower()


def test_probe_duration_on_generated_silent_wav(monkeypatch, tmp_path: Path):
    if _FFMPEG_WINGET is None:
        pytest.skip("No ffmpeg found (set FFMPEG_PATH or put ffmpeg on PATH)")
    ff = _import_ffmpeg_fresh(monkeypatch, str(_FFMPEG_WINGET))

    wav = tmp_path / "silent.wav"
    ff.run_ffmpeg([
        "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
        "-t", "1", str(wav),
    ])
    assert wav.exists() and wav.stat().st_size > 0

    duration = ff.probe_duration(wav)
    assert 0.9 <= duration <= 1.1, f"expected ~1.0s, got {duration}"


# --------------------------------------------------------------------------- #
# retries — explicit non-retryable opt-out
# --------------------------------------------------------------------------- #

def test_exception_can_opt_out_of_retrying():
    """`retryable = False` beats the substring heuristic.

    Quota exhaustion carries a 429 in its message, which the substring match
    would otherwise classify as transient. The opt-out exists so a service can
    say "this will not fix itself" without having to launder the message.
    """
    from app.utils.retries import is_retryable_http

    class _Terminal(RuntimeError):
        retryable = False

    assert is_retryable_http(_Terminal("429 RESOURCE_EXHAUSTED quota")) is False
    # Same message, no opt-out → still treated as transient.
    assert is_retryable_http(RuntimeError("429 RESOURCE_EXHAUSTED quota")) is True


def test_opt_out_does_not_affect_ordinary_exceptions():
    """Absence of the attribute must not change existing classification."""
    from app.utils.retries import is_retryable_http

    assert is_retryable_http(RuntimeError("503 unavailable")) is True
    assert is_retryable_http(RuntimeError("400 bad request")) is False
