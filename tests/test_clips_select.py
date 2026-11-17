"""Clip ranking and edition packing (app/clips/select.py)."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

import pytest

from app.clips.formats import FORMATS, FormatSpec
from app.clips.select import (
    EditionPlan,
    SkippedEdition,
    language_factor,
    pack_medium,
    pick_short,
    plan_day,
    rank_funny,
    rank_top,
)

TOP = FORMATS["top_daily"]
FUNNY = FORMATS["funny_daily"]
BUDGET = 52.0


@dataclass
class FakeClip:
    id: int
    view_count: int = 100
    duration_s: float = 30.0
    category: str | None = "play"
    funny_score: float | None = 5.0
    hype_score: float | None = 5.0
    needs_speech: bool | None = False
    language: str | None = "en"


def _clips(n: int, start_id: int = 1, **kw) -> list[FakeClip]:
    return [FakeClip(id=start_id + i, view_count=1000 - i, **kw) for i in range(n)]


def _ids(clips) -> list[int]:
    return [c.id for c in clips]


# --------------------------------------------------------------------------- #
# rank_top
# --------------------------------------------------------------------------- #

def test_rank_top_orders_by_views_desc():
    clips = [FakeClip(1, view_count=10), FakeClip(2, view_count=300), FakeClip(3, view_count=50)]
    assert _ids(rank_top(clips)) == [2, 3, 1]


def test_rank_top_breaks_view_ties_by_hype():
    clips = [
        FakeClip(1, view_count=100, hype_score=2),
        FakeClip(2, view_count=100, hype_score=9),
        FakeClip(3, view_count=100, hype_score=None),
    ]
    assert _ids(rank_top(clips)) == [2, 1, 3]


def test_rank_top_full_tie_is_deterministic_by_id():
    clips = [FakeClip(7), FakeClip(3), FakeClip(5)]
    assert _ids(rank_top(clips)) == [3, 5, 7]


def test_rank_top_keeps_unclassified_and_speech_clips():
    clips = [FakeClip(1, category=None, hype_score=None, needs_speech=None),
             FakeClip(2, needs_speech=True)]
    assert set(_ids(rank_top(clips))) == {1, 2}


# --------------------------------------------------------------------------- #
# rank_funny
# --------------------------------------------------------------------------- #

def test_rank_funny_keeps_only_funny_category():
    clips = [FakeClip(1, category="funny"), FakeClip(2, category="fail"), FakeClip(3, category=None)]
    assert _ids(rank_funny(clips)) == [1]


def test_rank_funny_scores_funny_times_log_views():
    # 9 * log(11) ≈ 21.6  vs  5 * log(10001) ≈ 46.1
    clips = [
        FakeClip(1, category="funny", funny_score=9, view_count=10),
        FakeClip(2, category="funny", funny_score=5, view_count=10_000),
    ]
    assert _ids(rank_funny(clips)) == [2, 1]


def test_rank_funny_penalises_needs_speech():
    # Same raw score; the speech-dependent one drops to 0.6x.
    clips = [
        FakeClip(1, category="funny", funny_score=8, needs_speech=True),
        FakeClip(2, category="funny", funny_score=8, needs_speech=False),
    ]
    assert _ids(rank_funny(clips)) == [2, 1]
    # ...but a speech clip that is clearly funnier still wins.
    clips = [
        FakeClip(1, category="funny", funny_score=10, needs_speech=True),  # 6.0 * log
        FakeClip(2, category="funny", funny_score=5, needs_speech=False),  # 5.0 * log
    ]
    assert _ids(rank_funny(clips)) == [1, 2]


def test_rank_funny_all_needs_speech_still_ranks():
    clips = [FakeClip(i, category="funny", funny_score=i, needs_speech=True) for i in (1, 2, 3)]
    assert _ids(rank_funny(clips)) == [3, 2, 1]


def test_rank_funny_ties_break_by_id():
    clips = [FakeClip(9, category="funny"), FakeClip(4, category="funny")]
    assert _ids(rank_funny(clips)) == [4, 9]


def test_rank_funny_no_candidates():
    assert rank_funny([FakeClip(1, category="play")]) == []
    assert rank_funny([]) == []


# --------------------------------------------------------------------------- #
# pack_medium
# --------------------------------------------------------------------------- #

def test_pack_medium_stops_once_both_minimums_met():
    # 8 x 30 s = 240 s: inside 180-300 with the minimum clip count.
    packed = pack_medium(_clips(15), TOP)
    assert _ids(packed) == list(range(1, 9))


def test_pack_medium_keeps_adding_until_min_duration():
    # 8 x 20 s = 160 s < 180: needs a 9th clip.
    packed = pack_medium(_clips(15, duration_s=20.0), TOP)
    assert len(packed) == 9
    assert sum(c.duration_s for c in packed) >= TOP.min_s


def test_pack_medium_skips_clip_that_would_overflow_max():
    spec = replace(TOP, min_s=100, max_s=120, min_clips=3, max_clips=12)
    clips = [FakeClip(1, duration_s=50), FakeClip(2, duration_s=50),
             FakeClip(3, duration_s=40), FakeClip(4, duration_s=10)]
    # 50+50 = 100, +40 = 140 > 120 -> skip #3, take #4 -> 110.
    assert _ids(pack_medium(clips, spec)) == [1, 2, 4]


def test_pack_medium_skips_clip_longer_than_60s_if_it_overflows():
    spec = replace(TOP, min_s=60, max_s=70, min_clips=2, max_clips=12)
    clips = [FakeClip(1, duration_s=75), FakeClip(2, duration_s=30), FakeClip(3, duration_s=40)]
    assert _ids(pack_medium(clips, spec)) == [2, 3]


def test_pack_medium_never_exceeds_max_clips():
    spec = replace(TOP, min_s=180, max_s=300, min_clips=2, max_clips=3)
    assert pack_medium(_clips(10, duration_s=30.0), spec) is None


def test_pack_medium_returns_none_when_pool_too_small():
    assert pack_medium(_clips(7), TOP) is None
    assert pack_medium([], TOP) is None


def test_pack_medium_returns_rank_order():
    packed = pack_medium(_clips(10), TOP)
    views = [c.view_count for c in packed]
    assert views == sorted(views, reverse=True)


# --------------------------------------------------------------------------- #
# pick_short
# --------------------------------------------------------------------------- #

def test_pick_short_is_rank_one_untrimmed_when_it_fits():
    clip, trim = pick_short([FakeClip(1, duration_s=40), FakeClip(2)], budget_s=BUDGET)
    assert clip.id == 1
    assert trim is None


def test_pick_short_trims_the_start_of_a_long_clip():
    clip, trim = pick_short([FakeClip(1, duration_s=60.0)], budget_s=BUDGET)
    assert clip.id == 1
    assert trim == pytest.approx(8.0)


def test_pick_short_trims_clip_longer_than_60s():
    _, trim = pick_short([FakeClip(1, duration_s=95.0)], budget_s=BUDGET)
    assert trim == pytest.approx(43.0)


def test_pick_short_exact_fit_is_not_trimmed():
    _, trim = pick_short([FakeClip(1, duration_s=BUDGET)], budget_s=BUDGET)
    assert trim is None


def test_pick_short_rejects_bad_input():
    with pytest.raises(ValueError):
        pick_short([], budget_s=BUDGET)
    with pytest.raises(ValueError):
        pick_short([FakeClip(1)], budget_s=0)


# --------------------------------------------------------------------------- #
# plan_day
# --------------------------------------------------------------------------- #

def _pool() -> list[FakeClip]:
    # 12 popular non-funny clips + 10 funny ones with fewer views.
    top = _clips(12, start_id=1, category="play")
    funny = [FakeClip(id=100 + i, view_count=500 - i, category="funny", funny_score=7)
             for i in range(10)]
    return top + funny


def test_plan_day_builds_both_editions_in_countdown_order():
    result = plan_day(_pool(), [TOP, FUNNY], short_budget_s=BUDGET)
    top, funny = result
    assert isinstance(top, EditionPlan) and isinstance(funny, EditionPlan)
    assert top.format_key == "top_daily"

    # Playback: #8 first, #1 last.
    assert [i.rank for i in top.items] == list(range(8, 0, -1))
    assert [i.position for i in top.items] == list(range(1, 9))
    assert top.items[-1].clip_id == 1

    # The Short is the #1 clip and nothing else.
    shorts = [i for i in top.items if i.in_short]
    assert len(shorts) == 1 and shorts[0].rank == 1
    assert top.total_s == pytest.approx(240.0)


def test_plan_day_funny_excludes_top_clips():
    # Top would take the most-viewed clips, which here are also funny.
    pool = [FakeClip(id=i, view_count=10_000 - i, category="funny", funny_score=9)
            for i in range(1, 17)]
    top, funny = plan_day(pool, [TOP, FUNNY], short_budget_s=BUDGET)
    top_ids = {i.clip_id for i in top.items}
    funny_ids = {i.clip_id for i in funny.items}
    assert top_ids and funny_ids
    assert not top_ids & funny_ids


def test_plan_day_funny_without_candidates_is_skipped_with_reason():
    pool = _clips(12, category="play")
    top, funny = plan_day(pool, [TOP, FUNNY], short_budget_s=BUDGET)
    assert isinstance(top, EditionPlan)
    assert isinstance(funny, SkippedEdition)
    assert funny.format_key == "funny_daily"
    assert "0 candidate" in funny.reason


def test_plan_day_funny_skipped_when_top_ate_the_pool():
    pool = [FakeClip(id=i, view_count=1000 - i, category="funny") for i in range(1, 11)]
    top, funny = plan_day(pool, [TOP, FUNNY], short_budget_s=BUDGET)
    assert isinstance(top, EditionPlan)
    assert isinstance(funny, SkippedEdition)
    assert "2 candidate" in funny.reason


def test_plan_day_empty_pool_skips_everything():
    result = plan_day([], [TOP, FUNNY], short_budget_s=BUDGET)
    assert all(isinstance(r, SkippedEdition) for r in result)
    assert [r.format_key for r in result] == ["top_daily", "funny_daily"]


def test_plan_day_short_trim_follows_budget():
    pool = _clips(12)
    pool[0].duration_s = 58.0
    top, = plan_day(pool, [TOP], short_budget_s=BUDGET)
    short = next(i for i in top.items if i.in_short)
    assert short.clip_id == 1
    assert short.trim_start_s == pytest.approx(6.0)
    assert all(i.trim_start_s is None for i in top.items if not i.in_short)


def test_plan_day_format_without_short_marks_none():
    weekly = FORMATS["top10_weekly"]
    pool = _clips(20, duration_s=55.0)
    plan, = plan_day(pool, [weekly], short_budget_s=BUDGET)
    assert isinstance(plan, EditionPlan)
    assert len(plan.items) == 10
    assert not any(i.in_short for i in plan.items)


def test_plan_day_formats_with_different_windows_may_share_clips():
    # Weeklies may reuse clips from the dailies of the same day.
    weekly = replace(FORMATS["top10_weekly"], min_s=180, max_s=600)
    top, week = plan_day(_clips(20), [TOP, weekly], short_budget_s=BUDGET)
    assert {i.clip_id for i in top.items} <= {i.clip_id for i in week.items}


def test_plan_day_all_needs_speech_funny_still_planned():
    pool = _clips(12, category="play") + [
        FakeClip(id=100 + i, category="funny", needs_speech=True) for i in range(9)
    ]
    _, funny = plan_day(pool, [TOP, FUNNY], short_budget_s=BUDGET)
    assert isinstance(funny, EditionPlan)


def test_edition_plan_rank_one_clip_helper():
    top, = plan_day(_clips(10), [TOP], short_budget_s=BUDGET)
    assert top.short is not None and top.short.rank == 1
    assert math.isclose(top.total_s, sum(30.0 for _ in top.items))


# --------------------------------------------------------------------------- #
# Language preference
# --------------------------------------------------------------------------- #

PREFERRED = ("en", "pt")


@pytest.mark.parametrize("language, expected", [
    ("en", 1.0), ("pt", 1.0), ("pt-br", 1.0), ("EN-GB", 1.0),
    ("es", 0.5), ("ja", 0.5), ("other", 0.5),
    (None, 1.0), ("", 1.0),
])
def test_language_factor(language, expected):
    assert language_factor(language, preferred=PREFERRED, other_factor=0.5) == expected


def test_rank_top_prefers_en_and_pt():
    clips = [
        FakeClip(1, view_count=1000, language="es"),  # competes as 500
        FakeClip(2, view_count=600, language="en"),
        FakeClip(3, view_count=550, language="pt"),
        FakeClip(4, view_count=400, language="ja"),   # competes as 200
    ]
    ranked = rank_top(clips, preferred=PREFERRED, other_factor=0.5)
    assert [c.id for c in ranked] == [2, 3, 1, 4]


def test_rank_top_other_language_still_wins_with_enough_views():
    clips = [FakeClip(1, view_count=2000, language="es"), FakeClip(2, view_count=900, language="en")]
    assert [c.id for c in rank_top(clips, preferred=PREFERRED, other_factor=0.5)] == [1, 2]


def test_rank_top_factor_one_means_views_only():
    clips = [FakeClip(1, view_count=1000, language="es"), FakeClip(2, view_count=600, language="en")]
    assert [c.id for c in rank_top(clips, preferred=PREFERRED, other_factor=1.0)] == [1, 2]


def test_rank_funny_applies_language_factor_to_the_whole_score():
    # Same funny score; 10x the views does not make up for half the score.
    clips = [
        FakeClip(1, category="funny", funny_score=8, view_count=10_000, language="es"),
        FakeClip(2, category="funny", funny_score=8, view_count=1_000, language="pt"),
    ]
    ranked = rank_funny(clips, preferred=PREFERRED, other_factor=0.5)
    assert [c.id for c in ranked] == [2, 1]


def test_rankings_default_to_configured_preference(monkeypatch):
    clips = [FakeClip(1, view_count=1000, language="es"), FakeClip(2, view_count=600, language="en")]
    monkeypatch.setattr("app.config.CLIPS_PREFERRED_LANGUAGES", ("en", "pt"))
    monkeypatch.setattr("app.config.CLIPS_OTHER_LANGUAGE_FACTOR", 0.5)
    assert [c.id for c in rank_top(clips)] == [2, 1]
    monkeypatch.setattr("app.config.CLIPS_OTHER_LANGUAGE_FACTOR", 1.0)
    assert [c.id for c in rank_top(clips)] == [1, 2]


def test_plan_day_prefers_en_and_pt_clips():
    spanish = [FakeClip(id=i, view_count=5000 - i, language="es") for i in range(1, 9)]
    english = [FakeClip(id=100 + i, view_count=3000 - i, language="en") for i in range(8)]
    top, = plan_day(spanish + english, [TOP], short_budget_s=BUDGET,
                    preferred=PREFERRED, other_factor=0.5)
    assert {i.clip_id for i in top.items} == {c.id for c in english}


def test_pack_medium_leaves_room_for_the_minimum_clip_count():
    # Best-ranked clips are long: taking them all reaches 300 s with only 6
    # clips and nothing left fits. The packer must keep room for 8.
    long = [FakeClip(id=i, view_count=1000 - i, duration_s=50.0) for i in range(1, 7)]
    short = [FakeClip(id=100 + i, view_count=500 - i, duration_s=10.0) for i in range(10)]
    packed = pack_medium(long + short, TOP)
    assert packed is not None
    assert len(packed) >= TOP.min_clips
    assert TOP.min_s <= sum(c.duration_s for c in packed) <= TOP.max_s
    # Still rank-first: as many of the long, best clips as the room allows.
    assert [c.id for c in packed][:5] == [1, 2, 3, 4, 5]


def test_pack_medium_lookahead_keeps_rank_order_when_room_is_ample():
    packed = pack_medium(_clips(15), TOP)
    assert [c.id for c in packed] == list(range(1, 9))
