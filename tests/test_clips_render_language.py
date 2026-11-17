"""Clip render, language pass: medium, Short and thumbnail per channel."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.clips.formats import SHORT_MAX_S
from app.clips.render import (
    TITLE_CARD_S,
    BaseRender,
    LanguageRender,
    RenderError,
    build_medium_args,
    build_short_args,
    build_thumbnail_args,
    loudest_moment,
    parse_rms_windows,
    render_language,
)


def _filter(args: list[str]) -> str:
    return args[args.index("-filter_complex") + 1]


# --------------------------------------------------------------------------- #
# Loudest moment (thumbnail frame)
# --------------------------------------------------------------------------- #

ASTATS = """\
frame:0    pts:0       pts_time:0
lavfi.astats.Overall.RMS_level=-inf
frame:1    pts:24000   pts_time:0.5
lavfi.astats.Overall.RMS_level=-30.5
frame:2    pts:48000   pts_time:1
lavfi.astats.Overall.RMS_level=-12.25
frame:3    pts:72000   pts_time:1.5
lavfi.astats.Overall.RMS_level=-20.0
"""


def test_parse_rms_windows():
    assert parse_rms_windows(ASTATS) == [(0.0, float("-inf")), (0.5, -30.5), (1.0, -12.25), (1.5, -20.0)]


def test_loudest_moment_is_middle_of_loudest_window():
    assert loudest_moment(Path("x.mp4"), capture=lambda args: ASTATS) == pytest.approx(1.25)


def test_loudest_moment_none_without_audio():
    assert loudest_moment(Path("x.mp4"), capture=lambda args: "") is None
    silent = "frame:0 pts:0 pts_time:0\nlavfi.astats.Overall.RMS_level=-inf\n"
    assert loudest_moment(Path("x.mp4"), capture=lambda args: silent) is None


# --------------------------------------------------------------------------- #
# Command builders
# --------------------------------------------------------------------------- #

def test_medium_is_title_card_then_base_then_outro(tmp_path):
    args = build_medium_args(tmp_path / "base.mp4", tmp_path / "outro.mp4",
                             title_file=tmp_path / "title.txt", dst=tmp_path / "m.mp4")
    inputs = [args[i + 1] for i, a in enumerate(args) if a == "-i"]
    assert inputs[0].startswith("color=") and f"d={TITLE_CARD_S}" in inputs[0]
    assert inputs[1].startswith("anullsrc=")
    assert inputs[2:] == [str(tmp_path / "base.mp4"), str(tmp_path / "outro.mp4")]
    graph = _filter(args)
    assert "title.txt" in graph and "expansion=none" in graph
    assert "concat=n=3:v=1:a=1" in graph
    assert args[-1] == str(tmp_path / "m.mp4")


def test_short_draws_title_over_base_then_outro(tmp_path):
    args = build_short_args(tmp_path / "b.mp4", tmp_path / "o.mp4",
                            title_file=tmp_path / "t.txt", dst=tmp_path / "s.mp4")
    graph = _filter(args)
    assert "t.txt" in graph
    assert "concat=n=2:v=1:a=1" in graph


def test_thumbnail_is_1280x720_jpeg_with_title(tmp_path):
    args = build_thumbnail_args(tmp_path / "peak.png", title_file=tmp_path / "t.txt",
                                dst=tmp_path / "thumb.jpg")
    vf = args[args.index("-vf") + 1]
    assert "1280" in vf and "720" in vf
    assert "t.txt" in vf
    assert args[args.index("-frames:v") + 1] == "1"
    assert args[-1].endswith("thumb.jpg")


# --------------------------------------------------------------------------- #
# render_language
# --------------------------------------------------------------------------- #

class FakeFFmpeg:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> None:
        self.calls.append(args)
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"out")


def _base(tmp_path, *, with_short: bool = True) -> BaseRender:
    ed = tmp_path / "ed"
    ed.mkdir(exist_ok=True)
    for name in ("base_16x9.mp4", "base_9x16.mp4", "peak.png"):
        (ed / name).write_bytes(b"x")
        (ed / name).with_suffix(".hash").write_text(f"hash-{name}", encoding="utf-8")
    return BaseRender(
        base_16x9=ed / "base_16x9.mp4",
        base_9x16=ed / "base_9x16.mp4" if with_short else None,
        offsets=(0.0, 9.7),
        duration_s=67.7,
        peak_frame=ed / "peak.png",
    )


def _outros(tmp_path) -> tuple[Path, Path]:
    # Written once: the cache keys outros by size and mtime.
    o16, o9 = tmp_path / "outro_16x9.mp4", tmp_path / "outro_9x16.mp4"
    for path in (o16, o9):
        if not path.exists():
            path.write_bytes(path.stem.encode())
    return o16, o9


def _probe(short_s: float = 45.0, outro_s: float = 6.5):
    def probe(path: Path) -> float:
        return {"base_9x16.mp4": short_s, "outro_9x16.mp4": outro_s}[Path(path).name]
    return probe


def _render(tmp_path, run, *, title="GTA 6 Top Clips of the Day #3", base=None, probe=None):
    o16, o9 = _outros(tmp_path)
    return render_language(
        base or _base(tmp_path), title=title, outro_16x9=o16, outro_9x16=o9,
        lang_dir=tmp_path / "ed" / "en", run=run, probe=probe or _probe(),
    )


def test_render_language_outputs(tmp_path):
    run = FakeFFmpeg()
    out = _render(tmp_path, run)
    assert isinstance(out, LanguageRender)
    assert [Path(c[-1]).name for c in run.calls] == ["medio.mp4", "short.mp4", "thumb.jpg"]
    lang = tmp_path / "ed" / "en"
    assert out == LanguageRender(lang / "medio.mp4", lang / "short.mp4", lang / "thumb.jpg")
    # Title files are LF-only: drawtext renders a \r as a line break.
    title = (lang / "title_16x9.txt").read_bytes()
    assert b"\r" not in title and b"GTA 6" in title


def test_render_language_is_cached(tmp_path):
    _render(tmp_path, FakeFFmpeg())
    again = FakeFFmpeg()
    _render(tmp_path, again)
    assert again.calls == []


def test_render_language_rerenders_on_title_change(tmp_path):
    _render(tmp_path, FakeFFmpeg())
    again = FakeFFmpeg()
    _render(tmp_path, again, title="GTA 6 Top Clips of the Day #4")
    assert len(again.calls) == 3


def test_render_language_rerenders_on_base_change(tmp_path):
    base = _base(tmp_path)
    _render(tmp_path, FakeFFmpeg(), base=base)
    (tmp_path / "ed" / "base_16x9.hash").write_text("new", encoding="utf-8")
    again = FakeFFmpeg()
    _render(tmp_path, again, base=base)
    assert [Path(c[-1]).name for c in again.calls] == ["medio.mp4"]


def test_render_language_rejects_short_over_the_cap(tmp_path):
    run = FakeFFmpeg()
    with pytest.raises(RenderError, match="59"):
        _render(tmp_path, run, probe=_probe(short_s=SHORT_MAX_S - 5.0, outro_s=6.0))
    assert "short.mp4" not in [Path(c[-1]).name for c in run.calls]


def test_render_language_short_at_the_cap_is_fine(tmp_path):
    _render(tmp_path, FakeFFmpeg(), probe=_probe(short_s=SHORT_MAX_S - 6.0, outro_s=6.0))


def test_render_language_without_short(tmp_path):
    run = FakeFFmpeg()
    out = _render(tmp_path, run, base=_base(tmp_path, with_short=False))
    assert out.short is None
    assert [Path(c[-1]).name for c in run.calls] == ["medio.mp4", "thumb.jpg"]
