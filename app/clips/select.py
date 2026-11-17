"""Ranking clips and packing them into editions. Pure: no I/O, no database.

Input clips only need the attributes in `RankableClip`, so ORM `Clip` rows
are passed straight in. The caller narrows the pool beforehand (the format's
time window, downloaded clips only); this module never filters on dates.

Rank vs position: `rank` 1 is the best clip. `position` is playback order,
and editions play as a countdown, so rank 1 has the last position.

Language preference: both rankings multiply a clip's score by
`language_factor`, 1.0 for the channels' languages (and for a clip whose
language is unknown) and `CLIPS_OTHER_LANGUAGE_FACTOR` for the rest. The
`preferred`/`other_factor` parameters default to the config at call time.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, Sequence, TypeVar

from app import config
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
    language: str | None


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


def language_factor(language: str | None, *, preferred: Sequence[str], other_factor: float) -> float:
    """1.0 for a preferred or unknown language, else `other_factor`.

    Matches on the primary subtag, so "pt-br" counts as "pt".
    """
    if not language:
        return 1.0
    primary = language.strip().lower().split("-", 1)[0]
    return 1.0 if primary in preferred else other_factor


def _language_args(preferred: Sequence[str] | None, other_factor: float | None) -> tuple[Sequence[str], float]:
    return (
        config.CLIPS_PREFERRED_LANGUAGES if preferred is None else preferred,
        config.CLIPS_OTHER_LANGUAGE_FACTOR if other_factor is None else other_factor,
    )


def rank_top(
    clips: Sequence[C], *, preferred: Sequence[str] | None = None, other_factor: float | None = None,
) -> list[C]:
    """Views x language factor, desc; classification only breaks ties. Every clip qualifies."""
    preferred, other_factor = _language_args(preferred, other_factor)

    def key(c: RankableClip):
        weighted = c.view_count * language_factor(c.language, preferred=preferred, other_factor=other_factor)
        return (-weighted, -(c.hype_score or 0.0), c.id)

    return sorted(clips, key=key)


def _funny_score(clip: RankableClip, preferred: Sequence[str], other_factor: float) -> float:
    score = (clip.funny_score or 0.0) * math.log1p(clip.view_count)
    if clip.needs_speech:
        score *= _NEEDS_SPEECH_FACTOR
    return score * language_factor(clip.language, preferred=preferred, other_factor=other_factor)


def rank_funny(
    clips: Sequence[C], *, preferred: Sequence[str] | None = None, other_factor: float | None = None,
) -> list[C]:
    """`category == "funny"` only, by funny_score x log(1 + views) x speech and language factors."""
    preferred, other_factor = _language_args(preferred, other_factor)
    funny = [c for c in clips if c.category == "funny"]
    return sorted(funny, key=lambda c: (-_funny_score(c, preferred, other_factor), c.id))


def pack_medium(ranked: Sequence[C], spec: FormatSpec) -> list[C] | None:
    """Best-ranked clips that make a medium of `spec`, in rank order.

    Walks the ranking and stops as soon as both `min_clips` and `min_s` are
    met. A clip is skipped (and the next one tried) when taking it would leave
    no room under `max_s` for the clips still needed to reach `min_clips`,
    counted at the shortest durations still ahead in the ranking. So long
    top clips are taken while they fit, and never at the cost of the edition.
    Clips are never cut in the medium. None when the pool cannot reach the
    minimums.
    """
    picked: list[C] = []
    total = 0.0
    for i, clip in enumerate(ranked):
        if len(picked) >= spec.max_clips:
            break
        still_needed = max(0, spec.min_clips - len(picked) - 1)
        ahead = sorted(c.duration_s for c in ranked[i + 1:])[:still_needed]
        reserve = sum(ahead) if len(ahead) == still_needed else 0.0
        if total + clip.duration_s + reserve > spec.max_s:
            continue
        picked.append(clip)
        total += clip.duration_s
        if len(picked) >= spec.min_clips and total >= spec.min_s:
            return picked
    return None


def pick_short(packed: Sequence[C], *, budget_s: float) -> tuple[C, float | None]:
    """The rank-1 clip and how much to cut from its start to fit `budget_s`.

    `budget_s` is the room left for the clip in the Short (the Short cap minus
    the 9:16 outro and a safety margin). The start is cut because the payoff of a clip is
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
    preferred: Sequence[str] | None = None,
    other_factor: float | None = None,
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
        rank = rank_top if spec.ranking == "top" else rank_funny
        ranked = rank(pool, preferred=preferred, other_factor=other_factor)
        result = _plan_one(ranked, spec, short_budget_s)
        if isinstance(result, EditionPlan):
            taken.update(item.clip_id for item in result.items)
        results.append(result)
    return results
