"""Groq Whisper large-v3 → word-level SRT (Phase 5 of the pipeline).

Single responsibility: take a narration WAV, ask Groq Whisper for word-level
timestamps, and write a chunked `captions.srt` next to it. The service always
re-transcribes when invoked; idempotency (cache-skip on existing `captions.srt`)
is the orchestrator's job.
"""

from __future__ import annotations

from pathlib import Path

from groq import Groq

from app.config import require
from app.utils import cache, filepaths
from app.utils.retries import default_retry

_LOG_PREFIX = "[caption_service]"

# Whisper large-v3 is the current Groq speech-to-text flagship. Hard-coded
# here because the service is purpose-built around its response shape.
_WHISPER_MODEL = "whisper-large-v3"


class CaptionError(RuntimeError):
    """Raised when transcription fails or yields an unusable response."""


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O, no SDK — fully unit-testable)
# --------------------------------------------------------------------------- #

def _format_srt_time(seconds: float) -> str:
    """Format `seconds` as SRT `HH:MM:SS,mmm` (comma-separated milliseconds).

    Clamps negatives to zero — defensive; Whisper shouldn't return them.
    """
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _group_words(words: list[dict], chunk_size: int) -> list[dict]:
    """Chunk `words` into cues of up to `chunk_size` words.

    Each cue is ``{"index": int, "start": float, "end": float, "text": str}``.
    Empty `words` → empty list. Empty word strings are skipped so we never emit
    a blank cue. If `end <= start` we bump end by +0.1s so the cue stays on
    screen long enough to be visible.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")

    cues: list[dict] = []
    cue_index = 1
    buffer: list[dict] = []

    def flush() -> None:
        nonlocal cue_index, buffer
        if not buffer:
            return
        text = " ".join(w["word"].strip() for w in buffer if w["word"].strip()).strip()
        if not text:
            buffer = []
            return
        start = float(buffer[0]["start"])
        end = float(buffer[-1]["end"])
        if end <= start:
            end = start + 0.1
        cues.append({"index": cue_index, "start": start, "end": end, "text": text})
        cue_index += 1
        buffer = []

    for w in words:
        # Skip tokens with no printable content so they don't pad the chunk count.
        if not str(w.get("word", "")).strip():
            continue
        buffer.append(w)
        if len(buffer) >= chunk_size:
            flush()

    flush()
    return cues


def _render_srt(cues: list[dict]) -> str:
    """Serialize cues to SRT text (LF line endings, trailing blank line)."""
    blocks: list[str] = []
    for cue in cues:
        start = _format_srt_time(float(cue["start"]))
        end = _format_srt_time(float(cue["end"]))
        blocks.append(
            f"{int(cue['index'])}\n{start} --> {end}\n{cue['text']}\n"
        )
    # Each cue block already ends with \n; join with \n to insert the mandatory
    # blank line between cues, and finish with a trailing \n so the last cue
    # also has a separating newline (matches the SRT convention).
    return "\n".join(blocks) + ("\n" if blocks else "")


# --------------------------------------------------------------------------- #
# API call
# --------------------------------------------------------------------------- #

@default_retry
def _call_whisper(client: Groq, narration_wav: Path) -> object:
    """Invoke Groq Whisper and return the raw Transcription object.

    Wrapped in `default_retry` so transient HTTP/5xx errors are retried with
    backoff. Deterministic failures (e.g. 400 bad request) propagate as-is.
    """
    with open(narration_wav, "rb") as f:
        return client.audio.transcriptions.create(
            file=(narration_wav.name, f.read()),
            model=_WHISPER_MODEL,
            response_format="verbose_json",
            timestamp_granularities=["word"],
            language="pt",
        )


def _extract_words(transcription: object) -> list[dict]:
    """Normalize the Groq Transcription into a plain list of dicts.

    The SDK has shipped `.words` as both object attributes and plain dicts
    across versions, so we handle both shapes.
    """
    raw_words = getattr(transcription, "words", None)
    if raw_words is None and isinstance(transcription, dict):
        raw_words = transcription.get("words")
    raw_words = raw_words or []

    def _pick(item: object, key: str, default):
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    words: list[dict] = []
    for w in raw_words:
        words.append(
            {
                "word": _pick(w, "word", "") or "",
                "start": float(_pick(w, "start", 0.0) or 0.0),
                "end": float(_pick(w, "end", 0.0) or 0.0),
            }
        )
    return words


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def transcribe_to_srt(
    narration_wav: Path,
    job_id: str,
    *,
    words_per_chunk: int = 6,
    narration_hash: str | None = None,
) -> Path:
    """Transcribe the narration WAV and write captions.srt.

    Returns the absolute path to the SRT. Raises CaptionError on API failure.
    Writes a sibling `captions.hash` so reruns with identical narration
    short-circuit without hitting Groq. If `narration_hash` is None the hash is
    derived from the WAV bytes; callers that already know the narration-text
    hash (e.g. the orchestrator) should pass it to keep the cache key consistent
    with `tts_service`.
    """
    narration_wav = Path(narration_wav)
    if not narration_wav.exists():
        raise CaptionError(f"narration WAV not found: {narration_wav}")

    filepaths.ensure_job_dirs(job_id)
    srt_path = filepaths.captions_path(job_id)
    hash_path = filepaths.captions_hash_path(job_id)

    if narration_hash is None:
        # Fall back to hashing the WAV bytes so the cache still works when the
        # caller can't supply the narration-text hash.
        narration_hash = cache.hash_text(narration_wav.read_bytes().hex())

    if cache.artifact_fresh(srt_path, hash_path, narration_hash):
        print(f"{_LOG_PREFIX} cache hit {srt_path}")
        return srt_path

    client = Groq(api_key=require("GROQ_API_KEY"))
    try:
        transcription = _call_whisper(client, narration_wav)
    except FileNotFoundError as exc:
        # Defensive: the pre-flight existence check should have caught this,
        # but a race (e.g. the file gets deleted underneath us) surfaces here.
        raise CaptionError(f"narration WAV not found: {narration_wav}") from exc

    words = _extract_words(transcription)
    if not words:
        raise CaptionError(
            "Groq Whisper returned an empty word list — cannot build captions."
        )

    cues = _group_words(words, words_per_chunk)
    if not cues:
        raise CaptionError(
            "Word list produced zero cues after chunking (all words empty?)."
        )

    srt_text = _render_srt(cues)
    # BOM-free UTF-8, LF line endings — FFmpeg's `subtitles` filter is happiest
    # with this combination on Windows.
    srt_path.write_text(srt_text, encoding="utf-8", newline="\n")
    cache.write_hash_file(hash_path, narration_hash)
    print(f"{_LOG_PREFIX} wrote {srt_path} ({len(cues)} cues from {len(words)} words)")
    return srt_path
