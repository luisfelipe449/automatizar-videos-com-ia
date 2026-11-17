"""Branded outro with a spoken and written call to action, per channel and aspect.

Source: `assets/input/outro_16x9.mp4` (the brand animation) plus an optional
sidecar `outro_16x9.json` with a `delogo` box covering a generator watermark.
The box belongs to that exact file, so replacing the video means updating or
deleting the sidecar. Without a brand video, a plain placeholder is rendered.

Output per (profile, aspect), cached by a hash of every input:
- 16x9: 1920x1080, last frame frozen to at least 10 s. YouTube end-screen
  elements need 5-20 s at the end of a video over 25 s.
- 9x16: 1080x1920, the outro centred over a blurred copy of itself.
The source's own audio is dropped. The CTA is narrated (Gemini TTS, cached
by text and voice) 0.8 s in, over the outro music. The CTA text is drawn from a file
because drawtext cannot wrap and file contents need no escaping.
"""
from __future__ import annotations

import hashlib
import json
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from app.config import ASSETS_DIR, CLIPS_DIR, INPUT_DIR
from app.clips.profiles import ChannelProfile
from app.utils import cache
from app.utils.ffmpeg_helpers import probe_duration, run_ffmpeg

Aspect = Literal["16x9", "9x16"]

_FPS = 30
_SAMPLE_RATE = 48000
_FONT = ASSETS_DIR / "fonts" / "BebasNeue-Regular.ttf"
_MUSIC_DIR = INPUT_DIR / "music"
_PREFERRED_MUSIC = "upbeat_future.mp3"
_MUSIC_VOLUME = 0.25
_CTA_DELAY_S = 0.8
_CTA_TAIL_S = 0.6
_MIN_DURATION = {"16x9": 10.0, "9x16": 5.0}
# Text layout: (wrap width in chars, font size, y of the first line).
# 16x9: above the stars, which start near y=250. 9x16: inside the blurred
# band above the centred video, which starts at y=656.
_TEXT = {"16x9": (44, 64, 60), "9x16": (22, 84, 200)}
# Bump when the filter graph changes so cached outros re-render.
_GRAPH_VERSION = "3"


@dataclass(frozen=True)
class DelogoBox:
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class OutroSource:
    path: Path
    delogo: DelogoBox | None


def _escape_path(p: Path) -> str:
    # Filter-graph form: forward slashes, drive colon escaped ("C\:/...").
    return Path(p).resolve().as_posix().replace(":", r"\:")


def build_placeholder_args(dst: Path) -> list[str]:
    font = _escape_path(_FONT)
    return [
        "-y", "-f", "lavfi", "-i", f"color=c=0x0b1030:s=1280x720:r={_FPS}:d=5",
        "-vf",
        f"drawtext=fontfile='{font}':text='FIVE STAR CLIPS':fontsize=120:"
        f"fontcolor=0xff4fd8:borderw=4:bordercolor=0x00e5ff:"
        f"x=(w-text_w)/2:y=(h-text_h)/2:alpha='min(1,t/1.2)'",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dst),
    ]


