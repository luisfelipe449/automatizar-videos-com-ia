"""Tests for `app.models` — the Pydantic contract between pipeline stages."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import RenderSpec, Scene, Script


def _valid_script_kwargs(**overrides):
    """Helper: minimal kwargs for a valid Script. Override anything per-test."""
    base = {
        "job_id": "2026-04-30_tema-slug_abc123",
        "title": "Título do Short",
        "description": "Descrição curta do vídeo. #Shorts",
        "tags": ["ciencia", "espaco"],
        "narration": "Texto da narração.",
        "scenes": [
            {"id": 1, "scene_text": "cena 1", "image_prompt": "prompt 1", "duration_seconds": 4},
            {"id": 2, "scene_text": "cena 2", "image_prompt": "prompt 2", "duration_seconds": 4},
            {"id": 3, "scene_text": "cena 3", "image_prompt": "prompt 3", "duration_seconds": 4},
        ],
    }
    base.update(overrides)
    return base


def test_valid_script_parses_cleanly():
    script = Script(**_valid_script_kwargs())
    assert script.job_id == "2026-04-30_tema-slug_abc123"
    assert len(script.scenes) == 3
    assert sum(s.duration_seconds for s in script.scenes) == 12


def test_total_duration_over_limit_rejected():
    # 3 scenes * 20s = 60s > 58s budget.
    scenes = [
        {"id": 1, "scene_text": "a", "image_prompt": "p", "duration_seconds": 20},
        {"id": 2, "scene_text": "b", "image_prompt": "p", "duration_seconds": 20},
        {"id": 3, "scene_text": "c", "image_prompt": "p", "duration_seconds": 20},
    ]
    with pytest.raises(ValidationError, match="exceeds"):
        Script(**_valid_script_kwargs(scenes=scenes))


def test_scene_ids_non_contiguous_rejected():
    scenes = [
        {"id": 1, "scene_text": "a", "image_prompt": "p", "duration_seconds": 4},
        {"id": 3, "scene_text": "b", "image_prompt": "p", "duration_seconds": 4},
        {"id": 4, "scene_text": "c", "image_prompt": "p", "duration_seconds": 4},
    ]
    with pytest.raises(ValidationError, match="contiguous"):
        Script(**_valid_script_kwargs(scenes=scenes))


def test_scene_ids_not_starting_at_one_rejected():
    scenes = [
        {"id": 2, "scene_text": "a", "image_prompt": "p", "duration_seconds": 4},
        {"id": 3, "scene_text": "b", "image_prompt": "p", "duration_seconds": 4},
        {"id": 4, "scene_text": "c", "image_prompt": "p", "duration_seconds": 4},
    ]
    with pytest.raises(ValidationError, match="contiguous"):
        Script(**_valid_script_kwargs(scenes=scenes))


def test_description_missing_shorts_gets_normalized():
    script = Script(**_valid_script_kwargs(description="Descrição sem hashtag"))
    assert script.description.endswith("#Shorts")
    assert "#Shorts" in script.description


def test_description_with_lowercase_shorts_passes_unchanged():
    desc = "Descrição com hashtag #shorts em caixa baixa"
    script = Script(**_valid_script_kwargs(description=desc))
    assert script.description == desc


def test_render_spec_mismatched_scene_paths_rejected():
    scenes = [
        Scene(id=1, scene_text="a", image_prompt="p", duration_seconds=4),
        Scene(id=2, scene_text="b", image_prompt="p", duration_seconds=4),
    ]
    with pytest.raises(ValidationError, match="must match"):
        RenderSpec(
            job_id="job1",
            scenes=scenes,
            scene_paths=[Path("scene_01.png")],  # only 1, need 2
            narration_path=Path("narration.wav"),
            captions_path=Path("captions.ass"),
            output_path=Path("final.mp4"),
        )


def test_render_spec_accepts_mixed_image_and_video_paths():
    """A RenderSpec can carry a mix of .png (image) and .mp4 (video) per scene."""
    scenes = [
        Scene(id=1, scene_text="a", image_prompt="p", duration_seconds=4),
        Scene(id=2, scene_text="b", image_prompt="p", duration_seconds=4),
    ]
    spec = RenderSpec(
        job_id="job-mix",
        scenes=scenes,
        scene_paths=[Path("scene_01.mp4"), Path("scene_02.png")],
        narration_path=Path("narration.wav"),
        captions_path=Path("captions.ass"),
        output_path=Path("final.mp4"),
    )
    # Field is preserved in the same order; suffix-dispatch happens downstream.
    assert spec.scene_paths[0].suffix == ".mp4"
    assert spec.scene_paths[1].suffix == ".png"


def test_title_over_100_chars_rejected():
    with pytest.raises(ValidationError):
        Script(**_valid_script_kwargs(title="x" * 101))


def test_script_json_roundtrip_equal():
    original = Script(**_valid_script_kwargs())
    rebuilt = Script.model_validate_json(original.model_dump_json())
    assert rebuilt == original


# --------------------------------------------------------------------------- #
# Scene.rank / rank_title — optional ranking-overlay fields
# --------------------------------------------------------------------------- #

def test_scene_rank_pair_both_set_ok():
    scene = Scene(
        id=1,
        scene_text="fact about Pelé",
        image_prompt="a stadium at golden hour",
        duration_seconds=5,
        rank=10,
        rank_title="Pelé",
    )
    assert scene.rank == 10
    assert scene.rank_title == "Pelé"


def test_scene_rank_pair_both_null_ok():
    # Default behaviour (no ranking topic): fields are null and the scene still validates.
    scene = Scene(
        id=1,
        scene_text="opening hook",
        image_prompt="a neon-lit street",
        duration_seconds=4,
    )
    assert scene.rank is None
    assert scene.rank_title is None


def test_scene_rank_only_number_rejected():
    with pytest.raises(ValidationError, match="rank and rank_title"):
        Scene(
            id=1,
            scene_text="a",
            image_prompt="p",
            duration_seconds=4,
            rank=5,
            rank_title=None,
        )


def test_scene_rank_only_title_rejected():
    with pytest.raises(ValidationError, match="rank and rank_title"):
        Scene(
            id=1,
            scene_text="a",
            image_prompt="p",
            duration_seconds=4,
            rank=None,
            rank_title="Pelé",
        )


def test_scene_rank_out_of_range():
    # rank=0 is below the ge=1 floor.
    with pytest.raises(ValidationError):
        Scene(
            id=1,
            scene_text="a",
            image_prompt="p",
            duration_seconds=4,
            rank=0,
            rank_title="x",
        )
    # rank=101 is above the le=100 ceiling.
    with pytest.raises(ValidationError):
        Scene(
            id=1,
            scene_text="a",
            image_prompt="p",
            duration_seconds=4,
            rank=101,
            rank_title="x",
        )


def test_scene_rank_title_too_long():
    # >40 chars — overlay wouldn't fit cleanly on a 1080px-wide Short.
    with pytest.raises(ValidationError):
        Scene(
            id=1,
            scene_text="a",
            image_prompt="p",
            duration_seconds=4,
            rank=1,
            rank_title="x" * 41,
        )
