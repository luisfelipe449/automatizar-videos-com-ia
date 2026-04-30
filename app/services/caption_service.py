"""Groq Whisper large-v3 -> word-level ASS captions (Phase 5 of the pipeline).

Single responsibility: take a narration WAV, ask Groq Whisper for word-level
timestamps, and write a chunked `captions.ass` next to it. The service always
re-transcribes when invoked; idempotency (cache-skip on existing `captions.ass`)
is the orchestrator's job.

The output file is an ASS (Advanced SubStation Alpha) script with karaoke
`\\k<cs>` tags so the render stage can burn in CapCut-style per-word
highlighting via libass, with no extra client-side work.

Karaoke approach (ASS-native `\\k`)
-----------------------------------
We emit one `Dialogue:` event per cue. Inside the text we wrap each word with
a `{\\k<centiseconds>}` tag, where the value is the word's duration. libass
paints the already-played portion in `SecondaryColour` (white) and the still-
to-play portion in `PrimaryColour` (yellow). The visible effect: as the cue
plays, each word crosses from yellow -> white at its spoken moment.

That is NOT pixel-identical to CapCut (where ALL non-current words stay white
and only the current one pops yellow), but it's the clean, single-line-per-cue
approach that's one-pass cheap to render and visually close enough for Shorts.
Getting exact CapCut behaviour would require emitting N Dialogue lines per cue
(one per word boundary), roughly tripling the file size -- explicitly a non-goal.
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

# --------------------------------------------------------------------------- #
# ASS style constants
# --------------------------------------------------------------------------- #

# ASS color format is &H<AA><BB><GG><RR> (BGR with alpha first; 00 alpha = opaque).
# Yellow #FFD700 -> &H0000D7FF (B=00, G=D7, R=FF).
_ASS_YELLOW = "&H0000D7FF"
# White #FFFFFF -> &H00FFFFFF.
_ASS_WHITE = "&H00FFFFFF"
# Pure black (outline / shadow background) -> &H00000000.
_ASS_BLACK = "&H00000000"

# Font / sizing. Bebas Neue ships in assets/fonts/; libass picks it up via
# the `fontsdir=` filter option from render_service.
_ASS_FONTNAME = "Bebas Neue"
_ASS_FONTSIZE = 84

# Layout: bottom-center (Alignment=2 in ASS numpad convention),
# lifted ~300px off the bottom so it clears YouTube's Shorts UI chrome.
_ASS_MARGIN_V = 300
_ASS_MARGIN_LR = 60

# Stroke / shadow. Bebas Neue's vertical strokes are thin -- Outline=3 gives
# a solid read against any background; Shadow=1 adds a subtle depth bump.
_ASS_OUTLINE = 3
_ASS_SHADOW = 1

# Playfield the timestamps/positions are authored against. We render 1080x1920,
# so matching avoids any implicit scaling by libass.
_ASS_PLAYRES_X = 1080
_ASS_PLAYRES_Y = 1920


class CaptionError(RuntimeError):
    """Raised when transcription fails or yields an unusable response."""


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O, no SDK -- fully unit-testable)
# --------------------------------------------------------------------------- #

def _format_ass_time(seconds: float) -> str:
    """Format `seconds` as ASS `H:MM:SS.cc` (single-digit hour, centiseconds).

    Clamps negatives to zero -- defensive; Whisper shouldn't return them.
    Note this differs from SRT: single hour digit, period + centiseconds
    instead of comma + milliseconds.
    """
    if seconds < 0:
        seconds = 0.0
    total_cs = int(round(seconds * 100))
    hours, remainder = divmod(total_cs, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, cs = divmod(remainder, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _group_words(words: list[dict], chunk_size: int) -> list[dict]:
    """Chunk `words` into cues of up to `chunk_size` words.

    Each cue is ``{"index": int, "start": float, "end": float, "words": list}``
    where `words` is the raw per-word list (each dict has `word`, `start`,
    `end`). Empty `words` -> empty list. Empty word strings are skipped so we
    never emit a blank cue. If a cue's overall `end <= start` we bump `end` by
    +0.1s so the cue stays on screen long enough to be visible.
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
        cleaned = [
            {"word": w["word"].strip(), "start": float(w["start"]), "end": float(w["end"])}
            for w in buffer
            if str(w.get("word", "")).strip()
        ]
        if not cleaned:
            buffer = []
            return
        start = cleaned[0]["start"]
        end = cleaned[-1]["end"]
        if end <= start:
            end = start + 0.1
        cues.append({"index": cue_index, "start": start, "end": end, "words": cleaned})
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


def _escape_ass_text(text: str) -> str:
    r"""Escape characters that would confuse the ASS event parser.

    Braces open/close override blocks; a literal backslash breaks tag parsing.
    Newlines become `\N` (ASS hard line break). We deliberately keep this
    minimal -- Whisper output is plain words, no exotic punctuation.
    """
    return (
        text.replace("\\", "\\\\")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", "\\N")
    )


