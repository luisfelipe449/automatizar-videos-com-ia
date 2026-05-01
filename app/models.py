"""Pydantic schemas that form the contract between pipeline stages."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import MAX_DURATION_SECONDS, TARGET_HEIGHT, TARGET_WIDTH


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
