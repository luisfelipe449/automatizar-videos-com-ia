"""Clip formats: what each episode type holds and when it is due.

A format is channel-independent. Titles, descriptions and publish times
per channel live in the channel profile (`app.clips.profiles`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

# A Short over one minute with any active Content ID claim is blocked
# worldwide; under one minute nothing changes. GTA radio music draws claims,
# so Shorts stay under the line instead of using the 3-minute ceiling.
SHORT_MAX_S = 59

Ranking = Literal["top", "funny"]

# GTA VI's release; no clip of the game exists before it.
GAME_RELEASE_AT = datetime(2026, 11, 18, 0, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))


@dataclass(frozen=True)
class FormatSpec:
    """One episode type.

    `window` is how far back clips are drawn from. `min_s`/`max_s` bound the
    horizontal (16:9) video's clip content, excluding title card and outro.
    `weekday` is None for daily formats, else `date.weekday()` (3 = Thursday).
    No edition is produced before `starts_on`.
    """

    key: str
    ranking: Ranking
    window: timedelta
    min_s: int
    max_s: int
    min_clips: int
    max_clips: int
    has_short: bool
    starts_on: date
    weekday: int | None = None

    def window_for(self, day: date, pool_end: datetime) -> timedelta:
        """How far back from `pool_end` the edition of `day` draws clips.

        The format's `window`, except on its first edition (no earlier edition
        of it is due on or after `starts_on`): that one reaches back to
        `GAME_RELEASE_AT`, so the clips from the release until the regular
        window starts are not left out of every edition. Never shorter than
        `window`.
        """
        period = timedelta(days=7) if self.weekday is not None else timedelta(days=1)
        if day - period >= self.starts_on:
            return self.window
        return max(self.window, pool_end - GAME_RELEASE_AT)

    def is_due(self, day: date) -> bool:
        if day < self.starts_on:
            return False
        return self.weekday is None or day.weekday() == self.weekday


_LAUNCH = date(2026, 11, 19)
_FIRST_FULL_WEEK = date(2026, 11, 26)

FORMATS: dict[str, FormatSpec] = {
    spec.key: spec
    for spec in (
        FormatSpec(
            key="top_daily", ranking="top", window=timedelta(hours=24),
            min_s=180, max_s=300, min_clips=8, max_clips=12,
            has_short=True, starts_on=_LAUNCH,
        ),
        FormatSpec(
            key="funny_daily", ranking="funny", window=timedelta(hours=24),
            min_s=180, max_s=300, min_clips=8, max_clips=12,
            has_short=True, starts_on=_LAUNCH,
        ),
        FormatSpec(
            key="top10_weekly", ranking="top", window=timedelta(days=7),
            min_s=480, max_s=600, min_clips=10, max_clips=10,
            has_short=False, starts_on=_FIRST_FULL_WEEK, weekday=3,
        ),
        FormatSpec(
            key="funny_weekly", ranking="funny", window=timedelta(days=7),
            min_s=480, max_s=600, min_clips=12, max_clips=25,
            has_short=False, starts_on=_FIRST_FULL_WEEK, weekday=4,
        ),
    )
}


def formats_due_on(day: date) -> list[FormatSpec]:
    """Formats with an edition due on `day`, in publishing order."""
    return [spec for spec in FORMATS.values() if spec.is_due(day)]
