"""Clip render, base pass: per-clip segments, the 9:16 Short base and the xfade concat."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.clips import fonts
from app.clips.render import (
    XFADE_S,
    BaseRender,
    Credit,
    RenderError,
    build_concat_args,
    build_segment_args,
    build_short_base_args,
    credit_for,
    render_base,
    xfade_offsets,
)
from app.clips.select import EditionPlan, PlannedItem
from app.config import ASSETS_DIR

BEBAS = ASSETS_DIR / "fonts" / "BebasNeue-Regular.ttf"


def _filter(args: list[str]) -> str:
    return args[args.index("-filter_complex") + 1]


def _credit(**kw) -> Credit:
    base = dict(platform="twitch", name="xQc", icon=None, font=BEBAS)
    base.update(kw)
    return Credit(**base)


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #

def test_bebas_covers_latin_names_but_not_cyrillic_or_cjk():
    assert fonts.covers(BEBAS, "João_Ñandú-ßő 42")
    assert not fonts.covers(BEBAS, "Жора")
    assert not fonts.covers(BEBAS, "加藤純一")


def test_credit_font_falls_back_for_uncovered_names(tmp_path):
    cjk = tmp_path / "msyh.ttc"
    hangul = tmp_path / "malgun.ttf"
    cjk.write_bytes(b"x")
    hangul.write_bytes(b"x")
    pick = lambda name: fonts.credit_font(name, primary=BEBAS, cjk=cjk, hangul=hangul)  # noqa: E731
    assert pick("Gaules") == BEBAS
    assert pick("加藤純一") == cjk
    assert pick("우왁굳") == hangul
    assert pick("Жора") == cjk


def test_credit_font_keeps_primary_when_fallback_missing(tmp_path):
    missing = tmp_path / "nope.ttc"
    assert fonts.credit_font("加藤", primary=BEBAS, cjk=missing, hangul=missing) == BEBAS


# --------------------------------------------------------------------------- #
# Credit
# --------------------------------------------------------------------------- #

@dataclass
class FakeClip:
    id: int
    source: str = "twitch"
    broadcaster_name: str = "xQc"
    local_path: str | None = None


def test_credit_uses_platform_icon_when_present(tmp_path):
    (tmp_path / "twitch.png").write_bytes(b"png")
    credit = credit_for(FakeClip(1), icons_dir=tmp_path)
    assert credit.icon == tmp_path / "twitch.png"
    assert credit.name == "xQc"
    assert credit_for(FakeClip(2, source="kick"), icons_dir=tmp_path).icon is None


# --------------------------------------------------------------------------- #
# Segment (16:9, one per clip)
# --------------------------------------------------------------------------- #

def test_segment_normalises_to_1080p30_with_exact_duration(tmp_path):
    args = build_segment_args(tmp_path / "raw.mp4", tmp_path / "seg.mp4",
                              duration=29.97, rank=3, credit=_credit())
    graph = _filter(args)
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in graph
    assert "pad=1920:1080" in graph
    assert "fps=30" in graph
    # Video and audio are both cut to the probed duration so the xfade
    # offsets downstream line up with the audio.
    assert "trim=duration=29.970" in graph
    assert "atrim=0:29.970" in graph
    assert "loudnorm" in graph
    assert args[-1] == str(tmp_path / "seg.mp4")


def test_segment_draws_rank_badge_and_credit(tmp_path):
    graph = _filter(build_segment_args(tmp_path / "raw.mp4", tmp_path / "seg.mp4",
                                       duration=10, rank=7, credit=_credit(name="Gaules")))
    assert "text='#7'" in graph
    assert "text='Gaules'" in graph
    assert "expansion=none" in graph


def test_segment_escapes_broadcaster_name(tmp_path):
    graph = _filter(build_segment_args(tmp_path / "raw.mp4", tmp_path / "seg.mp4",
                                       duration=10, rank=1, credit=_credit(name="it's:100%")))
    assert r"it'\''s\:100\%" in graph


def test_segment_overlays_icon_or_draws_platform_box(tmp_path):
    icon = tmp_path / "twitch.png"
    with_icon = build_segment_args(tmp_path / "raw.mp4", tmp_path / "seg.mp4",
                                   duration=10, rank=1, credit=_credit(icon=icon))
    assert str(icon) in with_icon
    assert "overlay=" in _filter(with_icon)

    without = _filter(build_segment_args(tmp_path / "raw.mp4", tmp_path / "seg.mp4",
                                         duration=10, rank=1, credit=_credit()))
    assert "drawbox=" in without and "0x9146FF" in without


def test_segment_uses_credit_font(tmp_path):
    font = tmp_path / "msyh.ttc"
    graph = _filter(build_segment_args(tmp_path / "raw.mp4", tmp_path / "seg.mp4",
                                       duration=10, rank=1, credit=_credit(font=font)))
    assert "msyh.ttc" in graph


# --------------------------------------------------------------------------- #
# Short base (9:16)
# --------------------------------------------------------------------------- #

def test_short_base_is_blurred_background_with_centred_clip(tmp_path):
    args = build_short_base_args(tmp_path / "raw.mp4", tmp_path / "s.mp4",
                                 duration=40, trim_start_s=None, credit=_credit())
    graph = _filter(args)
    assert "split" in graph
    assert "crop=1080:1920" in graph and "boxblur" in graph
    assert "scale=1080:-2" in graph
    assert "overlay=(W-w)/2:(H-h)/2" in graph
    assert "trim=duration=40.000" in graph
    assert "-ss" not in args
    assert "text='#" not in graph  # the Short's title comes in the language pass


def test_short_base_cuts_the_start(tmp_path):
    args = build_short_base_args(tmp_path / "raw.mp4", tmp_path / "s.mp4",
                                 duration=58, trim_start_s=6, credit=_credit())
    # Input seek: -ss before -i, and the kept length is what remains.
    assert args.index("-ss") < args.index("-i")
    assert args[args.index("-ss") + 1] == "6.000"
    assert "trim=duration=52.000" in _filter(args)


# --------------------------------------------------------------------------- #
# Concat with xfade
# --------------------------------------------------------------------------- #

def test_xfade_offsets_are_clip_starts_in_the_output():
    assert xfade_offsets([10.0, 20.0, 5.0], 0.3) == pytest.approx([0.0, 9.7, 29.4])
    assert xfade_offsets([12.0], 0.3) == [0.0]


def test_concat_chains_xfade_and_acrossfade(tmp_path):
    segs = [tmp_path / f"seg_{i}.mp4" for i in range(3)]
    args = build_concat_args(segs, [10.0, 20.0, 5.0], tmp_path / "base.mp4")
    graph = _filter(args)
    assert graph.count("xfade=") == 2
    assert graph.count("acrossfade=") == 2
    assert f"duration={XFADE_S}:offset=9.700" in graph
    assert "offset=29.400" in graph
    assert [a for a in args if a.endswith(".mp4")][:3] == [str(s) for s in segs]


def test_concat_single_segment_is_stream_copy(tmp_path):
    args = build_concat_args([tmp_path / "a.mp4"], [12.0], tmp_path / "base.mp4")
    assert "-filter_complex" not in args
    assert args[args.index("-c") + 1] == "copy"


def test_concat_rejects_segment_shorter_than_transition(tmp_path):
    with pytest.raises(RenderError):
        build_concat_args([tmp_path / "a.mp4", tmp_path / "b.mp4"], [10.0, 0.2], tmp_path / "o.mp4")


# --------------------------------------------------------------------------- #
# render_base (runner + cache), ffmpeg mocked
# --------------------------------------------------------------------------- #

def _plan(with_short: bool = True) -> EditionPlan:
    # Countdown: rank 2 plays first.
    return EditionPlan("top_daily", (
        PlannedItem(clip_id=20, rank=2, position=1, duration_s=10.0),
        PlannedItem(clip_id=10, rank=1, position=2, duration_s=58.0,
                    in_short=with_short, trim_start_s=6.0 if with_short else None),
    ))


def _clips(tmp_path) -> dict[int, FakeClip]:
    raws = {}
    for cid in (10, 20):
        p = tmp_path / f"raw_{cid}.mp4"
        p.write_bytes(b"raw")
        raws[cid] = FakeClip(cid, broadcaster_name=f"s{cid}", local_path=str(p))
    return raws


class FakeFFmpeg:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> None:
        self.calls.append(args)
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"video")


def _probe(durations: dict[str, float]):
    def probe(path: Path) -> float:
        for key, value in durations.items():
            if Path(path).name.startswith(key):
                return value
        raise AssertionError(f"unexpected probe of {path}")
    return probe


PROBES = {"raw_10": 58.0, "raw_20": 10.0, "seg_01": 10.0, "seg_02": 58.0}


def test_render_base_renders_segments_concat_and_short(tmp_path):
    run = FakeFFmpeg()
    out = render_base(_plan(), _clips(tmp_path), tmp_path / "ed", run=run,
                      probe=_probe(PROBES), icons_dir=tmp_path, measure=lambda p: 12.5)
    assert isinstance(out, BaseRender)
    names = [Path(c[-1]).name for c in run.calls]
    assert names == ["seg_01.mp4", "seg_02.mp4", "base_16x9.mp4", "base_9x16.mp4", "peak.png"]
    # The thumbnail frame comes from the #1 clip's raw file at its loudest moment.
    peak = run.calls[-1]
    assert peak[peak.index("-i") + 1].endswith("raw_10.mp4")
    assert peak[peak.index("-ss") + 1] == "12.500"
    assert out.peak_frame == tmp_path / "ed" / "peak.png"
    # seg_01 holds rank 2 (countdown).
    assert "text='#2'" in _filter(run.calls[0])
    assert out.offsets == pytest.approx((0.0, 10.0 - XFADE_S))
    assert out.duration_s == pytest.approx(68.0 - XFADE_S)
    assert out.base_9x16 == tmp_path / "ed" / "base_9x16.mp4"
    meta = json.loads((tmp_path / "ed" / "base.json").read_text(encoding="utf-8"))
    assert meta["offsets"] == pytest.approx([0.0, 9.7])


def test_render_base_is_cached(tmp_path):
    clips = _clips(tmp_path)
    render_base(_plan(), clips, tmp_path / "ed", run=FakeFFmpeg(),
                probe=_probe(PROBES), icons_dir=tmp_path, measure=lambda p: 1.0)
    again = FakeFFmpeg()
    out = render_base(_plan(), clips, tmp_path / "ed", run=again,
                      probe=_probe(PROBES), icons_dir=tmp_path, measure=lambda p: 1.0)
    assert again.calls == []
    assert out.offsets == pytest.approx((0.0, 9.7))


def test_render_base_rerenders_only_what_changed(tmp_path):
    clips = _clips(tmp_path)
    render_base(_plan(), clips, tmp_path / "ed", run=FakeFFmpeg(),
                probe=_probe(PROBES), icons_dir=tmp_path, measure=lambda p: 1.0)
    clips[20].broadcaster_name = "renamed"
    again = FakeFFmpeg()
    render_base(_plan(), clips, tmp_path / "ed", run=again,
                probe=_probe(PROBES), icons_dir=tmp_path, measure=lambda p: 1.0)
    assert [Path(c[-1]).name for c in again.calls] == ["seg_01.mp4", "base_16x9.mp4"]


def test_render_base_rebuilds_concat_when_its_metadata_is_missing(tmp_path):
    clips = _clips(tmp_path)
    render_base(_plan(), clips, tmp_path / "ed", run=FakeFFmpeg(),
                probe=_probe(PROBES), icons_dir=tmp_path, measure=lambda p: 1.0)
    (tmp_path / "ed" / "base.json").unlink()
    again = FakeFFmpeg()
    out = render_base(_plan(), clips, tmp_path / "ed", run=again,
                      probe=_probe(PROBES), icons_dir=tmp_path, measure=lambda p: 1.0)
    assert [Path(c[-1]).name for c in again.calls] == ["base_16x9.mp4"]
    assert out.offsets == pytest.approx((0.0, 9.7))
    assert (tmp_path / "ed" / "base.json").is_file()


def test_render_base_without_short(tmp_path):
    run = FakeFFmpeg()
    out = render_base(_plan(with_short=False), _clips(tmp_path), tmp_path / "ed", run=run,
                      probe=_probe(PROBES), icons_dir=tmp_path, measure=lambda p: 1.0)
    assert out.base_9x16 is None
    assert "base_9x16.mp4" not in [Path(c[-1]).name for c in run.calls]


def test_render_base_requires_downloaded_clips(tmp_path):
    clips = _clips(tmp_path)
    clips[10].local_path = None
    with pytest.raises(RenderError, match="10"):
        render_base(_plan(), clips, tmp_path / "ed", run=FakeFFmpeg(),
                    probe=_probe(PROBES), icons_dir=tmp_path, measure=lambda p: 1.0)


def _short_call(run: FakeFFmpeg) -> list[str]:
    return next(c for c in run.calls if c[-1].endswith("base_9x16.mp4"))


def test_short_base_keeps_the_planned_length_when_the_file_runs_long(tmp_path):
    # Twitch reports 60.0 s; the plan cuts 7.0 s to fit a 53 s budget. The
    # file probes 60.04 s: the Short keeps the planned 53.0 s, ending where
    # the clip ends.
    plan = EditionPlan("top_daily", (
        PlannedItem(clip_id=20, rank=2, position=1, duration_s=10.0),
        PlannedItem(clip_id=10, rank=1, position=2, duration_s=60.0, in_short=True, trim_start_s=7.0),
    ))
    run = FakeFFmpeg()
    render_base(plan, _clips(tmp_path), tmp_path / "ed", run=run,
                probe=_probe({**PROBES, "raw_10": 60.04}), icons_dir=tmp_path, measure=lambda p: 1.0)
    short = _short_call(run)
    assert short[short.index("-ss") + 1] == "7.040"
    assert "trim=duration=53.000" in _filter(short)
    assert short[short.index("-t") + 1] == "53.000"


def test_short_base_never_outlasts_the_file(tmp_path):
    plan = EditionPlan("top_daily", (
        PlannedItem(clip_id=20, rank=2, position=1, duration_s=10.0),
        PlannedItem(clip_id=10, rank=1, position=2, duration_s=58.0, in_short=True),
    ))
    run = FakeFFmpeg()
    render_base(plan, _clips(tmp_path), tmp_path / "ed", run=run,
                probe=_probe({**PROBES, "raw_10": 57.9}), icons_dir=tmp_path, measure=lambda p: 1.0)
    short = _short_call(run)
    assert "-ss" not in short
    assert "trim=duration=57.900" in _filter(short)


# --------------------------------------------------------------------------- #
# Real ffmpeg
# --------------------------------------------------------------------------- #

def _synthetic_clip(dst: Path, seconds: float, freq: int) -> None:
    from app.utils.ffmpeg_helpers import run_ffmpeg

    run_ffmpeg([
        "-y", "-f", "lavfi", "-i", f"testsrc=size=1280x720:rate=60:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=44100:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dst),
    ])


@pytest.mark.slow
def test_render_base_real_ffmpeg(tmp_path):
    if shutil.which("ffmpeg") is None and shutil.which("ffmpeg.exe") is None:
        from app.utils.ffmpeg_helpers import ffmpeg_bin
        ffmpeg_bin()  # raises with a clear message when missing

    from app.utils.ffmpeg_helpers import probe_duration, probe_resolution

    _synthetic_clip(tmp_path / "raw_10.mp4", 6.0, 440)
    _synthetic_clip(tmp_path / "raw_20.mp4", 4.0, 660)
    clips = {
        10: FakeClip(10, broadcaster_name="Gaules", local_path=str(tmp_path / "raw_10.mp4")),
        20: FakeClip(20, broadcaster_name="加藤純一", local_path=str(tmp_path / "raw_20.mp4")),
    }
    plan = EditionPlan("top_daily", (
        PlannedItem(clip_id=20, rank=2, position=1, duration_s=4.0),
        PlannedItem(clip_id=10, rank=1, position=2, duration_s=6.0, in_short=True, trim_start_s=1.5),
    ))
    out = render_base(plan, clips, tmp_path / "ed", icons_dir=tmp_path)

    assert probe_resolution(out.base_16x9) == (1920, 1080)
    assert probe_duration(out.base_16x9) == pytest.approx(10.0 - XFADE_S, abs=0.1)
    assert out.offsets[1] == pytest.approx(4.0 - XFADE_S, abs=0.05)
    assert probe_resolution(out.base_9x16) == (1080, 1920)
    assert probe_duration(out.base_9x16) == pytest.approx(4.5, abs=0.1)

    # Language pass, with synthetic outros so no TTS is called.
    from app.clips.render import TITLE_CARD_S, render_language
    from app.utils.ffmpeg_helpers import run_ffmpeg

    outros = {}
    for aspect, size in (("16x9", "1920x1080"), ("9x16", "1080x1920")):
        outros[aspect] = tmp_path / f"outro_{aspect}.mp4"
        run_ffmpeg([
            "-y", "-f", "lavfi", "-i", f"color=c=navy:s={size}:r=30:d=2",
            "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2",
            "-shortest", str(outros[aspect]),
        ])
    lang = render_language(out, title="GTA 6 Momentos Engraçados da Semana #12",
                           outro_16x9=outros["16x9"], outro_9x16=outros["9x16"],
                           lang_dir=tmp_path / "ed" / "pt-BR")
    assert probe_duration(lang.medium) == pytest.approx(TITLE_CARD_S + 9.7 + 2.0, abs=0.15)
    assert probe_resolution(lang.medium) == (1920, 1080)
    assert probe_duration(lang.short) == pytest.approx(4.5 + 2.0, abs=0.15)
    assert probe_resolution(lang.short) == (1080, 1920)
    assert probe_resolution(lang.thumbnail) == (1280, 720)
