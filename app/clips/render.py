"""Edition render, base pass: everything that is the same in every language.

Per edition directory (`assets/clips/editions/<edition_key>/`):
- `seg_NN.mp4`, one per clip in playback position NN: 1920x1080 @ 30 fps,
  48 kHz stereo, loudness-normalised, with the "#rank" badge top-left and the
  streamer credit (platform icon + channel name) bottom-left.
- `base_16x9.mp4`: the segments joined with a short crossfade.
- `base_9x16.mp4`: the Short's clip over a blurred copy of itself, its start
  cut by the plan's `trim_start_s`. No title; the language pass adds it.
- `base.json`: where each clip starts in `base_16x9.mp4` (chapters).

Each output is cached by a hash of its inputs, so renaming one streamer
re-renders one segment and the concat, nothing else.

Every segment's video and audio are cut to the same probed length. The
xfade offsets are computed from segment durations; a segment whose audio
ran longer than its video would push every later clip's sound out of sync.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from app.clips import fonts
from app.clips.outro import _escape_path, _file_fingerprint
from app.clips.select import EditionPlan
from app.config import ASSETS_DIR
from app.services.render_service import _escape_drawtext
from app.utils import cache
from app.utils.ffmpeg_helpers import probe_duration, run_ffmpeg

XFADE_S = 0.3
_FPS = 30
_SAMPLE_RATE = 48000
# YouTube plays back at -14 LUFS; normalising each clip there keeps a quiet
# streamer and a screaming one at the same level.
_LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
_ICONS_DIR = ASSETS_DIR / "icons"
# Without an icon PNG in assets/icons/<platform>.png, a square in the
# platform's colour stands in for it.
_PLATFORM_COLOR = {"twitch": "0x9146FF", "kick": "0x53FC18", "youtube": "0xFF0000"}
_BADGE_COLOR = "0xFFD700"


@dataclass(frozen=True)
class _CreditLayout:
    x: int
    y: int
    icon: int  # square side, px
    font_size: int


# 16:9: bottom-left of the frame. 9:16: inside the blurred band under the
# centred clip, which ends at y=1264 for a 16:9 source.
_CREDIT_16X9 = _CreditLayout(x=48, y=960, icon=64, font_size=56)
_CREDIT_9X16 = _CreditLayout(x=60, y=1344, icon=72, font_size=64)

# Intermediate files: near-lossless, re-encoded once more by the language pass.
_ENCODE = [
    "-r", str(_FPS),
    "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "192k", "-ar", str(_SAMPLE_RATE),
    "-movflags", "+faststart",
]
# Bump when a filter graph changes so cached outputs re-render.
_GRAPH_VERSION = "1"


class RenderError(RuntimeError):
    """The edition cannot be rendered as planned."""


class RenderableClip(Protocol):
    id: int
    source: str
    broadcaster_name: str
    local_path: str | None


@dataclass(frozen=True)
class Credit:
    platform: str
    name: str
    icon: Path | None
    font: Path


@dataclass(frozen=True)
class BaseRender:
    base_16x9: Path
    base_9x16: Path | None
    offsets: tuple[float, ...]  # start of each clip in base_16x9, playback order
    duration_s: float


def credit_for(
    clip: RenderableClip,
    *,
    icons_dir: Path = _ICONS_DIR,
    font_for: Callable[[str], Path] = fonts.credit_font,
) -> Credit:
    icon = icons_dir / f"{clip.source}.png"
    return Credit(
        platform=clip.source,
        name=clip.broadcaster_name,
        icon=icon if icon.is_file() else None,
        font=font_for(clip.broadcaster_name),
    )


# --------------------------------------------------------------------------- #
# Filter-graph builders (pure)
# --------------------------------------------------------------------------- #

def _credit_graph(src_label: str, credit: Credit, lay: _CreditLayout, icon_input: int) -> tuple[str, list[str]]:
    """Graph from `[src_label]` to `[v]` drawing the credit; plus extra inputs."""
    text = (
        f"drawtext=fontfile='{_escape_path(credit.font)}':expansion=none:"
        f"text='{_escape_drawtext(credit.name)}':fontsize={lay.font_size}:fontcolor=white:"
        f"borderw=3:bordercolor=black:box=1:boxcolor=black@0.5:boxborderw=12:"
        f"x={lay.x + lay.icon + 20}:y={lay.y}+({lay.icon}-text_h)/2"
    )
    if credit.icon is not None:
        graph = (
            f"[{icon_input}:v]scale={lay.icon}:{lay.icon}[icon];"
            f"[{src_label}][icon]overlay={lay.x}:{lay.y},{text}[v]"
        )
        return graph, ["-i", str(credit.icon)]
    color = _PLATFORM_COLOR.get(credit.platform, "0x808080")
    graph = (
        f"[{src_label}]drawbox=x={lay.x}:y={lay.y}:w={lay.icon}:h={lay.icon}:"
        f"color={color}@1:t=fill,{text}[v]"
    )
    return graph, []


def _audio_graph(duration: float) -> str:
    # loudnorm resamples to 192 kHz internally; resample back before padding.
    return (
        f"[0:a]{_LOUDNORM},aresample={_SAMPLE_RATE},"
        f"aformat=sample_rates={_SAMPLE_RATE}:channel_layouts=stereo,"
        f"apad=whole_dur={duration:.3f},atrim=0:{duration:.3f},asetpts=PTS-STARTPTS[a]"
    )


def _fit_video(duration: float) -> str:
    # Clone the last frame, then cut: the video lasts exactly `duration` even
    # when its stream is a few frames shorter than the container.
    return (
        f"tpad=stop_mode=clone:stop_duration=2,"
        f"trim=duration={duration:.3f},setpts=PTS-STARTPTS"
    )


def build_segment_args(src: Path, dst: Path, *, duration: float, rank: int, credit: Credit) -> list[str]:
    """One clip as a 16:9 segment of exactly `duration` seconds."""
    font = _escape_path(fonts.BRAND_FONT)
    badge = (
        f"drawtext=fontfile='{font}':expansion=none:text='#{rank}':fontsize=128:"
        f"fontcolor={_BADGE_COLOR}:borderw=6:bordercolor=black:x=48:y=36"
    )
    video = (
        f"[0:v]fps={_FPS},"
        f"scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
        f"{_fit_video(duration)},format=yuv420p,{badge}[base]"
    )
    credit_graph, extra = _credit_graph("base", credit, _CREDIT_16X9, icon_input=1)
    return [
        "-y", "-i", str(src), *extra,
        "-filter_complex", f"{video};{credit_graph};{_audio_graph(duration)}",
        "-map", "[v]", "-map", "[a]", "-t", f"{duration:.3f}",
        *_ENCODE, str(dst),
    ]


def build_short_base_args(
    src: Path, dst: Path, *, duration: float, trim_start_s: float | None, credit: Credit,
) -> list[str]:
    """The Short's clip as 1080x1920, its first `trim_start_s` seconds dropped.

    `duration` is the full clip length; the output lasts what remains.
    """
    keep = duration - (trim_start_s or 0.0)
    if keep <= 0:
        raise RenderError(f"trim of {trim_start_s} s leaves nothing of a {duration} s clip")
    video = (
        f"[0:v]fps={_FPS},{_fit_video(keep)},split[bgsrc][fgsrc];"
        f"[bgsrc]scale=-2:1920,crop=1080:1920,boxblur=20:2[bg];"
        f"[fgsrc]scale=1080:-2:flags=lanczos[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[base]"
    )
    credit_graph, extra = _credit_graph("base", credit, _CREDIT_9X16, icon_input=1)
    # -ss before -i seeks the input; with re-encoding the cut is frame-accurate.
    seek = ["-ss", f"{trim_start_s:.3f}"] if trim_start_s else []
    return [
        "-y", *seek, "-i", str(src), *extra,
        "-filter_complex", f"{video};{credit_graph};{_audio_graph(keep)}",
        "-map", "[v]", "-map", "[a]", "-t", f"{keep:.3f}",
        *_ENCODE, str(dst),
    ]


def xfade_offsets(durations: Sequence[float], xfade_s: float = XFADE_S) -> list[float]:
    """Start of each segment in the crossfaded output.

    Each transition overlaps two segments by `xfade_s`, so segment k starts at
    the sum of the earlier durations minus k overlaps. This is also the
    `offset` xfade needs for the transition into segment k.
    """
    offsets, elapsed = [], 0.0
    for k, dur in enumerate(durations):
        offsets.append(elapsed - k * xfade_s)
        elapsed += dur
    return offsets


def build_concat_args(
    segments: Sequence[Path], durations: Sequence[float], dst: Path, xfade_s: float = XFADE_S,
) -> list[str]:
    if len(segments) != len(durations) or not segments:
        raise RenderError("concat needs one duration per segment and at least one segment")
    if len(segments) == 1:
        return ["-y", "-i", str(segments[0]), "-c", "copy", "-movflags", "+faststart", str(dst)]
    if min(durations) <= xfade_s:
        raise RenderError(f"segment shorter than the {xfade_s} s transition: {list(durations)}")

    offsets = xfade_offsets(durations, xfade_s)
    parts, prev_v, prev_a = [], "0:v", "0:a"
    for k in range(1, len(segments)):
        parts.append(
            f"[{prev_v}][{k}:v]xfade=transition=fade:duration={xfade_s}:offset={offsets[k]:.3f}[v{k}]"
        )
        parts.append(f"[{prev_a}][{k}:a]acrossfade=d={xfade_s}[a{k}]")
        prev_v, prev_a = f"v{k}", f"a{k}"
    inputs = [arg for seg in segments for arg in ("-i", str(seg))]
    return [
        "-y", *inputs,
        "-filter_complex", ";".join(parts),
        "-map", f"[{prev_v}]", "-map", f"[{prev_a}]",
        *_ENCODE, str(dst),
    ]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def _credit_key(credit: Credit) -> dict:
    return {
        "platform": credit.platform,
        "name": credit.name,
        "icon": _file_fingerprint(credit.icon) if credit.icon else None,
        "font": str(credit.font),
    }


def _render_cached(dst: Path, key: dict, build: Callable[[], list[str]], run: Callable[[list[str]], None]) -> str:
    """Run `build()` unless `dst` is fresh for `key`. Returns the key's hash."""
    expected = cache.hash_text(json.dumps({"v": _GRAPH_VERSION, **key}, sort_keys=True))
    hash_path = dst.with_suffix(".hash")
    if not cache.artifact_fresh(dst, hash_path, expected):
        run(build())
        cache.write_hash_file(hash_path, expected)
    return expected


