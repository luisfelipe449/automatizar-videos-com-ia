"""Gemini TTS → WAV (Phase 4 of the pipeline).

Single responsibility: take narration text + job_id, call Gemini TTS, write a
16-bit 24kHz mono WAV to `filepaths.narration_path(job_id)`. Skips the API
call entirely when the existing artifact + sibling hash file say the input
hasn't changed.
"""

from __future__ import annotations

import wave
from pathlib import Path

from google import genai
from google.genai import types

from app.config import GEMINI_TTS_MODEL, require
from app.utils import cache, ffmpeg_helpers, filepaths
from app.utils.retries import default_retry

_LOG_PREFIX = "[tts_service]"

# Gemini TTS returns raw PCM at this exact shape — hard-coded by the API.
_PCM_SAMPLE_RATE = 24_000
_PCM_SAMPLE_WIDTH_BYTES = 2  # 16-bit signed little-endian
_PCM_CHANNELS = 1  # mono


class TTSError(RuntimeError):
    """Raised when Gemini TTS returns an unusable response or the WAV is invalid."""


@default_retry
def _call_gemini_tts(client: genai.Client, narration: str, voice_name: str) -> bytes:
    """Invoke Gemini TTS and return the raw PCM bytes.

    Wrapped in `default_retry` so transient HTTP/5xx errors get backoff.
    Deterministic errors (e.g. 400 bad request) propagate as-is.
    """
    response = client.models.generate_content(
        model=GEMINI_TTS_MODEL,
        contents=narration,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name),
                )
            ),
        ),
    )
    try:
        pcm_bytes = response.candidates[0].content.parts[0].inline_data.data
    except (AttributeError, IndexError, TypeError) as exc:
        raise TTSError(f"Unexpected Gemini TTS response shape: {exc}") from exc

    if not pcm_bytes:
        raise TTSError("Gemini TTS returned empty audio payload")
    return pcm_bytes


def _write_wav(pcm_bytes: bytes, out_path: Path) -> None:
    """Wrap raw PCM in a RIFF/WAV container and write it to `out_path`."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(_PCM_CHANNELS)
        wav.setsampwidth(_PCM_SAMPLE_WIDTH_BYTES)
        wav.setframerate(_PCM_SAMPLE_RATE)
        wav.writeframes(pcm_bytes)


def _validate_wav(wav_path: Path) -> None:
    """Belt-and-suspenders check: ffprobe must report duration > 0.

    Deletes the wav and raises `TTSError` if the probe fails or reports 0s —
    Gemini could theoretically return silence-only or corrupted bytes.
    """
    try:
        duration = ffmpeg_helpers.probe_duration(wav_path)
    except ffmpeg_helpers.FFmpegError as exc:
        wav_path.unlink(missing_ok=True)
        raise TTSError(f"ffprobe rejected generated WAV: {exc.stderr}") from exc
    if duration <= 0:
        wav_path.unlink(missing_ok=True)
        raise TTSError(f"Generated WAV has non-positive duration: {duration}s")


def synthesize_narration(narration: str, job_id: str, *, voice_name: str = "Kore") -> Path:
    """Generate narration.wav via Gemini TTS.

    Returns the absolute path to the WAV. If an existing WAV's hash matches
    `hash_text(narration)`, returns the existing path without re-calling the API.
    Raises TTSError on unrecoverable API failure.
    """
    filepaths.ensure_job_dirs(job_id)
    wav_path = filepaths.narration_path(job_id)
    hash_path = filepaths.narration_hash_path(job_id)

    expected = cache.hash_text(narration)
    if cache.artifact_fresh(wav_path, hash_path, expected):
        print(f"{_LOG_PREFIX} cache hit {wav_path}")
        return wav_path

    client = genai.Client(api_key=require("GEMINI_API_KEY"))
    pcm_bytes = _call_gemini_tts(client, narration, voice_name)

    _write_wav(pcm_bytes, wav_path)
    _validate_wav(wav_path)
    cache.write_hash_file(hash_path, expected)

    print(f"{_LOG_PREFIX} wrote {wav_path} ({len(pcm_bytes)} PCM bytes)")
    return wav_path
