"""Tests for `app.utils.*` primitives."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from app.utils import cache, quota
from app.utils.filepaths import make_job_id, scene_image_path
from app.utils.retries import is_retryable_http


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


# --------------------------------------------------------------------------- #
# ffmpeg_helpers (need FFMPEG_PATH; tests use monkeypatch to stay hermetic)
# --------------------------------------------------------------------------- #

_FFMPEG_WINGET = Path(
    "C:/Users/1luis/AppData/Local/Microsoft/WinGet/Packages/"
    "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/"
    "ffmpeg-8.1-full_build/bin/ffmpeg.exe"
)


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
    if not _FFMPEG_WINGET.exists():
        pytest.skip(f"ffmpeg not found at {_FFMPEG_WINGET}")
    ff = _import_ffmpeg_fresh(monkeypatch, str(_FFMPEG_WINGET))
    binary = ff.ffmpeg_bin()
    result = subprocess.run(
        [binary, "-version"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
    )
    assert result.returncode == 0
    assert "ffmpeg version" in result.stdout.lower()


def test_probe_duration_on_generated_silent_wav(monkeypatch, tmp_path: Path):
    if not _FFMPEG_WINGET.exists():
        pytest.skip(f"ffmpeg not found at {_FFMPEG_WINGET}")
    ff = _import_ffmpeg_fresh(monkeypatch, str(_FFMPEG_WINGET))

    wav = tmp_path / "silent.wav"
    ff.run_ffmpeg([
        "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
        "-t", "1", str(wav),
    ])
    assert wav.exists() and wav.stat().st_size > 0

    duration = ff.probe_duration(wav)
    assert 0.9 <= duration <= 1.1, f"expected ~1.0s, got {duration}"