def _build_dialogue_line(
    cue: dict,
    *,
    style: str = "Default",
) -> str:
    r"""Build a single ASS `Dialogue:` line for one cue with `\k` karaoke tags.

    Each word gets `{\k<cs>}<word>` where `<cs>` is the word's duration in
    centiseconds (rounded, min 1). Words are separated by a single space OUTSIDE
    the `{\k}` block so trailing whitespace doesn't count toward the highlight
    timing. Resulting text follows the format:

        {\k30}Word1 {\k25}Word2 {\k40}Word3

    `cue` must have keys `start`, `end`, `words` (list of {word, start, end}).
    """
    words = cue["words"]
    if not words:
        raise ValueError("cue has no words")

    parts: list[str] = []
    for w in words:
        duration_s = max(0.0, float(w["end"]) - float(w["start"]))
        # Minimum 1cs so a degenerate zero-length word still renders.
        cs = max(1, int(round(duration_s * 100)))
        word_text = _escape_ass_text(w["word"])
        parts.append(f"{{\\k{cs}}}{word_text}")
    text = " ".join(parts)

    start = _format_ass_time(float(cue["start"]))
    end = _format_ass_time(float(cue["end"]))
    # Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
    return f"Dialogue: 0,{start},{end},{style},,0,0,0,,{text}"


def _ass_header() -> str:
    """Return the fixed `[Script Info]` + `[V4+ Styles]` + `[Events]` preamble.

    Values track the constants at the top of the module so tests can assert
    on them without duplicating literals.
    """
    style_fields = [
        "Default",
        _ASS_FONTNAME,
        str(_ASS_FONTSIZE),
        _ASS_YELLOW,   # PrimaryColour -> yet-to-play (current / upcoming word)
        _ASS_WHITE,    # SecondaryColour -> already-played words
        _ASS_BLACK,    # OutlineColour
        _ASS_BLACK,    # BackColour (shadow)
        "0", "0", "0", "0",           # Bold, Italic, Underline, StrikeOut
        "100", "100",                 # ScaleX, ScaleY
        "0", "0",                     # Spacing, Angle
        "1",                          # BorderStyle (1 = outline + drop shadow)
        str(_ASS_OUTLINE),
        str(_ASS_SHADOW),
        "2",                          # Alignment: 2 = bottom-center
        str(_ASS_MARGIN_LR),
        str(_ASS_MARGIN_LR),
        str(_ASS_MARGIN_V),
        "1",                          # Encoding (1 = default)
    ]
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {_ASS_PLAYRES_X}\n"
        f"PlayResY: {_ASS_PLAYRES_Y}\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: {','.join(style_fields)}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )


def _render_ass(cues: list[dict]) -> str:
    """Serialize cues to a full ASS script (LF line endings)."""
    lines = [_ass_header()]
    for cue in cues:
        lines.append(_build_dialogue_line(cue) + "\n")
    return "".join(lines)


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
            language="en",
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

def transcribe(
    narration_wav: Path,
    job_id: str,
    *,
    words_per_chunk: int = 6,
    narration_hash: str | None = None,
) -> Path:
    """Transcribe the narration WAV and write captions.ass.

    Returns the absolute path to the ASS file. Raises CaptionError on API
    failure. Writes a sibling `captions.hash` so reruns with identical narration
    short-circuit without hitting Groq. If `narration_hash` is None the hash is
    derived from the WAV bytes; callers that already know the narration-text
    hash (e.g. the orchestrator) should pass it to keep the cache key consistent
    with `tts_service`.
    """
    narration_wav = Path(narration_wav)
    if not narration_wav.exists():
        raise CaptionError(f"narration WAV not found: {narration_wav}")

    filepaths.ensure_job_dirs(job_id)
    ass_path = filepaths.captions_path(job_id)
    hash_path = filepaths.captions_hash_path(job_id)

    if narration_hash is None:
        # Fall back to hashing the WAV bytes so the cache still works when the
        # caller can't supply the narration-text hash.
        narration_hash = cache.hash_text(narration_wav.read_bytes().hex())

    if cache.artifact_fresh(ass_path, hash_path, narration_hash):
        print(f"{_LOG_PREFIX} cache hit {ass_path}")
        return ass_path

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
            "Groq Whisper returned an empty word list -- cannot build captions."
        )

    cues = _group_words(words, words_per_chunk)
    if not cues:
        raise CaptionError(
            "Word list produced zero cues after chunking (all words empty?)."
        )

    ass_text = _render_ass(cues)
    # BOM-free UTF-8, LF line endings -- FFmpeg's `subtitles` filter is happiest
    # with this combination on Windows.
    ass_path.write_text(ass_text, encoding="utf-8", newline="\n")
    cache.write_hash_file(hash_path, narration_hash)
    print(f"{_LOG_PREFIX} wrote {ass_path} ({len(cues)} cues from {len(words)} words)")
    return ass_path
