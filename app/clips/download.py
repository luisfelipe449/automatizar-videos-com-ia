"""Download clips with yt-dlp and make the low-res proxy the classifier watches.

Files live in `<CLIPS_DIR>/raw/<source>_<id>.mp4` (+ `.proxy.mp4`). A clip
counts as downloaded only when both files exist, so a crash between the two
steps redoes the pair on the next run. Neither file appears at its final name
until it is whole: yt-dlp writes `<name>.part` and renames on success, and the
proxy is encoded to `<name>.part.mp4` and renamed the same way. A killed or
timed-out step therefore never leaves a partial file that counts as done.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable

from sqlalchemy.orm import Session

from app.config import CLIPS_DIR
from app.db.models import Clip
from app.utils.ffmpeg_helpers import FFmpegError, run_ffmpeg

_LOG_PREFIX = "[clips.download]"
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_DOWNLOAD_TIMEOUT_S = 300


class DownloadError(RuntimeError):
    """A clip could not be fetched or converted."""


def raw_path_for(source: str, clip_id: str, *, base: Path = CLIPS_DIR) -> Path:
    return base / "raw" / f"{_UNSAFE.sub('_', source)}_{_UNSAFE.sub('_', clip_id)}.mp4"


def proxy_path_for(raw: Path) -> Path:
    return raw.with_name(raw.stem + ".proxy.mp4")


def build_download_cmd(url: str, dest: Path) -> list[str]:
    # yt-dlp runs as a module of this interpreter so the pinned version in
    # requirements.txt is the one used, not whatever is on PATH. Never pass
    # --no-part: the `.part` file is what keeps a partial download off `dest`.
    return [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", "--no-progress", "--quiet", "--no-warnings",
        "-f", "best[height<=1080]/best",
        "-o", str(dest),
        url,
    ]


def build_proxy_args(src: Path, dst: Path) -> list[str]:
    # 480p / 15 fps: what the classifier needs to judge a moment, at a size
    # that fits inline in one Gemini request.
    return [
        "-y", "-i", str(src),
        "-vf", "scale=-2:480,fps=15",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-c:a", "aac", "-b:a", "64k", "-ac", "1",
        str(dst),
    ]


def download_clip(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        build_download_cmd(url, dest),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_DOWNLOAD_TIMEOUT_S,
    )
    if result.returncode != 0 or not dest.exists():
        tail = "\n".join((result.stderr or "").splitlines()[-10:])
        raise DownloadError(f"yt-dlp failed for {url} (exit {result.returncode}): {tail}")


def make_proxy(src: Path, dst: Path, *, run: Callable[[list[str]], None] = run_ffmpeg) -> None:
    """Encode the proxy beside `dst` and rename it into place only on success."""
    # Same directory, so the rename is atomic; `.mp4` last so FFmpeg picks the muxer.
    tmp = dst.with_name(dst.stem + ".part.mp4")
    try:
        run(build_proxy_args(src, tmp))
        os.replace(tmp, dst)
    except FFmpegError as exc:
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"proxy failed for {src.name}: {exc}") from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _is_complete(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def ensure_downloaded(
    session: Session,
    clips: Iterable[Clip],
    *,
    base: Path = CLIPS_DIR,
    download_fn: Callable[[str, Path], None] = download_clip,
    proxy_fn: Callable[[Path, Path], None] = make_proxy,
) -> list[Clip]:
    """Make sure each clip has its file and proxy on disk.

    Returns the clips that are ready. A clip that fails is logged, keeps
    `local_path = NULL`, and is skipped; one bad clip never stops the rest.
    """
    ready: list[Clip] = []
    for clip in clips:
        raw = raw_path_for(clip.source, clip.source_clip_id, base=base)
        proxy = proxy_path_for(raw)
        try:
            if not _is_complete(raw):
                download_fn(clip.url, raw)
            if not _is_complete(proxy):
                proxy_fn(raw, proxy)
        except (DownloadError, subprocess.TimeoutExpired) as exc:
            print(f"{_LOG_PREFIX} skip {clip.source}:{clip.source_clip_id}: {exc}")
            clip.local_path = None
            continue
        clip.local_path = str(raw)
        ready.append(clip)
    session.commit()
    return ready