def render_base(
    plan: EditionPlan,
    clips: Mapping[int, RenderableClip],
    edition_dir: Path,
    *,
    run: Callable[[list[str]], None] = run_ffmpeg,
    probe: Callable[[Path], float] = probe_duration,
    icons_dir: Path = _ICONS_DIR,
    font_for: Callable[[str], Path] = fonts.credit_font,
) -> BaseRender:
    """Render (or reuse) the language-neutral base videos of one edition."""
    items = sorted(plan.items, key=lambda item: item.position)
    sources: dict[int, Path] = {}
    for item in items:
        clip = clips.get(item.clip_id)
        path = Path(clip.local_path) if clip is not None and clip.local_path else None
        if path is None or not path.is_file():
            raise RenderError(f"clip {item.clip_id} is not downloaded")
        sources[item.clip_id] = path
    edition_dir.mkdir(parents=True, exist_ok=True)

    segments: list[Path] = []
    seg_hashes: list[str] = []
    for item in items:
        src = sources[item.clip_id]
        credit = credit_for(clips[item.clip_id], icons_dir=icons_dir, font_for=font_for)
        dst = edition_dir / f"seg_{item.position:02d}.mp4"
        key = {"kind": "segment", "src": _file_fingerprint(src), "rank": item.rank,
               "credit": _credit_key(credit)}
        seg_hashes.append(_render_cached(
            dst, key,
            lambda src=src, dst=dst, item=item, credit=credit: build_segment_args(
                src, dst, duration=probe(src), rank=item.rank, credit=credit),
            run,
        ))
        segments.append(dst)

    base = edition_dir / "base_16x9.mp4"
    meta_path = edition_dir / "base.json"
    concat_key = {"kind": "concat", "segments": seg_hashes, "xfade": XFADE_S}
    expected = cache.hash_text(json.dumps({"v": _GRAPH_VERSION, **concat_key}, sort_keys=True))
    if cache.artifact_fresh(base, base.with_suffix(".hash"), expected) and meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        durations = [probe(seg) for seg in segments]
        _render_cached(base, concat_key, lambda: build_concat_args(segments, durations, base), run)
        offsets = xfade_offsets(durations)
        meta = {"offsets": offsets, "durations": durations,
                "duration_s": offsets[-1] + durations[-1]}
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    short_path = None
    short = plan.short
    if short is not None:
        src = sources[short.clip_id]
        credit = credit_for(clips[short.clip_id], icons_dir=icons_dir, font_for=font_for)
        short_path = edition_dir / "base_9x16.mp4"
        key = {"kind": "short", "src": _file_fingerprint(src), "trim": short.trim_start_s,
               "credit": _credit_key(credit)}
        _render_cached(
            short_path, key,
            lambda: build_short_base_args(src, short_path, duration=probe(src),
                                          trim_start_s=short.trim_start_s, credit=credit),
            run,
        )

    return BaseRender(
        base_16x9=base,
        base_9x16=short_path,
        offsets=tuple(meta["offsets"]),
        duration_s=meta["duration_s"],
    )
