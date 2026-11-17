"""Subprocess wrappers around `ffmpeg` / `ffprobe`, plus filter-argument escaping.

Zero opinion about filters — this module resolves the binary, runs a
command, surfaces a tail of stderr on failure, exposes two probes, and
escapes paths and text for embedding in a filter graph.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.config import FFMPEG_PATH

_STDERR_TAIL_LINES = 40
# stdin=DEVNULL: ffmpeg polls stdin for interactive keys ('q' to quit). With an
# inherited stdin that never closes (WSL → Windows interop, pytest), it can
# block until the subprocess timeout instead of finishing the encode.
_RUN_KW = {
    "capture_output": True,
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
    "stdin": subprocess.DEVNULL,
}


class FFmpegError(RuntimeError):
    """Raised when ffmpeg returns non-zero. `.stderr` holds the tail."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"ffmpeg failed (exit {returncode}): {' '.join(cmd)}\n"
            f"--- stderr tail ---\n{stderr}"
        )
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr


def _resolve(tool: str) -> str:
    """Resolve `tool` ('ffmpeg' or 'ffprobe') from FFMPEG_PATH or PATH."""
    configured = (FFMPEG_PATH or "").strip()
    if configured and Path(configured).exists():
        if tool == "ffmpeg":
            return configured
        probe_name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
        sibling = Path(configured).with_name(probe_name)
        if sibling.exists():
            return str(sibling)
    try:
        result = subprocess.run([tool, "-version"], timeout=10, **_RUN_KW)
        if result.returncode == 0:
            return tool
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    raise FileNotFoundError(
        f"{tool} not found. Set FFMPEG_PATH in .env or install ffmpeg on PATH."
    )


def ffmpeg_bin() -> str:
    return _resolve("ffmpeg")


def ffprobe_bin() -> str:
    return _resolve("ffprobe")


def run_ffmpeg_capture(args: list[str], *, timeout: int = 600) -> str:
    """Run `ffmpeg <args>` and return its stdout (e.g. a filter printing to `file=-`)."""
    cmd = [ffmpeg_bin(), *args]
    result = subprocess.run(cmd, timeout=timeout, **_RUN_KW)
    if result.returncode != 0:
        tail = os.linesep.join((result.stderr or "").splitlines()[-_STDERR_TAIL_LINES:])
        raise FFmpegError(cmd, result.returncode, tail)
    return result.stdout or ""


def run_ffmpeg(args: list[str], *, timeout: int = 600) -> None:
    """Run `ffmpeg <args>`; raise FFmpegError with stderr tail on failure."""
    run_ffmpeg_capture(args, timeout=timeout)


def _run_probe(cmd: list[str]) -> str:
    result = subprocess.run(cmd, timeout=30, **_RUN_KW)
    if result.returncode != 0:
        raise FFmpegError(cmd, result.returncode, (result.stderr or "").strip())
    return result.stdout.strip()


def probe_duration(path: Path) -> float:
    """Return media duration in seconds via `ffprobe -show_format`."""
    out = _run_probe([
        ffprobe_bin(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    return float(out)


def probe_resolution(path: Path) -> tuple[int, int]:
    """Return (width, height) of the first video stream."""
    out = _run_probe([
        ffprobe_bin(), "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        str(path),
    ])
    width, height = out.split("x", 1)
    return int(width), int(height)


def escape_filter_path(p: Path) -> str:
    r"""Return an FFmpeg-safe representation of `p` for a filter argument
    (`subtitles=`, `fontfile=`, `textfile=`).

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


def escape_drawtext(text: str) -> str:
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
