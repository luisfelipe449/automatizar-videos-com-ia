"""Compose scene images + narration + ASS captions into a 9:16 Short (Phase 7).

Single responsibility: build one `filter_complex` graph and invoke FFmpeg once.
Ken Burns zoom on every scene, `xfade` crossfade transitions between consecutive
scenes, ASS karaoke captions burnt in via `libass`. Optionally mixes a random
CC0 background track under the narration at a fixed -20 dB.

The filter-graph builder (`_build_ffmpeg_command`) is pure and side-effect-free
so it can be snapshot-tested without touching ffmpeg.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import ASSETS_DIR
from app.models import RenderSpec, Scene
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

# Ken Burns: zoom from 1.0 -> 1.18 over the scene, anchored at the image
# centre. 0.0013 per frame at 30 fps reaches 1.18x in ~4.6s, so short scenes
# get a subtle nudge and long scenes cap out at 1.18x (the `min(...)` clamp).
# Centre-anchoring (via x/y expressions below) keeps the subject on-axis
# instead of zoompan's default top-left pan.
_ZOOM_EXPR = "min(1.0+0.0013*on,1.18)"
_ZOOM_X_EXPR = "iw/2-(iw/zoom/2)"
_ZOOM_Y_EXPR = "ih/2-(ih/zoom/2)"

# Directory holding bundled fonts (e.g. Bebas Neue). Passed to libass via
# `fontsdir=` so we don't need to install fonts system-wide on Windows.
_FONTS_DIR: Path = ASSETS_DIR / "fonts"

# Directory holding bundled CC0 background-music tracks. Missing/empty dir
# means "no music" — the pipeline degrades gracefully.
_MUSIC_DIR: Path = ASSETS_DIR / "input" / "music"

_MAX_DURATION_S = 60.0

# Crossfade duration between consecutive scenes, in seconds. Each xfade overlaps
# the tail of the previous stream with the head of the next by this amount, so
# the final output duration is `sum(durations) - (N-1) * _XFADE_DURATION`. Any
# scene shorter than this value cannot host an xfade and is rejected upstream.
_XFADE_DURATION = 0.4

# FFmpeg `xfade` transition kind. "fade" is a plain luma crossfade; other
# options (fadeblack, dissolve, wipeleft, ...) are tunable later.
_XFADE_TRANSITION = "fade"


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


def _escape_drawtext(text: str) -> str:
    r"""Return a drawtext-safe representation of `text`.

    Two layers of escaping apply when this string is inserted into
    ``...text='<this>'...`` inside a filter_complex argument:

    1. **Shell-like quoting layer**: single quotes wrap the value, so a
       literal ``'`` must be written as ``'\''`` (close quote, escaped quote,
       reopen quote). Do this FIRST, before any backslash mangling.
    2. **Filter-arg layer**: ``:`` separates args, ``\`` is an escape char, and
       ``%`` triggers drawtext's text-expansion. Escape each with a backslash.

    Backslashes themselves are NOT doubled — drawtext reads the string after
    the filter parser has consumed one level of backslash escapes, so passing
    a raw ``\`` through would be interpreted as starting an escape sequence.
    Whisper output is plain Latin text, so backslashes are extremely unlikely;
    we still escape them as ``\\`` for safety.
    """
    # Order matters and is tricky:
    # 1. Escape backslashes FIRST — before we inject our own.
    # 2. Escape colon and percent for the filter-arg layer.
    # 3. LAST: replace bare single quotes with the quote-toggle sequence
    #    "'\\''". This has to come last because the replacement itself
    #    contains a backslash that MUST NOT be re-escaped.
    return (
        text
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace(":", "\\:")
        .replace("'", "'\\''")
    )


def _build_rank_overlay(scene: Scene) -> str:
    """Return a drawtext filter fragment for the rank overlay, or ''.

    The fragment INCLUDES the leading comma so it slots straight after the
    zoompan node in the per-scene chain. Empty string when the scene has no
    rank (which is every scene for non-ranking topics, and the hook/intro/CTA/
    closing scenes for ranking topics).

    Visual spec: Bebas Neue ~120pt, top-center (y=80), white fill with a
    6-px black outline. Matches the caption style so ranked shorts read as a
    single design.
    """
    if scene.rank is None:
        return ""
    text = f"{scene.rank} - {scene.rank_title}"
    escaped = _escape_drawtext(text)
    # The drawtext `fontfile=` arg accepts the same forward-slash + escaped
    # colon form that `subtitles=` uses on Windows, so we reuse the helper.
    font = _escape_subtitle_path(_FONTS_DIR / "BebasNeue-Regular.ttf")
    return (
        f",drawtext=fontfile='{font}':text='{escaped}':"
        f"fontcolor=white:fontsize=120:bordercolor=black:borderw=6:"
        f"x=(w-text_w)/2:y=80"
    )


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
    per_scene_durations: list[float] = []

    for idx, scene in enumerate(spec.scenes):
        dur = float(durations[idx]) if durations is not None else float(scene.duration_seconds)
        per_scene_durations.append(dur)
        # zoompan `d=` is in output frames; round up so no scene is clipped short.
        frames = max(1, int(round(dur * _FPS)))
        # Scale (preserving aspect, cropping overflow) so zoompan sees a
        # correctly sized frame regardless of the source image's aspect.
        # The optional rank-overlay runs AFTER zoompan so the text isn't
        # scaled/cropped along with the image, and BEFORE the xfade merge
        # so it fades out with its own scene.
        rank_overlay = _build_rank_overlay(scene)
        chain = (
            f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},"
            f"zoompan=z='{_ZOOM_EXPR}':x='{_ZOOM_X_EXPR}':y='{_ZOOM_Y_EXPR}':"
            f"d={frames}:s={w}x{h}:fps={_FPS}"
            f"{rank_overlay}"
            f"[v{idx}]"
        )
        chains.append(chain)

    # Validate every scene can host an xfade. Only enforced when there are 2+
    # scenes (single-scene specs never crossfade). Scaling upstream generally
    # keeps scenes well above the floor; this is a belt-and-braces guard.
    if len(spec.scenes) >= 2:
        for idx, dur in enumerate(per_scene_durations):
            if dur < _XFADE_DURATION:
                raise RenderError(
                    f"scene {idx} duration {dur:.3f}s is shorter than "
                    f"xfade duration {_XFADE_DURATION}s; cannot crossfade. "
                    f"Consider fewer scenes or a longer narration."
                )

    # Chain pairwise xfades. Offset of the k-th xfade is measured on the
    # CUMULATIVE output timeline (accounting for prior overlaps):
    #   offset_k = sum(dur[0..k]) - (k+1) * XFADE
    # Single-scene case: no xfade, the raw [v0] stream is piped straight into
    # the subtitle filter.
    n_scenes = len(spec.scenes)
    if n_scenes == 1:
        pre_subs_label = "[v0]"
        xfade_chains: list[str] = []
    else:
        xfade_chains = []
        cumulative = 0.0
        prev_label = "[v0]"
        for k in range(n_scenes - 1):
            cumulative += per_scene_durations[k]
            offset = cumulative - (k + 1) * _XFADE_DURATION
            out_label = f"[vx{k}]"
            xfade_chains.append(
                f"{prev_label}[v{k + 1}]"
                f"xfade=transition={_XFADE_TRANSITION}:"
                f"duration={_XFADE_DURATION}:offset={offset:.6f}"
                f"{out_label}"
            )
            prev_label = out_label
        pre_subs_label = prev_label

    sub_path = _escape_subtitle_path(spec.captions_path)
    fonts_dir = _escape_subtitle_path(_FONTS_DIR)
    # Subtitles go AFTER the xfade chain so they render over the final timeline.
    # No `force_style=` here: the .ass file already carries its own Style block
    # (including the karaoke-colored PrimaryColour / SecondaryColour).
    # force_style would override them and kill the yellow/white highlight.
    subs_chain = (
        f"{pre_subs_label}"
        f"subtitles='{sub_path}':fontsdir='{fonts_dir}'"
        f"[v]"
    )

    graph_parts = chains + xfade_chains + [subs_chain]

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
    """Rescale each scene's duration so the xfaded timeline matches the audio.

    Preserves the relative weights from the script — a scene that was planned
    for twice as long as another still ends up twice as long. Called when the
    real TTS output is shorter (common) or longer than the script estimate.

    Because consecutive scenes crossfade (overlap by `_XFADE_DURATION`), the
    final video length is `sum(scaled) - (N - 1) * _XFADE_DURATION`. To hit
    `audio_duration_s` at the output, we scale towards a sum that COMPENSATES
    for those overlaps:
        target_sum = audio_duration_s + (N - 1) * _XFADE_DURATION
    For single-scene specs the compensation collapses to zero, matching the
    pre-xfade behaviour exactly.

    If any scaled duration falls below `_XFADE_DURATION`, raises RenderError
    — xfade can't fit. In practice with 9-13 scenes over ~45-58s audio this
    never trips, but short narrations with many scenes can (and should fail
    loudly).
    """
    n = len(scenes)
    # Only compensate when there's an xfade to compensate for.
    overlap_total = (n - 1) * _XFADE_DURATION if n >= 2 else 0.0
    target_sum = audio_duration_s + overlap_total

    weights = [float(s.duration_seconds) for s in scenes]
    total = sum(weights)
    if total <= 0:
        # Defensive: fall back to equal splits of the compensated target.
        scaled = [target_sum / n] * n
    else:
        scaled = [(w / total) * target_sum for w in weights]

    if n >= 2:
        for idx, dur in enumerate(scaled):
            if dur < _XFADE_DURATION:
                raise RenderError(
                    f"scene {idx} scaled duration {dur:.3f}s is shorter than "
                    f"xfade duration {_XFADE_DURATION}s; cannot crossfade. "
                    f"Audio is {audio_duration_s:.2f}s across {n} scenes — "
                    f"consider fewer scenes or a longer narration."
                )
    return scaled


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
