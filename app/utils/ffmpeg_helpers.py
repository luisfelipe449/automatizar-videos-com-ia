"""Subprocess wrappers around `ffmpeg` / `ffprobe`.

Zero opinion about filters — this module only resolves the binary, runs a
command, surfaces a tail of stderr on failure, and exposes two probes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.config import FFMPEG_PATH

_STDERR_TAIL_LINES = 40
_RUN_KW = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}


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


def run_ffmpeg(args: list[str], *, timeout: int = 600) -> None:
    """Run `ffmpeg <args>`; raise FFmpegError with stderr tail on failure."""
    cmd = [ffmpeg_bin(), *args]
    result = subprocess.run(cmd, timeout=timeout, **_RUN_KW)
    if result.returncode != 0:
        tail = os.linesep.join((result.stderr or "").splitlines()[-_STDERR_TAIL_LINES:])
        raise FFmpegError(cmd, result.returncode, tail)


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
