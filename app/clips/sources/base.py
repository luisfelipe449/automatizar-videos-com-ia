"""The interface every clip source implements."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.clips.models import CandidateClip


class ClipSource(Protocol):
    name: str

    def fetch(self, since: datetime, until: datetime) -> list[CandidateClip]:
        """Clips created in [since, until), most-viewed first."""
        ...
