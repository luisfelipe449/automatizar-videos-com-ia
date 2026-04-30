"""Tests for `app.services.render_service`.

Two flavours:
- Pure unit tests over the filter-graph / argv builders (no ffmpeg invoked).
- One slow smoke test that actually runs ffmpeg end-to-end and validates the
  resulting MP4. Skipped automatically when ffmpeg isn't available.
"""

from __future__ import annotations

import math
import os
import re
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
    _build_rank_overlay,
    _escape_drawtext,
    _escape_subtitle_path,
    _pick_music_track,
    _scale_scene_durations,
    _XFADE_DURATION,
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
# Unit: _escape_drawtext / _build_rank_overlay
# --------------------------------------------------------------------------- #

def test_escape_drawtext_handles_colons_quotes_backslashes():
    # Mix of every token drawtext cares about: single quote, colon, percent, backslash.
    raw = "O'Connor: 50% off\\test"
    escaped = _escape_drawtext(raw)

    # Colon, percent, and backslash all get an escape.
    assert "\\:" in escaped
    assert "\\%" in escaped
    # Backslash in the input becomes an escaped pair.
    assert "\\\\" in escaped
    # No bare colon should remain — every `:` MUST be escaped.
    unescaped_colons = escaped.replace("\\:", "")
    assert ":" not in unescaped_colons
    # Single quote uses the quote-toggle trick: '\'' closes-out-reopens the
    # surrounding single-quoted argument, which is how ffmpeg accepts a
    # literal apostrophe. A bare `\'` would NOT be interpreted correctly.
    assert "'\\''" in escaped


def test_escape_drawtext_apostrophe_regression():
    """Regression: names like "Kitti's Hog-nosed Bat" used to break ffmpeg.

    The old escape (`\'`) closed the drawtext text='...' wrapper, and the
    parser tripped on the orphan `s Hog-nosed Bat`. The fix uses the shell-
    style '\\'' toggle so the literal apostrophe survives intact.
    """
    escaped = _escape_drawtext("Kitti's Hog-nosed Bat")
    # Expected exact output: Kitti'\''s Hog-nosed Bat
    assert escaped == "Kitti'\\''s Hog-nosed Bat"


def test_build_rank_overlay_when_unranked_is_empty():
    scene = Scene(
        id=1,
        scene_text="hook",
        image_prompt="p",
        duration_seconds=4,
    )
    assert _build_rank_overlay(scene) == ""


def test_build_rank_overlay_includes_drawtext_with_correct_text():
    scene = Scene(
        id=3,
        scene_text="Pelé is the GOAT.",
        image_prompt="p",
        duration_seconds=5,
        rank=10,
        rank_title="Pelé",
    )
    overlay = _build_rank_overlay(scene)

    # Leading comma so the fragment slots straight after zoompan.
    assert overlay.startswith(",drawtext=")
    # The composed on-screen text is present (with no colons, so escaping
    # doesn't alter "10 - Pelé").
    assert "text='10 - Pelé'" in overlay
    # Style knobs from the spec: 120pt, black outline, top-80 position.
    assert "fontsize=120" in overlay
    assert "bordercolor=black" in overlay
    assert "borderw=6" in overlay
    assert "fontcolor=white" in overlay
    assert "y=80" in overlay
    # Font path references the bundled Bebas Neue TTF.
    assert "BebasNeue-Regular.ttf" in overlay
    assert "fontfile='" in overlay


# --------------------------------------------------------------------------- #
# Unit: _pick_music_track
# --------------------------------------------------------------------------- #

def test_pick_music_track_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Stage 3 fake mp3 files. _pick_music_track only lists them, it doesn't
    # decode — so empty files are fine for this unit test.
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "a.mp3").write_bytes(b"")
    (music_dir / "b.mp3").write_bytes(b"")
    (music_dir / "c.mp3").write_bytes(b"")
    monkeypatch.setattr(render_service, "_MUSIC_DIR", music_dir)

    # Same job_id → same track every call (reproducible re-renders).
    first = _pick_music_track("foo")
    assert first is not None
    for _ in range(5):
        assert _pick_music_track("foo") == first

    # ~20 job_ids rotate through at least 2 distinct tracks — the distribution
    # isn't required to be uniform, just non-degenerate.
    picks = {_pick_music_track(f"job-{i}") for i in range(20)}
    assert len(picks) > 1, f"expected varied picks across 20 job_ids, got {picks}"


