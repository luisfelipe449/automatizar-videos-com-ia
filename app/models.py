"""Pydantic schemas that form the contract between pipeline stages."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import MAX_DURATION_SECONDS, TARGET_HEIGHT, TARGET_WIDTH

# --- narration coverage (see Script._validate_scenes_cover_narration) ------- #

# Below this many narration words, coverage is noise rather than signal.
# Real scripts run 80-150 words; only synthetic fixtures land under this.
_COVERAGE_MIN_WORDS = 30
# 0.8 tolerates rewording and dropped filler but catches a whole missing half.
_COVERAGE_MIN_RATIO = 0.8
# Unicode-aware so Portuguese accents count as word characters.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _significant_words(text: str) -> list[str]:
    """Lowercased word tokens, punctuation stripped, for coverage comparison."""
    return _WORD_RE.findall(text.lower())


# --- unicode punctuation normalization -------------------------------------- #

# Characters LLMs emit that the caption font (Bebas Neue) has no glyph for, so
# they'd burn into the video as empty boxes. En/em dashes are deliberately NOT
# here — the font has those and they're stylistically fine.
_PUNCT_FIXES = {
    "‑": "-",  # non-breaking hyphen — gpt-oss uses it in "Lembre‑se"
    "‐": "-",  # plain unicode hyphen
    " ": " ",  # non-breaking space
    " ": " ",  # narrow no-break space
    " ": " ",  # thin space
}
_PUNCT_TABLE = str.maketrans(_PUNCT_FIXES)


def _normalize_punctuation(text: str) -> str:
    """Replace unrenderable unicode punctuation with ASCII equivalents."""
    return text.translate(_PUNCT_TABLE)


class Scene(BaseModel):
    """One visual beat: short narrated text + image prompt + on-screen duration."""

    model_config = {"str_strip_whitespace": True}

    id: int = Field(ge=1)
    scene_text: str = Field(min_length=1)
    image_prompt: str = Field(min_length=1)
    # A single scene that runs longer than 30s is almost certainly a mistake.
    duration_seconds: int = Field(ge=1, le=30)
    # Optional ranking overlay: when the topic is a Top-N / ranking video, the
    # LLM fills in `rank` + `rank_title` on the fact scenes (not on the hook,
    # intro, CTA, or closing). `render_service` then draws "10 - Pelé" at the
    # top of the scene. Both fields must be set together or both null — see
    # `_validate_rank_pair`. `rank_title` is ≤40 chars to keep the overlay
    # readable on a 1080px wide Short.
    rank: int | None = Field(default=None, ge=1, le=100)
    rank_title: str | None = Field(default=None, min_length=1, max_length=40)

    @field_validator("scene_text", "rank_title", mode="before")
    @classmethod
    def _normalize_text(cls, value):
        return _normalize_punctuation(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_rank_pair(self) -> "Scene":
        if (self.rank is None) != (self.rank_title is None):
            raise ValueError("rank and rank_title must both be set or both be null")
        return self


class Script(BaseModel):
    """Full LLM output: metadata + narration + ordered scenes. Validated contract."""

    model_config = {"str_strip_whitespace": True}

    # Used as a filesystem directory name — keep it safe.
    job_id: str = Field(min_length=1, pattern=r"^[0-9A-Za-z_\-]+$")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=15)
    narration: str = Field(min_length=1)
    scenes: list[Scene] = Field(min_length=1)

    @field_validator("title", "description", "narration", mode="before")
    @classmethod
    def _normalize_text(cls, value):
        return _normalize_punctuation(value) if isinstance(value, str) else value

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, tags: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in tags:
            stripped = tag.strip()
            if not (1 <= len(stripped) <= 30):
                raise ValueError(
                    f"Each tag must be 1-30 chars after stripping; got {stripped!r}"
                )
            cleaned.append(stripped)
        return cleaned

    @field_validator("description")
    @classmethod
    def _ensure_shorts_hashtag(cls, description: str) -> str:
        # Shorts compliance: normalize by appending #Shorts if missing.
        if "#shorts" in description.lower():
            return description
        return f"{description} #Shorts"

    @field_validator("scenes")
    @classmethod
    def _validate_scene_ids(cls, scenes: list[Scene]) -> list[Scene]:
        expected = list(range(1, len(scenes) + 1))
        actual = [s.id for s in scenes]
        if actual != expected:
            raise ValueError(
                f"Scene ids must be a contiguous sequence starting at 1; "
                f"got {actual}, expected {expected}"
            )
        return scenes

    @model_validator(mode="after")
    def _validate_scenes_cover_narration(self) -> "Script":
        """Reject scripts whose scenes only cover part of the narration.

        `scene_text` is contractually the line of narration a scene covers, so
        the scenes together should span the whole thing. Models sometimes stop
        emitting scenes halfway through — the render still succeeds (durations
        are rescaled to the real audio length) but the surviving images get
        stretched over the uncovered half, so the visuals drift out of sync
        with the voice. `generate_script` feeds ValidationErrors back to the
        model, so failing here buys a self-correcting retry.

        Word-set based rather than exact-match: models reword and drop filler
        constantly, and only wholesale-missing narration is worth failing on.
        """
        narration_words = _significant_words(self.narration)
        # Coverage is meaningless for a handful of words (and test fixtures
        # legitimately use throwaway narration), so only police real scripts.
        if len(narration_words) < _COVERAGE_MIN_WORDS:
            return self

        covered = set().union(
            *(_significant_words(s.scene_text) for s in self.scenes)
        )
        hits = sum(1 for w in narration_words if w in covered)
        ratio = hits / len(narration_words)
        if ratio < _COVERAGE_MIN_RATIO:
            raise ValueError(
                f"Scenes cover only {ratio:.0%} of the narration "
                f"(minimum {_COVERAGE_MIN_RATIO:.0%}). Every part of the "
                f"narration needs a scene: split the remaining narration into "
                f"more scenes so the visuals track the whole voiceover."
            )
        return self

    @model_validator(mode="after")
    def _validate_total_duration(self) -> "Script":
        total = sum(s.duration_seconds for s in self.scenes)
        if total > MAX_DURATION_SECONDS:
            raise ValueError(
                f"Total scene duration {total}s exceeds the "
                f"{MAX_DURATION_SECONDS}s budget for a Short."
            )
        return self


class Job(BaseModel):
    """Runtime container: the original prompt plus the (optional) generated Script."""

    job_id: str
    prompt: str
    script: Script | None = None


class RenderSpec(BaseModel):
    """Declarative input to `render_service`: every path FFmpeg needs.

    `scene_paths` can be a mix of `.png` (image scenes, rendered with
    zoompan Ken Burns) and `.mp4` (video scenes from fal.ai LTX Video,
    rendered with scale+crop+freeze-pad). render_service dispatches based
    on the file extension per scene.
    """

    job_id: str
    scenes: list[Scene]
    # One per scene, in scene order. `.png` -> image path, `.mp4` -> video
    # clip. Renamed from `image_paths` when video-per-scene mode landed.
    scene_paths: list[Path]
    narration_path: Path
    captions_path: Path
    output_path: Path
    width: int = TARGET_WIDTH
    height: int = TARGET_HEIGHT

    @model_validator(mode="after")
    def _validate_scene_count(self) -> "RenderSpec":
        if len(self.scene_paths) != len(self.scenes):
            raise ValueError(
                f"scene_paths length ({len(self.scene_paths)}) must match "
                f"scenes length ({len(self.scenes)})."
            )
        return self
