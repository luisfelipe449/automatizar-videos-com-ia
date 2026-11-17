"""Clip download (yt-dlp) and classifier proxy (FFmpeg)."""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.clips import download
from app.clips.download import (
    DownloadError,
    build_download_cmd,
    build_proxy_args,
    ensure_downloaded,
    proxy_path_for,
    raw_path_for,
)
from app.db.database import Base, get_engine, get_session
from app.db.models import Clip
from app.utils.ffmpeg_helpers import FFmpegError


def test_raw_path_is_per_source_and_sanitized(tmp_path):
    assert raw_path_for("twitch", "Abc-1_x", base=tmp_path) == tmp_path / "raw" / "twitch_Abc-1_x.mp4"
    # Anything outside [A-Za-z0-9_-] is replaced, so an id can never escape the dir.
    assert raw_path_for("kick", "../../etc", base=tmp_path) == tmp_path / "raw" / "kick_______etc.mp4"


def test_proxy_sits_next_to_raw(tmp_path):
    raw = tmp_path / "raw" / "twitch_a.mp4"
    assert proxy_path_for(raw) == tmp_path / "raw" / "twitch_a.proxy.mp4"


def test_download_cmd_runs_pinned_yt_dlp_module():
    cmd = build_download_cmd("https://clips.twitch.tv/Abc", Path("out/twitch_Abc.mp4"))
    assert cmd[:3] == [sys.executable, "-m", "yt_dlp"]
    assert cmd[-1] == "https://clips.twitch.tv/Abc"
    assert cmd[cmd.index("-f") + 1] == "best[height<=1080]/best"
    assert cmd[cmd.index("-o") + 1] == str(Path("out/twitch_Abc.mp4"))
    assert "--no-playlist" in cmd


def test_download_cmd_keeps_yt_dlp_part_files():
    # yt-dlp writes `<dest>.part` and renames it on success, so a killed or
    # failed download never leaves a partial file at `dest`. --no-part would
    # break that.
    cmd = build_download_cmd("https://clips.twitch.tv/Abc", Path("out/twitch_Abc.mp4"))
    assert "--no-part" not in cmd


def _partial_writer(exc: BaseException | None):
    calls: list[list[str]] = []

    def run(args: list[str]) -> None:
        calls.append(args)
        Path(args[-1]).write_bytes(b"half a video")
        if exc is not None:
            raise exc
    return run, calls


def test_proxy_appears_only_when_ffmpeg_succeeds(tmp_path):
    src, dst = tmp_path / "a.mp4", tmp_path / "a.proxy.mp4"
    run, calls = _partial_writer(None)
    download.make_proxy(src, dst, run=run)
    assert Path(calls[0][-1]) != dst and Path(calls[0][-1]).parent == tmp_path
    assert dst.read_bytes() == b"half a video"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.proxy.mp4"]


@pytest.mark.parametrize("exc", [
    FFmpegError(["ffmpeg"], 1, "killed"),
    subprocess.TimeoutExpired(["ffmpeg"], 600),
])
def test_failed_proxy_leaves_no_partial_file(tmp_path, exc):
    src, dst = tmp_path / "a.mp4", tmp_path / "a.proxy.mp4"
    run, _ = _partial_writer(exc)
    with pytest.raises((DownloadError, subprocess.TimeoutExpired)):
        download.make_proxy(src, dst, run=run)
    assert list(tmp_path.iterdir()) == []


def test_proxy_args_downscale_for_cheap_classification():
    args = build_proxy_args(Path("a.mp4"), Path("a.proxy.mp4"))
    vf = args[args.index("-vf") + 1]
    assert "scale=-2:480" in vf and "fps=15" in vf
    assert args[-1] == str(Path("a.proxy.mp4"))
    assert args[0] == "-y"


# --------------------------------------------------------------------------- #
# ensure_downloaded
# --------------------------------------------------------------------------- #

@pytest.fixture
def session(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with get_session(engine) as s:
        yield s


def _add_clip(session, clip_id: str) -> Clip:
    clip = Clip(
        source="twitch", source_clip_id=clip_id, url=f"https://clips.twitch.tv/{clip_id}",
        broadcaster_name="x", title="t", view_count=10, duration_s=20.0,
        created_at_source=datetime(2026, 11, 19, tzinfo=timezone.utc),
    )
    session.add(clip)
    session.commit()
    return clip


class Recorder:
    def __init__(self, fail_on: set[str] = frozenset()):
        self.downloads: list[str] = []
        self.proxies: list[Path] = []
        self.fail_on = fail_on

    def download(self, url: str, dest: Path) -> None:
        self.downloads.append(url)
        if any(url.endswith(f) for f in self.fail_on):
            raise DownloadError(f"boom {url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"video")

    def proxy(self, src: Path, dst: Path) -> None:
        self.proxies.append(src)
        dst.write_bytes(b"proxy")


def test_downloads_and_records_local_path(session, tmp_path):
    clip = _add_clip(session, "a")
    rec = Recorder()

    done = ensure_downloaded(session, [clip], base=tmp_path, download_fn=rec.download, proxy_fn=rec.proxy)

    assert done == [clip]
    assert Path(clip.local_path) == tmp_path / "raw" / "twitch_a.mp4"
    assert proxy_path_for(Path(clip.local_path)).exists()


def test_existing_files_are_not_downloaded_again(session, tmp_path):
    clip = _add_clip(session, "a")
    rec = Recorder()
    ensure_downloaded(session, [clip], base=tmp_path, download_fn=rec.download, proxy_fn=rec.proxy)
    ensure_downloaded(session, [clip], base=tmp_path, download_fn=rec.download, proxy_fn=rec.proxy)
    assert len(rec.downloads) == 1
    assert len(rec.proxies) == 1


def test_missing_file_is_downloaded_again_even_with_local_path_set(session, tmp_path):
    clip = _add_clip(session, "a")
    rec = Recorder()
    ensure_downloaded(session, [clip], base=tmp_path, download_fn=rec.download, proxy_fn=rec.proxy)
    Path(clip.local_path).unlink()
    ensure_downloaded(session, [clip], base=tmp_path, download_fn=rec.download, proxy_fn=rec.proxy)
    assert len(rec.downloads) == 2


def test_one_failed_download_does_not_stop_the_rest(session, tmp_path, capsys):
    bad, good = _add_clip(session, "bad"), _add_clip(session, "good")
    rec = Recorder(fail_on={"bad"})

    done = ensure_downloaded(session, [bad, good], base=tmp_path, download_fn=rec.download, proxy_fn=rec.proxy)

    assert done == [good]
    assert bad.local_path is None
    assert "bad" in capsys.readouterr().out


def test_failed_proxy_counts_as_failed_download(session, tmp_path):
    clip = _add_clip(session, "a")
    rec = Recorder()

    def broken_proxy(src, dst):
        raise DownloadError("ffmpeg died")

    assert ensure_downloaded(session, [clip], base=tmp_path, download_fn=rec.download, proxy_fn=broken_proxy) == []
    assert clip.local_path is None


@pytest.mark.slow
def test_make_proxy_with_real_ffmpeg(tmp_path):
    from app.utils.ffmpeg_helpers import probe_resolution, run_ffmpeg

    src = tmp_path / "src.mp4"
    run_ffmpeg([
        "-y", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", str(src),
    ])
    dst = tmp_path / "src.proxy.mp4"
    download.make_proxy(src, dst)
    assert probe_resolution(dst) == (854, 480)