def test_pick_music_track_empty_dir_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    monkeypatch.setattr(render_service, "_MUSIC_DIR", music_dir)
    assert _pick_music_track("foo") is None


def test_pick_music_track_missing_dir_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(render_service, "_MUSIC_DIR", missing)
    assert _pick_music_track("foo") is None


# --------------------------------------------------------------------------- #
# Unit: _build_filter_graph
# --------------------------------------------------------------------------- #

def test_build_filter_graph_has_zoompan_xfade_and_subtitles(tmp_path: Path):
    spec = _make_spec(tmp_path, n_scenes=3, durations=[2, 3, 4])
    graph = _build_filter_graph(spec)

    # One scale+zoompan chain per scene.
    for idx, dur in enumerate([2, 3, 4]):
        assert f"[{idx}:v]scale=1080:1920" in graph
        assert f"crop=1080:1920" in graph
        frames = dur * 30
        assert f"d={frames}" in graph
        assert f"[v{idx}]" in graph

    # No concat — we now crossfade with xfade instead.
    assert "concat=n=" not in graph
    # N-1 xfade entries for N scenes.
    xfade_count = len(re.findall(r"xfade=transition=fade", graph))
    assert xfade_count == 2, f"expected 2 xfades for 3 scenes, got {xfade_count}"
    assert "duration=0.4" in graph
    # Subtitles baked in after the xfade chain, referencing the .ass captions file.
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


def test_xfade_offsets_accumulate(tmp_path: Path):
    """The k-th xfade's offset = sum(dur[0..k]) - (k+1) * XFADE_DURATION."""
    spec = _make_spec(tmp_path, n_scenes=3, durations=[2, 3, 4])
    # Use pre-scaled durations directly so the math is unambiguous.
    graph = _build_filter_graph(spec, durations=[2.0, 3.0, 4.0])

    # Extract both offsets in order of appearance.
    offsets = [float(m) for m in re.findall(r"offset=([0-9.]+)", graph)]
    assert len(offsets) == 2, f"expected 2 offsets, got {offsets}"

    # First xfade: between the result of scene 0 and scene 1. It should kick in
    # 0.4s before scene 0 ends on the cumulative timeline: 2 - 0.4 = 1.6.
    assert math.isclose(offsets[0], 2.0 - 0.4, abs_tol=1e-6)
    # Second xfade: between (fade of 0+1) and scene 2. Cumulative 2+3=5, minus
    # 2 * 0.4 = 0.8 worth of overlap already consumed: 4.2.
    assert math.isclose(offsets[1], 2.0 + 3.0 - 2 * 0.4, abs_tol=1e-6)


def test_scale_scene_durations_compensates_for_xfades(tmp_path: Path):
    """With N scenes, target_sum = audio + (N-1) * XFADE to absorb the overlap."""
    spec = _make_spec(tmp_path, n_scenes=3, durations=[1, 1, 1])
    scaled = _scale_scene_durations(spec.scenes, audio_duration_s=10.0)

    # target_sum = 10 + 2 * 0.4 = 10.8, split 1:1:1 → 3.6 per scene.
    assert len(scaled) == 3
    for dur in scaled:
        assert math.isclose(dur, 3.6, abs_tol=1e-6)
    # And the post-overlap total lands on the audio duration exactly.
    assert math.isclose(
        sum(scaled) - (len(scaled) - 1) * _XFADE_DURATION, 10.0, abs_tol=1e-6
    )


def test_scale_scene_durations_single_scene_no_compensation(tmp_path: Path):
    """Single-scene specs don't crossfade, so target_sum == audio_duration."""
    spec = _make_spec(tmp_path, n_scenes=1, durations=[5])
    scaled = _scale_scene_durations(spec.scenes, audio_duration_s=8.0)
    assert len(scaled) == 1
    assert math.isclose(scaled[0], 8.0, abs_tol=1e-6)


def test_single_scene_has_no_xfade(tmp_path: Path):
    """A 1-scene spec emits the raw [v0] -> subtitles chain with no xfade."""
    spec = _make_spec(tmp_path, n_scenes=1, durations=[3])
    graph = _build_filter_graph(spec)

    # No xfade, no concat — [v0] feeds straight into subtitles.
    assert "xfade=" not in graph
    assert "concat=" not in graph
    # Subtitles still applied, and the terminal label is still [v].
    assert "[v0]subtitles=" in graph
    assert graph.rstrip().endswith("[v]")


