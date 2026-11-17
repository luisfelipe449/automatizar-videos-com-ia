"""Episode numbers and the YouTube metadata of each channel's videos.

Numbers count per (channel, format) and are stored in `Edition.numbers`
keyed by the channel profile key. Only approved editions count, so a
rejected edition does not leave a gap. The number is frozen on the edition
before render because it is drawn into the video.

Pitfall: two editions of one format rendered before either is approved get
the same number. The daily batch is approved before the next one renders;
if that slips, re-render the later edition after approving the earlier one
(clear its `numbers` first).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy.orm import Session

from app.clips.profiles import ChannelProfile
from app.db.models import Edition

# YouTube category "Gaming".
_CATEGORY_GAMING = "20"
# YouTube shows chapters only when there are at least 3, the first starts at
# 0:00 and every one lasts at least 10 s; otherwise it shows none.
_MIN_CHAPTERS = 3
_MIN_CHAPTER_S = 10.0


def next_number(session: Session, profile_key: str, format_key: str) -> int:
    approved = session.query(Edition).filter_by(format_key=format_key, status="approved")
    numbers = [ed.numbers.get(profile_key) for ed in approved]
    return max((n for n in numbers if n is not None), default=0) + 1


def assign_numbers(
    session: Session, edition: Edition, profiles: Sequence[ChannelProfile],
) -> dict[str, int]:
    """Freeze an episode number per channel on `edition`; existing ones stay."""
    numbers = dict(edition.numbers)
    for profile in profiles:
        if profile.key not in numbers:
            numbers[profile.key] = next_number(session, profile.key, edition.format_key)
    if numbers != edition.numbers:
        edition.numbers = numbers
        session.commit()
    return numbers


def format_timestamp(seconds: float) -> str:
    total = int(math.floor(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def build_chapters(offsets: Sequence[float], ranks: Sequence[int], *, lead_in_s: float) -> list[str]:
    """Chapter lines ("1:00 #2") for a medium, or [] when YouTube would drop them.

    `offsets` are clip starts in the base video and `lead_in_s` is what plays
    before it (the title card). The title card joins the first chapter, which
    YouTube requires at 0:00. A clip starting under 10 s after the last kept
    chapter gets no marker of its own.
    """
    lines: list[str] = []
    last_start = None
    for offset, rank in zip(offsets, ranks):
        start = 0.0 if last_start is None else offset + lead_in_s
        if last_start is not None and start - last_start < _MIN_CHAPTER_S:
            continue
        lines.append(f"{format_timestamp(start)} #{rank}")
        last_start = start
    return lines if len(lines) >= _MIN_CHAPTERS else []


@dataclass(frozen=True)
class VideoMetadata:
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    default_language: str = "en"
    category_id: str = _CATEGORY_GAMING

    def snippet(self) -> dict:
        """The `snippet` part of a YouTube `videos.insert` body."""
        return {
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "categoryId": self.category_id,
            "defaultLanguage": self.default_language,
        }


def build_metadata(
    profile: ChannelProfile,
    format_key: str,
    number: int,
    *,
    chapters: Sequence[str] = (),
) -> VideoMetadata:
    """Title, description, tags and language of one video on one channel.

    Pass `chapters` for the medium; the Short has none.
    """
    blocks = [profile.descriptions[format_key]]
    if chapters:
        blocks.append("\n".join(chapters))
    if profile.hashtags:
        blocks.append(" ".join(profile.hashtags))
    return VideoMetadata(
        title=profile.title_for(format_key, number),
        description="\n\n".join(blocks),
        tags=list(profile.tags),
        default_language=profile.language,
    )
