"""Branded outro: source resolution, per-language CTA, both aspect ratios."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.clips import outro
from app.clips.outro import (
    DelogoBox,
    OutroSource,
    build_outro_args,
    outro_duration,
    resolve_source,
    render_outro,
    wrap_cta,
)
from app.clips.profiles import load_profile


# --------------------------------------------------------------------------- #
# Source resolution
# --------------------------------------------------------------------------- #

def test_brand_outro_is_used_with_its_sidecar_box(tmp_path):
    (tmp_path / "outro_16x9.mp4").write_bytes(b"v")
    (tmp_path / "outro_16x9.json").write_text(
        json.dumps({"delogo": {"x": 1114, "y": 666, "w": 146, "h": 42}}), encoding="utf-8"
    )
    src = resolve_source(input_dir=tmp_path, work_dir=tmp_path / "work", make_placeholder=None)
    assert src == OutroSource(path=tmp_path / "outro_16x9.mp4", delogo=DelogoBox(1114, 666, 146, 42))


def test_brand_outro_without_sidecar_has_no_delogo(tmp_path):
    (tmp_path / "outro_16x9.mp4").write_bytes(b"v")
    src = resolve_source(input_dir=tmp_path, work_dir=tmp_path / "work", make_placeholder=None)
    assert src.delogo is None


def test_placeholder_is_generated_once_when_no_brand_outro(tmp_path):
    made: list[Path] = []

    def make(dst: Path) -> None:
        made.append(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"placeholder")

    first = resolve_source(input_dir=tmp_path, work_dir=tmp_path / "work", make_placeholder=make)
    second = resolve_source(input_dir=tmp_path, work_dir=tmp_path / "work", make_placeholder=make)
    assert first.path == second.path == tmp_path / "work" / "outro_placeholder_16x9.mp4"
    assert len(made) == 1


def test_shipped_sidecar_matches_shipped_outro():
    src = resolve_source(make_placeholder=None)
    assert src.path.name == "outro_16x9.mp4"
    assert src.delogo == DelogoBox(1114, 666, 146, 42)


# --------------------------------------------------------------------------- #
# Text and timing
# --------------------------------------------------------------------------- #

def test_wrap_cta_breaks_on_words():
    lines = wrap_cta("Like the video and subscribe for the best GTA 6 moments every day!", 24)
    assert all(len(line) <= 24 for line in lines)
    assert " ".join(lines) == "Like the video and subscribe for the best GTA 6 moments every day!"


def test_outro_lasts_long_enough_for_cta_and_end_screen():
    assert outro_duration("16x9", tts_s=3.0) == 10.0       # end-screen floor
    assert outro_duration("9x16", tts_s=2.0) == 5.0        # animation length floor
    assert outro_duration("9x16", tts_s=4.6) == pytest.approx(0.8 + 4.6 + 0.6)  # CTA must finish


# --------------------------------------------------------------------------- #
# FFmpeg args (pure)
# --------------------------------------------------------------------------- #

def _args(aspect: str, *, delogo=DelogoBox(1, 2, 3, 4), music=Path("m.mp3")) -> list[str]:
    return build_outro_args(
        OutroSource(Path("o.mp4"), delogo),
        aspect,
        cta_file=Path("cta.txt"),
        tts_wav=Path("cta.wav"),
        music=music,
        duration=10.0,
        dst=Path("out.mp4"),
    )


def _graph(args: list[str]) -> str:
    return args[args.index("-filter_complex") + 1]


def test_16x9_is_full_hd_frozen_to_duration_with_cta_on_top():
    graph = _graph(_args("16x9"))
    assert "delogo=x=1:y=2:w=3:h=4" in graph
    assert "scale=1920:1080" in graph
    assert "tpad=stop_mode=clone" in graph
    assert "textfile=" in graph and "y=60" in graph


def test_9x16_puts_outro_over_blurred_copy():
    graph = _graph(_args("9x16"))
    assert "crop=1080:1920" in graph and "boxblur" in graph
    assert "overlay=(W-w)/2:(H-h)/2" in graph


def test_no_delogo_when_source_has_none():
    assert "delogo" not in _graph(_args("16x9", delogo=None))


def test_source_audio_is_dropped_and_cta_delayed_over_music():
    args = _args("16x9")
    graph = _graph(args)
    assert "[0:a]" not in graph
    assert "adelay=800" in graph
    assert "amix=inputs=2" in graph
    assert args[args.index("-t") + 1] == "10.000"


def test_without_music_only_the_cta_is_heard():
    graph = _graph(_args("16x9", music=None))
    assert "amix" not in graph and "adelay=800" in graph


def test_output_matches_clip_encoding():
    args = _args("16x9")
    for flag, value in (("-r", "30"), ("-pix_fmt", "yuv420p"), ("-ar", "48000"), ("-c:v", "libx264")):
        assert args[args.index(flag) + 1] == value


# --------------------------------------------------------------------------- #
# render_outro caching (ffmpeg + TTS faked)
# --------------------------------------------------------------------------- #

class Fakes:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.tts_calls: list[tuple[str, str]] = []
        self.renders: list[list[str]] = []

    def tts(self, text: str, key: str, voice: str) -> Path:
        self.tts_calls.append((text, voice))
        wav = self.tmp / f"tts_{key}.wav"
        wav.write_bytes(b"wav")
        return wav

    def run(self, args: list[str]) -> None:
        self.renders.append(args)
        Path(args[-1]).write_bytes(b"mp4")


def _render(fakes: Fakes, profile, aspect="16x9"):
    src = OutroSource(fakes.tmp / "o.mp4", None)
    if not src.path.exists():  # rewriting would change mtime and bust the cache
        src.path.write_bytes(b"v")
    return render_outro(
        profile, aspect, source=src, work_dir=fakes.tmp / "work",
        tts_fn=fakes.tts, run=fakes.run, probe=lambda p: 3.0, music=None,
    )


def test_render_outro_is_cached_per_profile_and_aspect(tmp_path):
    fakes = Fakes(tmp_path)
    en = load_profile("five-star-clips")

    first = _render(fakes, en)
    again = _render(fakes, en)
    vertical = _render(fakes, en, "9x16")

    assert first == again != vertical
    assert len(fakes.renders) == 2
    assert first.name == "outro_five-star-clips_16x9.mp4"


def test_changing_the_cta_rerenders(tmp_path):
    fakes = Fakes(tmp_path)
    en = load_profile("five-star-clips")
    _render(fakes, en)
    _render(fakes, en.model_copy(update={"outro_cta": "Subscribe!"}))
    assert len(fakes.renders) == 2


def test_cta_text_file_holds_wrapped_lines(tmp_path):
    fakes = Fakes(tmp_path)
    br = load_profile("five-star-clips-br")
    _render(fakes, br, "9x16")
    cta_file = Path(_graph(fakes.renders[0]).split("textfile='")[1].split("'")[0].replace("\\:", ":"))
    text = cta_file.read_text(encoding="utf-8")
    assert "\n" in text
    assert text.replace("\n", " ") == br.outro_cta
    assert fakes.tts_calls == [(br.outro_cta, br.tts_voice)]


# --------------------------------------------------------------------------- #
# Real FFmpeg
# --------------------------------------------------------------------------- #

@pytest.mark.slow
@pytest.mark.parametrize("aspect, size", [("16x9", (1920, 1080)), ("9x16", (1080, 1920))])
def test_render_outro_with_real_ffmpeg(tmp_path, aspect, size):
    from app.utils.ffmpeg_helpers import probe_duration, probe_resolution, run_ffmpeg

    src = tmp_path / "o.mp4"
    run_ffmpeg([
        "-y", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=24:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=5",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", str(src),
    ])
    wav = tmp_path / "cta.wav"
    run_ffmpeg(["-y", "-f", "lavfi", "-i", "sine=frequency=600:duration=3", "-ar", "24000", "-ac", "1", str(wav)])
    music = Path(outro.__file__).resolve().parents[2] / "assets" / "input" / "music" / "upbeat_future.mp3"

    out = render_outro(
        load_profile("five-star-clips-br"), aspect,
        source=OutroSource(src, DelogoBox(1114, 666, 146, 42)),
        work_dir=tmp_path / "work", tts_fn=lambda text, key, voice: wav,
        music=music if music.exists() else None,
    )
    assert probe_resolution(out) == size
    assert probe_duration(out) == pytest.approx(outro_duration(aspect, tts_s=3.0), abs=0.1)
