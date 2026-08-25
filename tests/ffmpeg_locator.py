"""Locate a real ffmpeg binary for the integration tests.

The ffmpeg-dependent tests used to hard-code one developer's winget path, so
they silently skipped on every other machine. Resolution order:

  1. `FFMPEG_PATH` (set by `app.config`'s `load_dotenv`, i.e. the project `.env`)
  2. `ffmpeg` on PATH
  3. any winget-installed Gyan.FFmpeg build under the current user

Returns `None` when no binary is found, which callers turn into a skip.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_WINGET_GLOB = (
    "AppData/Local/Microsoft/WinGet/Packages/"
    "Gyan.FFmpeg_Microsoft.Winget.Source_*/ffmpeg-*-full_build/bin/ffmpeg.exe"
)


def find_ffmpeg() -> Path | None:
    """Return the path to a usable ffmpeg binary, or None."""
    # `app.config` loads the project .env at import time, so FFMPEG_PATH is
    # populated from there without the test having to parse .env itself.
    import app.config  # noqa: F401

    from_env = os.getenv("FFMPEG_PATH", "").strip()
    if from_env and Path(from_env).exists():
        return Path(from_env)

    on_path = shutil.which("ffmpeg")
    if on_path:
        return Path(on_path)

    matches = sorted(Path.home().glob(_WINGET_GLOB))
    if matches:
        return matches[-1]  # highest version, lexically

    return None
