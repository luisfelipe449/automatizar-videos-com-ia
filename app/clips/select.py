"""Ranking clips and packing them into editions. Pure: no I/O, no database.

Input clips only need the attributes in `RankableClip`, so ORM `Clip` rows
are passed straight in. The caller narrows the pool beforehand (the format's
time window, downloaded clips only); this module never filters on dates.

Rank vs position: `rank` 1 is the best clip. `position` is playback order,
and editions play as a countdown, so rank 1 has the last position.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, Sequence, TypeVar

from app.clips.formats import FormatSpec

# Funny clips that only land if you follow the streamer's speech lose 40% of
# their score: half the audience watches the other language's channel.
_NEEDS_SPEECH_FACTOR = 0.6


class RankableClip(Protocol):
    id: int
    view_count: int
    duration_s: float
    category: str | None
    funny_score: float | None
    hype_score: float | None
    needs_speech: bool | None


C = TypeVar("C", bound=RankableClip)


@dataclass(frozen=True)
class PlannedItem:
    clip_id: int
    rank: int
    position: int
    duration_s: float
    in_short: bool = False
    # Seconds dropped from the clip's start in the Short only; the medium
    # always plays the clip whole.
    trim_start_s: float | None = None


@dataclass(frozen=True)
class EditionPlan:
    format_key: str
    items: tuple[PlannedItem, ...]  # playback order

    @property
    def total_s(self) -> float:
        return sum(item.duration_s for item in self.items)

    @property
    def short(self) -> PlannedItem | None:
        return next((item for item in self.items if item.in_short), None)


@dataclass(frozen=True)
class SkippedEdition:
    format_key: str
    reason: str


def rank_top(clips: Sequence[C]) -> list[C]:
    """Views desc; classification only breaks ties. Every clip qualifies."""
    return sorted(clips, key=lambda c: (-c.view_count, -(c.hype_score or 0.0), c.id))


def _funny_score(clip: RankableClip) -> float:
    score = (clip.funny_score or 0.0) * math.log1p(clip.view_count)
    return score * _NEEDS_SPEECH_FACTOR if clip.needs_speech else score


def rank_funny(clips: Sequence[C]) -> list[C]:
    """`category == "funny"` only, by funny_score x log(1 + views)."""
    funny = [c for c in clips if c.category == "funny"]
    return sorted(funny, key=lambda c: (-_funny_score(c), c.id))


def pack_medium(ranked: Sequence[C], spec: FormatSpec) -> list[C] | None:
    """Best-ranked clips that make a medium of `spec`, in rank order.

    Greedy: walks the ranking and stops as soon as both `min_clips` and
    `min_s` are met. A clip that would push the total past `max_s` is skipped
    and the next one tried, so one long clip cannot sink the edition. Clips
    are never cut in the medium. None when the pool cannot reach the minimums.
    """
    picked: list[C] = []
    total = 0.0
    for clip in ranked:
        if len(picked) >= spec.max_clips:
            break
        if total + clip.duration_s > spec.max_s:
            continue
        picked.append(clip)
        total += clip.duration_s
        if len(picked) >= spec.min_clips and total >= spec.min_s:
            return picked
    return None


def pick_short(packed: Sequence[C], *, budget_s: float) -> tuple[C, float | None]:
    """The rank-1 clip and how much to cut from its start to fit `budget_s`.

    `budget_s` is the room left for the clip in the Short (the Short cap minus
    the 9:16 outro). The start is cut because the payoff of a clip is
    usually at its end. Returns (clip, None) when the clip fits whole.
    """
    if not packed:
        raise ValueError("pick_short needs at least one clip")
    if budget_s <= 0:
        raise ValueError(f"short budget must be positive, got {budget_s}")
    clip = packed[0]
    excess = clip.duration_s - budget_s
    return clip, (excess if excess > 0 else None)


def _plan_one(ranked: Sequence[C], spec: FormatSpec, short_budget_s: float) -> EditionPlan | SkippedEdition:
    packed = pack_medium(ranked, spec)
    if packed is None:
        return SkippedEdition(
            spec.key,
            f"{len(ranked)} candidate(s), need {spec.min_clips}-{spec.max_clips} clips "
            f"totalling {spec.min_s}-{spec.max_s} s",
        )

    short_id, trim = None, None
    if spec.has_short:
        short_clip, trim = pick_short(packed, budget_s=short_budget_s)
        short_id = short_clip.id

    n = len(packed)
    items = tuple(
        PlannedItem(
            clip_id=clip.id,
            rank=rank,
            position=n - rank + 1,
            duration_s=clip.duration_s,
            in_short=clip.id == short_id,
            trim_start_s=trim if clip.id == short_id else None,
        )
        for rank, clip in reversed(list(enumerate(packed, start=1)))
    )
    return EditionPlan(spec.key, items)


def plan_day(
    clips: Sequence[RankableClip],
    specs: Sequence[FormatSpec],
    *,
    short_budget_s: float,
) -> list[EditionPlan | SkippedEdition]:
    """Plan each format in `specs`, in order. One result per spec.

    A format cannot use a clip already taken by an earlier format with the
    same window: Top and Funny of one day never repeat a clip, while a weekly
    may reuse the dailies. That is why Top goes first. `clips` is one pool;
    when windows differ, pass the widest and let the caller narrow per call.
    """
    used: dict[timedelta, set[int]] = {}
    results: list[EditionPlan | SkippedEdition] = []
    for spec in specs:
        taken = used.setdefault(spec.window, set())
        pool = [c for c in clips if c.id not in taken]
        ranked = rank_top(pool) if spec.ranking == "top" else rank_funny(pool)
        result = _plan_one(ranked, spec, short_budget_s)
        if isinstance(result, EditionPlan):
            taken.update(item.clip_id for item in result.items)
        results.append(result)
    return results