def test_scene_shorter_than_xfade_raises(tmp_path: Path):
    """Rescaling that produces a sub-XFADE scene duration must RenderError."""
    # 20 scenes over 7s audio — even before compensation each scene is 0.35s,
    # well under _XFADE_DURATION (0.4s). Compensation would push the target
    # sum up (7 + 19 * 0.4 = 14.6, so each scene ~0.73s) which is fine — so
    # instead we force a degenerate audio_duration to make at least one scene
    # fall below the floor.
    spec = _make_spec(tmp_path, n_scenes=20, durations=[1] * 20)
    # Pick audio_duration such that (audio + 19*0.4) / 20 < 0.4  →  audio < 0.4.
    with pytest.raises(RenderError, match="xfade"):
        _scale_scene_durations(spec.scenes, audio_duration_s=0.1)


def test_build_filter_graph_raises_on_empty_scenes(tmp_path: Path):
    # RenderSpec itself enforces min_length=1, so we hand-build an object with
    # cleared scenes to hit the defensive check inside the builder.
    spec = _make_spec(tmp_path, n_scenes=1)
    spec.scenes.clear()
    with pytest.raises(ValueError):
        _build_filter_graph(spec)


def test_filter_graph_with_ranked_scenes_embeds_drawtext(tmp_path: Path):
    """Only ranked scenes emit drawtext; unranked neighbors stay clean."""
    # 3 scenes: #1 and #3 unranked (hook + closing), #2 ranked (a fact).
    scenes = [
        Scene(id=1, scene_text="hook", image_prompt="p1", duration_seconds=2),
        Scene(
            id=2,
            scene_text="Pelé is the GOAT.",
            image_prompt="p2",
            duration_seconds=3,
            rank=10,
            rank_title="Pelé",
        ),
        Scene(id=3, scene_text="closing", image_prompt="p3", duration_seconds=4),
    ]
    spec = RenderSpec(
        job_id="test-job",
        scenes=scenes,
        image_paths=[tmp_path / f"s{i + 1}.png" for i in range(3)],
        narration_path=tmp_path / "n.wav",
        captions_path=tmp_path / "c.ass",
        output_path=tmp_path / "out.mp4",
    )
    graph = _build_filter_graph(spec)

    # Exactly one drawtext in the whole graph — the ranked fact scene.
    assert graph.count("drawtext=") == 1
    # The drawtext lives inside the SECOND scene's chain (the one ending [v1]),
    # i.e. AFTER its zoompan and BEFORE the [v1] label closes that chain.
    second_chain = next(
        part for part in graph.split(";") if part.endswith("[v1]")
    )
    assert "drawtext=" in second_chain
    assert "text='10 - Pelé'" in second_chain
    # Unranked chains must NOT carry a drawtext (no rank ⇒ no overlay).
    first_chain = next(part for part in graph.split(";") if part.endswith("[v0]"))
    third_chain = next(part for part in graph.split(";") if part.endswith("[v2]"))
    assert "drawtext=" not in first_chain
    assert "drawtext=" not in third_chain


def test_build_filter_graph_without_music_has_no_amix(tmp_path: Path):
    # Sanity: the default (music_path=None) output must NOT contain any of the
    # mix-specific tokens, so downstream consumers that just map <N>:a still
    # work identically to before.
    spec = _make_spec(tmp_path, n_scenes=2)
    graph = _build_filter_graph(spec)
    assert "amix" not in graph
    assert "volume=0.1" not in graph
    assert "[a]" not in graph.split(";")[-1]  # last chain ends with [v], not [a]


