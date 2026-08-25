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


# --------------------------------------------------------------------------- #
# Narration coverage
#
# `scene_text` is contractually "the exact line of narration this scene
# covers" (both system prompts say so). Nothing used to check it, so a script
# whose scenes covered only the first half of the narration passed validation
# — the renderer then stretched those few images over the whole audio and the
# visuals desynced from the voice. These tests pin that down.
# --------------------------------------------------------------------------- #

_NARRATION_SENTENCES = [
    "Você enviou o orçamento e o cliente nunca mais respondeu.",
    "Vou te mostrar os três erros que causam isso.",
    "Erro um: não detalhar o escopo do serviço.",
    "O cliente fica inseguro porque não sabe o que está incluído.",
    "Erro dois: preço sem nenhuma justificativa clara.",
    "Divida o valor em mão de obra, material e taxa.",
    "Erro três: falta de prazo de validade na proposta.",
    "Defina validade de sete a dez dias e destaque no topo.",
    "Use um modelo de proposta profissional para elevar sua credibilidade.",
    "Segue o canal para mais dicas e compartilha se ajudou.",
]


def _covered_script_kwargs(scene_sentences: list[str], **overrides):
    """Script kwargs whose narration is the full text and scenes cover `scene_sentences`."""
    return _valid_script_kwargs(
        narration=" ".join(_NARRATION_SENTENCES),
        scenes=[
            {
                "id": i,
                "scene_text": sentence,
                "image_prompt": f"prompt {i}",
                "duration_seconds": 5,
            }
            for i, sentence in enumerate(scene_sentences, start=1)
        ],
        **overrides,
    )


def test_scenes_covering_whole_narration_pass():
    script = Script(**_covered_script_kwargs(_NARRATION_SENTENCES))
    assert len(script.scenes) == 10


def test_scenes_covering_only_first_half_of_narration_rejected():
    """The real gpt-oss-120b failure: 6 scenes for a 10-beat narration."""
    with pytest.raises(ValidationError, match="narration"):
        Script(**_covered_script_kwargs(_NARRATION_SENTENCES[:5]))


def test_paraphrased_scene_text_still_passes():
    """Coverage is word-based, not exact-match — light rewording must not fail.

    Models reorder and drop filler words constantly; only wholesale missing
    narration should trip the check.
    """
    reworded = [s.replace("Erro", "Problema").rstrip(".") for s in _NARRATION_SENTENCES]
    script = Script(**_covered_script_kwargs(reworded))
    assert len(script.scenes) == 10


def test_short_synthetic_narration_skips_coverage_check():
    """A handful of words has no meaningful "coverage" — don't police fixtures."""
    script = Script(**_valid_script_kwargs())
    assert script.narration == "Texto da narração."


# --------------------------------------------------------------------------- #
# Unicode punctuation normalization
#
# gpt-oss-120b emits U+2011 NON-BREAKING HYPHEN in pt-BR enclitics
# ("Lembre‑se", "evitá‑los"). Bebas Neue has no glyph for U+2011 or U+2010 —
# only U+002D — so any that reaches the burned captions renders as an empty
# box. Normalize at the contract boundary, where the LLM introduces it.
# --------------------------------------------------------------------------- #

def test_non_breaking_hyphen_normalized_to_ascii():
    script = Script(
        **_valid_script_kwargs(
            title="Lembre\u2011se disso",
            narration="Como evit\u00e1\u2011los e corrigi\u2011los.",
            scenes=[
                {
                    "id": 1,
                    "scene_text": "Lembre\u2011se",
                    "image_prompt": "p",
                    "duration_seconds": 4,
                }
            ],
        )
    )
    assert "\u2011" not in script.title
    assert "\u2011" not in script.narration
    assert "\u2011" not in script.scenes[0].scene_text
    assert script.title == "Lembre-se disso"


def test_non_breaking_space_normalized_to_plain_space():
    script = Script(**_valid_script_kwargs(narration="dois\u00a0mil reais"))
    assert script.narration == "dois mil reais"


def test_en_and_em_dash_are_left_alone():
    """Bebas Neue does have these glyphs, and they're stylistically intended."""
    script = Script(**_valid_script_kwargs(title="Pre\u00e7o \u2014 valor \u2013 lucro"))
    assert script.title == "Pre\u00e7o \u2014 valor \u2013 lucro"
