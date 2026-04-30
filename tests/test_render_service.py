"""Tests for `app.services.render_service`.

Two flavours:
- Pure unit tests over the filter-graph / argv builders (no ffmpeg invoked).
- One slow smoke test that actually runs ffmpeg end-to-end and validates the
  resulting MP4. Skipped automatically when ffmpeg isn't available.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.models import RenderSpec, Scene
from app.services import render_service
from app.services.render_service import (
    RenderError,
    _build_ffmpeg_command,
    _build_filter_graph,
    _escape_subtitle_path,
    render_short,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

def _make_spec(
    tmp_path: Path,
    *,
    n_scenes: int = 2,
    durations: list[int] | None = None,
    create_files: bool = False,
) -> RenderSpec:
    """Build a RenderSpec whose paths live under tmp_path.

    When `create_files=True`, creates empty placeholder files for every
    referenced path so pre-flight existence checks pass.
    """
    durations = durations or [1] * n_scenes
    assert len(durations) == n_scenes
    scenes = [
        Scene(
            id=i + 1,
            scene_text=f"scene {i + 1}",
            image_prompt=f"prompt {i + 1}",
            duration_seconds=durations[i],
        )
        for i in range(n_scenes)
    ]
    image_paths = [tmp_path / f"scene_{i + 1:02d}.png" for i in range(n_scenes)]
    narration = tmp_path / "narration.wav"
    captions = tmp_path / "captions.ass"
    output = tmp_path / "rendered" / "final.mp4"

    if create_files:
        for p in image_paths:
            p.write_bytes(b"\x89PNG\r\n\x1a\n")  # PNG magic, enough for existence
        narration.write_bytes(b"RIFF")
        captions.write_text(
            "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\\k100}hi\n",
            encoding="utf-8",
        )
        output.parent.mkdir(parents=True, exist_ok=True)

    return RenderSpec(
        job_id="test-job",
        scenes=scenes,
        image_paths=image_paths,
        narration_path=narration,
        captions_path=captions,
        output_path=output,
    )


# --------------------------------------------------------------------------- #
# Unit: _escape_subtitle_path
# --------------------------------------------------------------------------- #

def test_escape_subtitle_path_escapes_windows_drive_colon(tmp_path: Path):
    ass = tmp_path / "captions.ass"
    ass.write_text("x", encoding="utf-8")
    escaped = _escape_subtitle_path(ass)

    # No OS-native backslashes (the only legitimate backslash is the `\:`
    # drive-colon escape), and the filename uses forward slashes.
    assert "/" in escaped
    # At least one escaped colon is present on Windows (drive letter).
    # On POSIX the path has no colon so this assertion is skipped.
    resolved = str(ass.resolve())
    if ":" in resolved:
        assert r"\:" in escaped
        # The bare unescaped "C:" form must not leak through.
        assert not any(
            escaped.startswith(f"{drive}:") for drive in "CDEFGH"
        ), f"unescaped drive letter in {escaped!r}"
    # No Windows-style backslash directory separators should remain.
    # (We only allow the single `\:` escape sequence.)
    cleaned = escaped.replace(r"\:", "")
    assert "\\" not in cleaned, f"unexpected backslash in {escaped!r}"


# --------------------------------------------------------------------------- #
# Unit: _build_filter_graph
# --------------------------------------------------------------------------- #

def test_build_filter_graph_has_zoompan_concat_and_subtitles(tmp_path: Path):
    spec = _make_spec(tmp_path, n_scenes=3, durations=[2, 3, 4])
    graph = _build_filter_graph(spec)

    # One scale+zoompan chain per scene.
    for idx, dur in enumerate([2, 3, 4]):
        assert f"[{idx}:v]scale=1080:1920" in graph
        assert f"crop=1080:1920" in graph
        frames = dur * 30
        assert f"d={frames}" in graph
        assert f"[v{idx}]" in graph

    # Concat over every vN label.
    assert "[v0][v1][v2]concat=n=3:v=1:a=0" in graph
    # Subtitles baked in after concat, referencing the .ass captions file.
    assert "subtitles=" in graph
    assert "captions.ass" in graph
    # `fontsdir=` is passed so libass can find Bebas Neue without a system install.
    assert "fontsdir=" in graph
    assert "fonts" in graph  # path fragment for assets/fonts
    # `force_style` must NOT appear: it would override the ASS file's own
    # Style block and kill the karaoke PrimaryColour/SecondaryColour coloring.
    assert "force_style" not in graph
    # Final label that -map "[v]" will reference.
    assert graph.rstrip().endswith("[v]")


def test_build_filter_graph_raises_on_empty_scenes(tmp_path: Path):
    # RenderSpec itself enforces min_length=1, so we hand-build an object with
    # cleared scenes to hit the defensive check inside the builder.
    spec = _make_spec(tmp_path, n_scenes=1)
    spec.scenes.clear()
    with pytest.raises(ValueError):
        _build_filter_graph(spec)


# --------------------------------------------------------------------------- #
# Unit: _build_ffmpeg_command
# --------------------------------------------------------------------------- #

def test_build_ffmpeg_command_two_scenes(tmp_path: Path):
    spec = _make_spec(tmp_path, n_scenes=2, durations=[4, 5])
    cmd = _build_ffmpeg_command(spec, "/usr/bin/ffmpeg")

    # First argument is the binary.
    assert cmd[0] == "/usr/bin/ffmpeg"

    # Exactly two `-framerate 1 -loop 1 -t 1 -i <image>` groups. Each input
    # feeds a single frame; zoompan's `d=` controls the per-scene duration.
    loop_indices = [i for i, a in enumerate(cmd) if a == "-loop"]
    assert len(loop_indices) == 2
    for i in loop_indices:
        assert cmd[i - 2] == "-framerate"
        assert cmd[i - 1] == "1"
        assert cmd[i + 1] == "1"
        assert cmd[i + 2] == "-t"
        assert cmd[i + 3] == "1"
        assert cmd[i + 4] == "-i"
        assert cmd[i + 5].endswith(".png")

    # Filter graph should carry the per-scene durations via zoompan `d=`.
    fc_value = cmd[cmd.index("-filter_complex") + 1]
    # 4s * 30fps = 120 frames, 5s * 30fps = 150 frames.
    assert "d=120" in fc_value
    assert "d=150" in fc_value

    # The narration -i appears once, after the image inputs.
    narration_positions = [
        i for i, a in enumerate(cmd) if a == "-i" and cmd[i + 1].endswith(".wav")
    ]
    assert len(narration_positions) == 1

    # filter_complex + its argument exists.
    fc_idx = cmd.index("-filter_complex")
    fc_value = cmd[fc_idx + 1]
    assert "zoompan" in fc_value
    assert "concat=n=2" in fc_value
    assert "subtitles=" in fc_value

    # -map "[v]" and -map <audio>:a where audio index = len(scenes) = 2.
    map_indices = [i for i, a in enumerate(cmd) if a == "-map"]
    assert len(map_indices) == 2
    assert cmd[map_indices[0] + 1] == "[v]"
    assert cmd[map_indices[1] + 1] == "2:a"

    # Encoder args.
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "-preset" in cmd and cmd[cmd.index("-preset") + 1] == "medium"
    assert "-crf" in cmd and cmd[cmd.index("-crf") + 1] == "23"
    assert "-movflags" in cmd and cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert "-shortest" in cmd

    # Output path is the last arg.
    assert cmd[-1] == str(spec.output_path)


def test_build_ffmpeg_command_zero_scenes_raises(tmp_path: Path):
    spec = _make_spec(tmp_path, n_scenes=1)
    spec.scenes.clear()
    with pytest.raises(ValueError):
        _build_ffmpeg_command(spec, "ffmpeg")


# --------------------------------------------------------------------------- #
# Unit: render_short pre-flight errors
# --------------------------------------------------------------------------- #

def test_render_short_missing_image_raises(tmp_path: Path):
    spec = _make_spec(tmp_path, n_scenes=1, create_files=False)
    with pytest.raises(RenderError, match="scene image not found"):
        render_short(spec)


def test_render_short_missing_narration_raises(tmp_path: Path):
    spec = _make_spec(tmp_path, n_scenes=1, create_files=False)
    # Create only the image so the error advances to narration.
    for p in spec.image_paths:
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(RenderError, match="narration not found"):
        render_short(spec)


# --------------------------------------------------------------------------- #
# Smoke: real ffmpeg end-to-end
# --------------------------------------------------------------------------- #

_FFMPEG_PATH_ENV = (
    "C:/Users/1luis/AppData/Local/Microsoft/WinGet/Packages/"
    "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/"
    "ffmpeg-8.1-full_build/bin/ffmpeg.exe"
)


def _ffmpeg_available() -> bool:
    if shutil.which("ffmpeg"):
        return True
    if os.getenv("FFMPEG_PATH") and Path(os.environ["FFMPEG_PATH"]).exists():
        return True
    if Path(_FFMPEG_PATH_ENV).exists():
        return True
    return False


@pytest.mark.slow
@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not available")
def test_render_service_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Point ffmpeg_helpers at the real binary if FFMPEG_PATH isn't already set.
    if not os.getenv("FFMPEG_PATH") and Path(_FFMPEG_PATH_ENV).exists():
        monkeypatch.setenv("FFMPEG_PATH", _FFMPEG_PATH_ENV)
        # `app.config.FFMPEG_PATH` was snapshotted at import; patch it too so
        # `ffmpeg_helpers._resolve` sees the override.
        monkeypatch.setattr("app.config.FFMPEG_PATH", _FFMPEG_PATH_ENV)
        monkeypatch.setattr(
            "app.utils.ffmpeg_helpers.FFMPEG_PATH", _FFMPEG_PATH_ENV
        )

    # Two tiny PNGs.
    red = tmp_path / "scene_01.png"
    blue = tmp_path / "scene_02.png"
    Image.new("RGB", (1080, 1920), (255, 0, 0)).save(red, "PNG")
    Image.new("RGB", (1080, 1920), (0, 0, 255)).save(blue, "PNG")

    # Silent narration WAV, generated via ffmpeg itself.
    narration = tmp_path / "narration.wav"
    ffmpeg_exe = (
        os.getenv("FFMPEG_PATH")
        or shutil.which("ffmpeg")
        or _FFMPEG_PATH_ENV
    )
    subprocess.run(
        [
            ffmpeg_exe, "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=24000:cl=mono",
            "-t", "2",
            str(narration),
        ],
        check=True,
        capture_output=True,
    )

    # Minimal ASS file with one karaoke cue spanning the whole 0-2s window.
    captions = tmp_path / "captions.ass"
    captions.write_text(
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Bebas Neue,56,&H0000D7FF,&H00FFFFFF,&H00000000,"
        "&H00000000,0,0,0,0,100,100,0,0,1,3,1,2,60,60,300,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,"
        "{\\k100}hello {\\k100}smoke\n",
        encoding="utf-8",
        newline="\n",
    )

    output = tmp_path / "rendered" / "final.mp4"
    spec = RenderSpec(
        job_id="smoke",
        scenes=[
            Scene(id=1, scene_text="a", image_prompt="p1", duration_seconds=1),
            Scene(id=2, scene_text="b", image_prompt="p2", duration_seconds=1),
        ],
        image_paths=[red, blue],
        narration_path=narration,
        captions_path=captions,
        output_path=output,
    )

    result = render_short(spec)
    assert result.exists()
    assert result == output.resolve() or result == output

    # Duration ~2s (allow generous slack for encoder quirks).
    from app.utils.ffmpeg_helpers import probe_duration, probe_resolution

    dur = probe_duration(result)
    assert 1.6 <= dur <= 2.4, f"unexpected duration {dur:.2f}s"

    w, h = probe_resolution(result)
    assert (w, h) == (1080, 1920)