def test_build_filter_graph_with_music_adds_amix(tmp_path: Path):
    spec = _make_spec(tmp_path, n_scenes=2)
    music = tmp_path / "bg.mp3"
    music.write_bytes(b"")  # contents irrelevant — builder is pure

    graph = _build_filter_graph(spec, music_path=music)

    # -20 dB ≈ 0.1 linear amplitude on the music stream.
    assert "volume=0.1" in graph
    # Mix narration + music into a single [a] output, pinned to narration length.
    assert "amix=inputs=2" in graph
    assert "duration=first" in graph
    # Narration is input index len(scenes); music is len(scenes)+1.
    assert "[2:a]" in graph  # narration
    assert "[3:a]" in graph  # music
    # Final audio label that -map "[a]" will reference.
    assert graph.rstrip().endswith("[a]")
    # Video label must still be present — the mix shouldn't clobber the video graph.
    assert "[v]" in graph


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
    # Two scenes → exactly one xfade, no concat.
    assert "concat=n=" not in fc_value
    assert fc_value.count("xfade=transition=fade") == 1
    assert "subtitles=" in fc_value

    # -map "[v]" and -map <audio>:a where audio index = len(scenes) = 2.
    # With music_path=None we stay on the raw narration stream, NOT [a].
    map_indices = [i for i, a in enumerate(cmd) if a == "-map"]
    assert len(map_indices) == 2
    assert cmd[map_indices[0] + 1] == "[v]"
    assert cmd[map_indices[1] + 1] == "2:a"
    # No mp3 input should be present in the no-music variant.
    assert not any(a == "-i" and cmd[i + 1].endswith(".mp3") for i, a in enumerate(cmd))

    # Encoder args.
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "-preset" in cmd and cmd[cmd.index("-preset") + 1] == "medium"
    assert "-crf" in cmd and cmd[cmd.index("-crf") + 1] == "23"
    assert "-movflags" in cmd and cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert "-shortest" in cmd

    # Output path is the last arg.
    assert cmd[-1] == str(spec.output_path)


def test_build_ffmpeg_command_with_music_adds_input_and_maps_amix(tmp_path: Path):
    spec = _make_spec(tmp_path, n_scenes=2, durations=[4, 5])
    music = tmp_path / "bg.mp3"
    music.write_bytes(b"")

    cmd = _build_ffmpeg_command(spec, "/usr/bin/ffmpeg", music_path=music)

    # One narration -i (.wav) and one music -i (.mp3), in that order.
    i_positions = [i for i, a in enumerate(cmd) if a == "-i"]
    narration_i = [i for i in i_positions if cmd[i + 1].endswith(".wav")]
    music_i = [i for i in i_positions if cmd[i + 1].endswith(".mp3")]
    assert len(narration_i) == 1
    assert len(music_i) == 1
    # Music input must come AFTER narration so the filter-graph indices
    # (len(scenes) for narration, len(scenes)+1 for music) line up.
    assert music_i[0] > narration_i[0]
    assert cmd[music_i[0] + 1] == str(music)

    # -map "[v]" + -map "[a]" (the mixed output, NOT <N>:a).
    map_indices = [i for i, a in enumerate(cmd) if a == "-map"]
    assert len(map_indices) == 2
    assert cmd[map_indices[0] + 1] == "[v]"
    assert cmd[map_indices[1] + 1] == "[a]"
    # Raw narration stream index must NOT be mapped directly when mixing.
    assert "2:a" not in {cmd[i + 1] for i in map_indices}

    # Filter graph carries the mix nodes.
    fc_value = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2" in fc_value
    assert "volume=0.1" in fc_value

    # Output path still trails.
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


def _wire_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> str:
    """Point `ffmpeg_helpers` at the real binary; return the ffmpeg exe path."""
    if not os.getenv("FFMPEG_PATH") and Path(_FFMPEG_PATH_ENV).exists():
        monkeypatch.setenv("FFMPEG_PATH", _FFMPEG_PATH_ENV)
        # `app.config.FFMPEG_PATH` was snapshotted at import; patch it too so
        # `ffmpeg_helpers._resolve` sees the override.
        monkeypatch.setattr("app.config.FFMPEG_PATH", _FFMPEG_PATH_ENV)
        monkeypatch.setattr(
            "app.utils.ffmpeg_helpers.FFMPEG_PATH", _FFMPEG_PATH_ENV
        )
    return (
        os.getenv("FFMPEG_PATH")
        or shutil.which("ffmpeg")
        or _FFMPEG_PATH_ENV
    )


def _build_smoke_inputs(tmp_path: Path, ffmpeg_exe: str) -> tuple[list[Path], Path, Path]:
    """Generate two PNGs, a 2s silent WAV narration, and a minimal ASS file."""
    red = tmp_path / "scene_01.png"
    blue = tmp_path / "scene_02.png"
    Image.new("RGB", (1080, 1920), (255, 0, 0)).save(red, "PNG")
    Image.new("RGB", (1080, 1920), (0, 0, 255)).save(blue, "PNG")

    narration = tmp_path / "narration.wav"
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

    return [red, blue], narration, captions