def make_placeholder(dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(build_placeholder_args(dst))


def resolve_source(
    *,
    input_dir: Path = INPUT_DIR,
    work_dir: Path = CLIPS_DIR / "outro",
    make_placeholder: Callable[[Path], None] | None = make_placeholder,
) -> OutroSource:
    brand = input_dir / "outro_16x9.mp4"
    if brand.is_file():
        sidecar = brand.with_suffix(".json")
        box = None
        if sidecar.is_file():
            raw = json.loads(sidecar.read_text(encoding="utf-8")).get("delogo")
            if raw:
                box = DelogoBox(int(raw["x"]), int(raw["y"]), int(raw["w"]), int(raw["h"]))
        return OutroSource(brand, box)
    placeholder = work_dir / "outro_placeholder_16x9.mp4"
    if not placeholder.is_file():
        if make_placeholder is None:
            raise FileNotFoundError(f"no brand outro at {brand} and placeholder disabled")
        make_placeholder(placeholder)
    return OutroSource(placeholder, None)


def wrap_cta(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width=width, break_long_words=False)


def outro_duration(aspect: Aspect, *, tts_s: float) -> float:
    return max(_MIN_DURATION[aspect], _CTA_DELAY_S + tts_s + _CTA_TAIL_S)


def pick_music(music_dir: Path = _MUSIC_DIR) -> Path | None:
    preferred = music_dir / _PREFERRED_MUSIC
    if preferred.is_file():
        return preferred
    tracks = sorted(music_dir.glob("*.mp3")) if music_dir.is_dir() else []
    return tracks[0] if tracks else None


def build_outro_args(
    source: OutroSource,
    aspect: Aspect,
    *,
    cta_file: Path,
    tts_wav: Path,
    music: Path | None,
    duration: float,
    dst: Path,
) -> list[str]:
    _, font_size, text_y = _TEXT[aspect]
    font = _escape_path(_FONT)
    delogo = ""
    if source.delogo:
        b = source.delogo
        delogo = f"delogo=x={b.x}:y={b.y}:w={b.w}:h={b.h},"
    base = (
        f"[0:v]{delogo}fps={_FPS},tpad=stop_mode=clone:stop_duration={duration:.3f},"
        f"trim=duration={duration:.3f},setpts=PTS-STARTPTS"
    )
    text = (
        f"drawtext=fontfile='{font}':textfile='{_escape_path(cta_file)}':"
        f"fontsize={font_size}:fontcolor=white:borderw=5:bordercolor=black:"
        f"line_spacing=8:text_align=C:x=(w-text_w)/2:y={text_y}:"
        f"alpha='min(1,max(0,(t-{_CTA_DELAY_S - 0.3:.1f})/0.4))'"
    )
    if aspect == "16x9":
        video = f"{base},scale=1920:1080:flags=lanczos,setsar=1,{text}[v]"
    else:
        video = (
            f"{base},split[bgsrc][fgsrc];"
            f"[bgsrc]scale=-2:1920,crop=1080:1920,boxblur=20:2[bg];"
            f"[fgsrc]scale=1080:-2:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,{text}[v]"
        )

    # Every audio branch is padded AND trimmed to exactly `duration`, with
    # timestamps reset. An unbounded `apad` let ffmpeg run away with
    # non-monotonic DTS on ~1 in 6 runs, ignoring `-t` and never exiting.
    fmt = f"aformat=sample_rates={_SAMPLE_RATE}:channel_layouts=stereo"
    fit = f"apad=whole_dur={duration:.3f},atrim=0:{duration:.3f},asetpts=PTS-STARTPTS"
    delay_ms = int(_CTA_DELAY_S * 1000)
    tts = f"[1:a]{fmt},adelay={delay_ms}:all=1,{fit}"
    if music is not None:
        fade_start = max(0.0, duration - 1.0)
        audio = (
            f"{tts}[tts];"
            f"[2:a]{fmt},volume={_MUSIC_VOLUME},atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=out:st={fade_start:.3f}:d=1,{fit}[mus];"
            f"[tts][mus]amix=inputs=2:duration=first:normalize=0[a]"
        )
    else:
        audio = f"{tts}[a]"

    args = ["-y", "-i", str(source.path), "-i", str(tts_wav)]
    if music is not None:
        args += ["-i", str(music)]
    args += [
        "-filter_complex", f"{video};{audio}",
        "-map", "[v]", "-map", "[a]",
        "-t", f"{duration:.3f}",
        "-r", str(_FPS),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", str(_SAMPLE_RATE),
        "-movflags", "+faststart",
        str(dst),
    ]
    return args


def _synthesize_cta(text: str, key: str, voice: str) -> Path:
    from app.services.tts_service import synthesize_narration

    # synthesize_narration caches by text and voice under
    # assets/generated/<job_id>, so each channel pays for its CTA once per
    # wording and voice.
    return synthesize_narration(text, f"clips_cta_{key}", voice_name=voice)


def _file_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"


def render_outro(
    profile: ChannelProfile,
    aspect: Aspect,
    *,
    source: OutroSource | None = None,
    work_dir: Path = CLIPS_DIR / "outro",
    tts_fn: Callable[[str, str, str], Path] = _synthesize_cta,
    run: Callable[[list[str]], None] = run_ffmpeg,
    probe: Callable[[Path], float] = probe_duration,
    music: Path | None | Literal["auto"] = "auto",
) -> Path:
    """Render (or reuse) the outro for this channel and aspect ratio."""
    source = source or resolve_source(work_dir=work_dir)
    music_path = pick_music() if music == "auto" else music
    tts_wav = tts_fn(profile.outro_cta, profile.key, profile.tts_voice)

    width, _, _ = _TEXT[aspect]
    work_dir.mkdir(parents=True, exist_ok=True)
    cta_file = work_dir / f"cta_{profile.key}_{aspect}.txt"
    cta_text = "\n".join(wrap_cta(profile.outro_cta, width))

    dst = work_dir / f"outro_{profile.key}_{aspect}.mp4"
    hash_path = dst.with_suffix(".hash")
    fingerprint = json.dumps({
        "v": _GRAPH_VERSION,
        "source": _file_fingerprint(source.path),
        "delogo": source.delogo.__dict__ if source.delogo else None,
        "cta": cta_text,
        "tts": hashlib.sha256(tts_wav.read_bytes()).hexdigest(),
        "music": _file_fingerprint(music_path) if music_path else None,
        "aspect": aspect,
    }, sort_keys=True)
    expected = cache.hash_text(fingerprint)
    if cache.artifact_fresh(dst, hash_path, expected):
        return dst

    # newline="\n": on Windows the default writes \r\n, and drawtext renders
    # the \r as a second line break.
    cta_file.write_text(cta_text, encoding="utf-8", newline="\n")
    duration = outro_duration(aspect, tts_s=probe(tts_wav))
    run(build_outro_args(
        source, aspect,
        cta_file=cta_file, tts_wav=tts_wav, music=music_path,
        duration=duration, dst=dst,
    ))
    cache.write_hash_file(hash_path, expected)
    return dst
