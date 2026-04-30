"""Compose scene images + narration + ASS captions into a 9:16 Short (Phase 7).

Single responsibility: build one `filter_complex` graph and invoke FFmpeg once.
Ken Burns zoom on every scene, hard cuts between scenes, ASS karaoke captions
burnt in via `libass`. Optionally mixes a random CC0 background track under
the narration at a fixed -20 dB. No transitions.

The filter-graph builder (`_build_ffmpeg_command`) is pure and side-effect-free
so it can be snapshot-tested without touching ffmpeg.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import ASSETS_DIR
from app.models import RenderSpec
from app.utils.ffmpeg_helpers import (
    FFmpegError,
    ffmpeg_bin,
    probe_duration,
    probe_resolution,
    run_ffmpeg,
)

_LOG_PREFIX = "[render_service]"

# Output framerate. zoompan's `d=` is in output frames, so we must multiply
# each scene's duration_seconds by this value when building the graph.
_FPS = 30

# Ken Burns: zoom from 1.0 -> 1.2 over the scene. 0.0015 per frame at 30 fps
# reaches ~1.2x in ~4.4s, so short scenes get a subtle nudge and long scenes
# cap out at 1.2x (the `min(...)` clamp).
_ZOOM_EXPR = "min(1.0+0.0015*on,1.2)"

# Directory holding bundled fonts (e.g. Bebas Neue). Passed to libass via
# `fontsdir=` so we don't need to install fonts system-wide on Windows.
_FONTS_DIR: Path = ASSETS_DIR / "fonts"

# Directory holding bundled CC0 background-music tracks. Missing/empty dir
# means "no music" — the pipeline degrades gracefully.
_MUSIC_DIR: Path = ASSETS_DIR / "input" / "music"

_MAX_DURATION_S = 60.0


class RenderError(RuntimeError):
    """Raised when ffmpeg fails or the output fails post-render validation."""


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def _escape_subtitle_path(p: Path) -> str:
    r"""Return an FFmpeg-safe representation of `p` for the `subtitles=` filter.

    FFmpeg's filter-graph parser treats ``:`` as an argument separator and ``\``
    as an escape introducer. On Windows the absolute path contains a drive
    letter colon ("C:/..."), which must be escaped once for the filter parser.
    We also normalise backslashes to forward slashes so libass is happy.

    The returned string does NOT include the outer single quotes — the caller
    wraps it in quotes when embedding in filter_complex.
    """
    # Use absolute, forward-slash form: "C:/Users/.../captions.ass".
    posix = Path(p).resolve().as_posix()
    # Escape the drive-letter colon for the filter parser: "C\:/Users/..."
    # Only the first colon matters on Windows; POSIX paths have no colon.
    return posix.replace(":", r"\:")


def _pick_music_track(job_id: str) -> Path | None:
    """Return a deterministic `*.mp3` track from `_MUSIC_DIR`, or None.

    Pure-ish helper — the only IO is listing the directory. Returns None when
    the directory is missing or empty, so the pipeline degrades gracefully to
    "no music" rather than crashing.

    The pick is a sha256(job_id) mod len(tracks): same job_id always picks the
    same track (reproducible re-renders), and different job_ids rotate through
    the library naturally.
    """
    if not _MUSIC_DIR.is_dir():
        return None
    tracks = sorted(_MUSIC_DIR.glob("*.mp3"))
    if not tracks:
        return None
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    idx = int(digest, 16) % len(tracks)
    return tracks[idx]


def _build_filter_graph(
    spec: RenderSpec,
    durations: list[float] | None = None,
    music_path: Path | None = None,
) -> str:
    """Build the `filter_complex` string. Pure: string in → string out.

    `durations` overrides `scene.duration_seconds` when provided (e.g. scaled
    to match the narration audio length). Must have the same length as
    `spec.scenes`.

    When `music_path` is set, the narration (input index `len(scenes)`) is
    mixed with the music track (input index `len(scenes)+1`) at a fixed -20 dB
    (linear amplitude 0.1) via `amix=duration=first`, so the mixed output
    ends when the narration ends regardless of music length.
    """
    if not spec.scenes:
        raise ValueError("RenderSpec.scenes must be non-empty")
    if durations is not None and len(durations) != len(spec.scenes):
        raise ValueError("durations length must match scenes length")

    w, h = spec.width, spec.height
    chains: list[str] = []
    concat_inputs = ""

    for idx, scene in enumerate(spec.scenes):
        dur = float(durations[idx]) if durations is not None else float(scene.duration_seconds)
        # zoompan `d=` is in output frames; round up so no scene is clipped short.
        frames = max(1, int(round(dur * _FPS)))
        # Scale (preserving aspect, cropping overflow) so zoompan sees a
        # correctly sized frame regardless of the source image's aspect.
        chain = (
            f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},"
            f"zoompan=z='{_ZOOM_EXPR}':d={frames}:s={w}x{h}:fps={_FPS}"
            f"[v{idx}]"
        )
        chains.append(chain)
        concat_inputs += f"[v{idx}]"

    sub_path = _escape_subtitle_path(spec.captions_path)
    fonts_dir = _escape_subtitle_path(_FONTS_DIR)
    # Subtitles go AFTER concat so they render over the final timeline.
    # No `force_style=` here: the .ass file already carries its own Style
    # block (including the karaoke-colored PrimaryColour / SecondaryColour).
    # force_style would override them and kill the yellow/white highlight.
    concat_and_subs = (
        f"{concat_inputs}concat=n={len(spec.scenes)}:v=1:a=0,"
        f"subtitles='{sub_path}':fontsdir='{fonts_dir}'"
        f"[v]"
    )

    graph_parts = chains + [concat_and_subs]

    if music_path is not None:
        # Narration is input index len(scenes); music is len(scenes)+1.
        # -20 dB ≈ linear 0.1 amplitude (we use the numeric form — `volume=-20dB`
        # would also work but 0.1 is unambiguous and matches the tests).
        # duration=first pins the mixed output to the narration length so the
        # (longer) music track is cut naturally. `-shortest` still helps but
        # this is more explicit.
        n = len(spec.scenes)
        narration_chain = f"[{n}:a]anull[a_narr]"
        music_chain = f"[{n + 1}:a]volume=0.1[a_bg]"
        mix_chain = "[a_narr][a_bg]amix=inputs=2:duration=first:dropout_transition=0[a]"
        graph_parts.extend([narration_chain, music_chain, mix_chain])

    return ";".join(graph_parts)


def _scale_scene_durations(
    scenes: list, audio_duration_s: float
) -> list[float]:
    """Rescale each scene's duration so their sum equals `audio_duration_s`.

    Preserves the relative weights from the script — a scene that was planned
    for twice as long as another still ends up twice as long. Called when the
    real TTS output is shorter (common) or longer than the script estimate.
    """
    weights = [float(s.duration_seconds) for s in scenes]
    total = sum(weights)
    if total <= 0:
        # Defensive: fall back to equal splits.
        return [audio_duration_s / len(scenes)] * len(scenes)
    return [(w / total) * audio_duration_s for w in weights]


def _build_ffmpeg_command(
    spec: RenderSpec,
    ffmpeg_bin_path: str,
    durations: list[float] | None = None,
    music_path: Path | None = None,
) -> list[str]:
    """Return the full argv for `subprocess.run`. Pure — no IO.

    `durations` (optional) overrides scene durations in both the filter graph
    and any per-scene flags. Must match `len(spec.scenes)`. When omitted, the
    scene's own `duration_seconds` is used.

    `music_path` (optional): if set, the mp3 is added as an extra `-i` input
    AFTER the narration and the filter graph mixes it under the narration at
    -20 dB. `-map` then points at the mixed `[a]` output instead of the raw
    narration stream.

    Layout (no music):
        ffmpeg  -framerate 1 -loop 1 -t 1 -i img_1  ...  -i img_N
                -i narration.wav
                -filter_complex "<graph>"
                -map "[v]" -map <N>:a
                -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p
                -c:a aac -b:a 128k
                -movflags +faststart
                -shortest
                <output>

    Layout (with music): identical except a trailing `-i <music>.mp3` is
    appended after narration and `-map <N>:a` becomes `-map "[a]"`.
    """
    if not spec.scenes:
        raise ValueError("RenderSpec.scenes must be non-empty")
    if len(spec.image_paths) != len(spec.scenes):
        # RenderSpec already validates this, but keep the builder defensive.
        raise ValueError("image_paths length must match scenes length")

    cmd: list[str] = [ffmpeg_bin_path, "-y"]

    # One still image per scene. We feed ONE input frame at 1 fps: zoompan
    # then generates `d` output frames from that single input frame. Using
    # `-loop 1 -t D` here instead would spam zoompan with D*fps duplicate
    # input frames and visually freeze the scene on the first frame.
    for image_path in spec.image_paths:
        cmd += [
            "-framerate", "1",
            "-loop", "1",
            "-t", "1",
            "-i", str(image_path),
        ]

    # Narration audio — its input index is len(scenes).
    audio_input_index = len(spec.scenes)
    cmd += ["-i", str(spec.narration_path)]

    # Optional music input — index len(scenes)+1. Must come AFTER narration
    # so the filter graph's fixed input indices line up.
    if music_path is not None:
        cmd += ["-i", str(music_path)]

    cmd += ["-filter_complex", _build_filter_graph(spec, durations, music_path)]
    audio_map = "[a]" if music_path is not None else f"{audio_input_index}:a"
    cmd += ["-map", "[v]", "-map", audio_map]
    cmd += [
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-r", str(_FPS),
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-shortest",
        str(spec.output_path),
    ]
    return cmd


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def render_short(spec: RenderSpec) -> Path:
    """Render the final Short MP4.

    Returns the absolute path to the MP4. Raises RenderError on failure.
    Validates the output post-render (duration ≤60s, resolution matches spec).
    """
    # Pre-flight: make sure every input exists so ffmpeg's error isn't the
    # first place we learn about a missing file.
    for image_path in spec.image_paths:
        if not Path(image_path).exists():
            raise RenderError(f"scene image not found: {image_path}")
    if not Path(spec.narration_path).exists():
        raise RenderError(f"narration not found: {spec.narration_path}")
    if not Path(spec.captions_path).exists():
        raise RenderError(f"captions not found: {spec.captions_path}")

    output_path = Path(spec.output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Scale scene durations to match the real narration length so no scene is
    # cut off by `-shortest` and the final video length matches the audio.
    audio_duration = probe_duration(Path(spec.narration_path))
    scaled = _scale_scene_durations(spec.scenes, audio_duration)
    script_total = sum(int(s.duration_seconds) for s in spec.scenes)
    if abs(audio_duration - script_total) > 0.5:
        print(
            f"{_LOG_PREFIX} rescaling scenes: script={script_total}s -> "
            f"audio={audio_duration:.2f}s"
        )

    # Pick a background-music track deterministically from the bundled CC0
    # library. Missing/empty dir -> None, and rendering continues without music.
    music_path = _pick_music_track(spec.job_id)
    music_label = music_path.name if music_path is not None else "(none)"

    cmd = _build_ffmpeg_command(spec, ffmpeg_bin(), scaled, music_path)
    print(
        f"{_LOG_PREFIX} running: ffmpeg "
        f"{len(spec.scenes)} scene(s) + narration + music={music_label} "
        f"-> {output_path.name}"
    )

    try:
        # `run_ffmpeg` prepends the binary itself, so we hand it argv-minus-bin.
        run_ffmpeg(cmd[1:])
    except FFmpegError as exc:
        # Clean up partial output so a retry isn't fooled into reusing it.
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass
        raise RenderError(f"ffmpeg failed: {exc}") from exc

    # Post-render validation. Any failure here is a hard error — the caller
    # would rather see a clear message than ship a broken Short.
    duration = probe_duration(output_path)
    if duration > _MAX_DURATION_S:
        raise RenderError(
            f"output duration {duration:.2f}s exceeds {_MAX_DURATION_S:.0f}s"
        )

    width, height = probe_resolution(output_path)
    if (width, height) != (spec.width, spec.height):
        raise RenderError(
            f"output resolution {width}x{height} does not match spec "
            f"{spec.width}x{spec.height}"
        )

    print(
        f"{_LOG_PREFIX} output: {output_path} "
        f"({duration:.2f}s, {width}x{height})"
    )
    return output_path