@pytest.mark.slow
@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not available")
def test_render_service_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _wire_ffmpeg(monkeypatch)
    # Force the no-music code path so this test stays focused on the base
    # pipeline behavior regardless of what's in assets/input/music at runtime.
    empty_music = tmp_path / "empty_music"
    empty_music.mkdir()
    monkeypatch.setattr(render_service, "_MUSIC_DIR", empty_music)

    ffmpeg_exe = (
        os.getenv("FFMPEG_PATH")
        or shutil.which("ffmpeg")
        or _FFMPEG_PATH_ENV
    )
    images, narration, captions = _build_smoke_inputs(tmp_path, ffmpeg_exe)

    output = tmp_path / "rendered" / "final.mp4"
    spec = RenderSpec(
        job_id="smoke",
        scenes=[
            Scene(id=1, scene_text="a", image_prompt="p1", duration_seconds=1),
            Scene(id=2, scene_text="b", image_prompt="p2", duration_seconds=1),
        ],
        image_paths=images,
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


@pytest.mark.slow
@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not available")
def test_render_service_smoke_with_music(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end render with a real music bed.

    Generates a 3s 440 Hz tone mp3 as the "music" library, points _MUSIC_DIR
    at it, and asserts the rendered mp4:
      - has exactly one audio stream (narration + music mixed down to one)
      - audio duration ~= narration duration (1s), NOT music duration (3s),
        proving `duration=first` cut the music at the narration boundary.
    """
    ffmpeg_exe = _wire_ffmpeg(monkeypatch)
    images, _, captions = _build_smoke_inputs(tmp_path, ffmpeg_exe)

    # Short 1s silent narration so we can tell narration-length from music-length.
    narration = tmp_path / "narration.wav"
    subprocess.run(
        [
            ffmpeg_exe, "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=24000:cl=mono",
            "-t", "1",
            str(narration),
        ],
        check=True,
        capture_output=True,
    )

    # Rewrite the ASS cue to fit within the new 1s narration window.
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
        "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,"
        "{\\k50}hello {\\k50}music\n",
        encoding="utf-8",
        newline="\n",
    )

    # Fake music library: a single 3s 440 Hz tone mp3. 3s > 1s narration so we
    # can verify `duration=first` clips it correctly.
    music_dir = tmp_path / "music_lib"
    music_dir.mkdir()
    music_track = music_dir / "tone.mp3"
    subprocess.run(
        [
            ffmpeg_exe, "-y",
            "-f", "lavfi",
            "-i", "sine=frequency=440:duration=3",
            "-acodec", "libmp3lame",
            str(music_track),
        ],
        check=True,
        capture_output=True,
    )
    assert music_track.exists() and music_track.stat().st_size > 0
    monkeypatch.setattr(render_service, "_MUSIC_DIR", music_dir)

    output = tmp_path / "rendered" / "final.mp4"
    spec = RenderSpec(
        job_id="smoke-music",
        scenes=[
            Scene(id=1, scene_text="a", image_prompt="p1", duration_seconds=1),
            Scene(id=2, scene_text="b", image_prompt="p2", duration_seconds=1),
        ],
        image_paths=images,
        narration_path=narration,
        captions_path=captions,
        output_path=output,
    )

    result = render_short(spec)
    assert result.exists()

    # 1) Audio duration must match the narration (~1s), NOT the 3s music track.
    #    This is the clearest proof that `duration=first` + `-shortest` cut the
    #    music rather than letting it drag the video out.
    from app.utils.ffmpeg_helpers import ffprobe_bin, probe_duration

    dur = probe_duration(result)
    assert 0.6 <= dur <= 1.4, f"expected ~1s (narration), got {dur:.2f}s"

    # 2) Exactly one audio stream — the amix output. If we'd accidentally mapped
    #    both, ffprobe would report 2 streams.
    probe = subprocess.run(
        [
            ffprobe_bin(), "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(result),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    audio_stream_lines = [ln for ln in probe.stdout.splitlines() if ln.strip()]
    assert len(audio_stream_lines) == 1, (
        f"expected 1 audio stream, got {len(audio_stream_lines)}: "
        f"{audio_stream_lines!r}"
    )
