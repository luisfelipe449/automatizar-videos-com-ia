"""Compose scene images + narration + SRT into a 9:16 Short (Phase 7).

Single responsibility: build one `filter_complex` graph and invoke FFmpeg once.
Ken Burns zoom on every scene, hard cuts between scenes, SRT burnt in via
`libass`. No transitions, no music bed — that's future scope.

The filter-graph builder (`_build_ffmpeg_command`) is pure and side-effect-free
so it can be snapshot-tested without touching ffmpeg.
"""

from __future__ import annotations

from pathlib import Path

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

# Ken Burns: zoom from 1.0 → 1.2 over the scene. 0.0015 per frame at 30 fps
# reaches ~1.2× in ~4.4s, so short scenes get a subtle nudge and long scenes
# cap out at 1.2× (the `min(...)` clamp).
_ZOOM_EXPR = "min(1.0+0.0015*on,1.2)"

# Burnt-in subtitle look. Matches the PLANO.md spec.
_SUBTITLE_STYLE = "Fontsize=14,Alignment=2,MarginV=200,Outline=2"

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
    # Use absolute, forward-slash form: "C:/Users/.../captions.srt".
    posix = Path(p).resolve().as_posix()
    # Escape the drive-letter colon for the filter parser: "C\:/Users/..."
    # Only the first colon matters on Windows; POSIX paths have no colon.
    return posix.replace(":", r"\:")


def _build_filter_graph(spec: RenderSpec) -> str:
    """Build the `filter_complex` string. Pure: string in → string out."""
    if not spec.scenes:
        raise ValueError("RenderSpec.scenes must be non-empty")

    w, h = spec.width, spec.height
    chains: list[str] = []
    concat_inputs = ""

    for idx, scene in enumerate(spec.scenes):
        dur = int(scene.duration_seconds)
        frames = dur * _FPS
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
    # Subtitles go AFTER concat so they render over the final timeline.
    concat_and_subs = (
        f"{concat_inputs}concat=n={len(spec.scenes)}:v=1:a=0,"
        f"subtitles='{sub_path}':force_style='{_SUBTITLE_STYLE}'"
        f"[v]"
    )

    return ";".join(chains + [concat_and_subs])


def _build_ffmpeg_command(spec: RenderSpec, ffmpeg_bin_path: str) -> list[str]:
    """Return the full argv for `subprocess.run`. Pure — no IO.

    Layout:
        ffmpeg  -loop 1 -t D -i img_1  ...  -loop 1 -t D -i img_N
                -i narration.wav
                -filter_complex "<graph>"
                -map "[v]" -map <N>:a
                -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p
                -c:a aac -b:a 128k
                -movflags +faststart
                -shortest
                <output>
    """
    if not spec.scenes:
        raise ValueError("RenderSpec.scenes must be non-empty")
    if len(spec.image_paths) != len(spec.scenes):
        # RenderSpec already validates this, but keep the builder defensive.
        raise ValueError("image_paths length must match scenes length")

    cmd: list[str] = [ffmpeg_bin_path, "-y"]

    # One looped still image per scene.
    for scene, image_path in zip(spec.scenes, spec.image_paths):
        cmd += [
            "-loop", "1",
            "-t", str(int(scene.duration_seconds)),
            "-i", str(image_path),
        ]

    # Narration audio — its input index is len(scenes).
    audio_input_index = len(spec.scenes)
    cmd += ["-i", str(spec.narration_path)]

    cmd += ["-filter_complex", _build_filter_graph(spec)]
    cmd += ["-map", "[v]", "-map", f"{audio_input_index}:a"]
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

    cmd = _build_ffmpeg_command(spec, ffmpeg_bin())
    print(
        f"{_LOG_PREFIX} running: ffmpeg "
        f"{len(spec.scenes)} scene(s) + narration -> {output_path.name}"
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
