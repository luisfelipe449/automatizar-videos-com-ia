"""Pydantic contracts passed between clips-mode stages."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Source = Literal["twitch", "kick", "youtube"]


class CandidateClip(BaseModel):
    """A clip as reported by a source, before it is stored or downloaded.

    Every source (Twitch API, pasted live links, ...) produces this shape, so
    storage, download and classification never branch on the platform.
    """

    source: Source
    source_clip_id: str
    url: str
    broadcaster_name: str
    # None when the platform gives no reliable channel link (e.g. a Twitch
    # display name that is not the login).
    broadcaster_url: str | None
    title: str = ""
    language: str | None = None
    view_count: int = Field(ge=0)
    duration_s: float = Field(gt=0)
    created_at: datetime
