"""Episode numbering and per-channel YouTube metadata (app/clips/metadata.py)."""
from __future__ import annotations

from datetime import date

import pytest

from app.clips.metadata import (
    VideoMetadata,
    assign_numbers,
    build_chapters,
    build_metadata,
    format_timestamp,
    next_number,
)
from app.clips.profiles import load_profile
from app.db.database import Base, get_engine, get_session
from app.db.models import Edition

EN = load_profile("five-star-clips")
BR = load_profile("five-star-clips-br")


@pytest.fixture
def session(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    with get_session(eng) as s:
        yield s


def _edition(session, day: int, status: str, numbers: dict, fmt: str = "top_daily") -> Edition:
    ed = Edition(format_key=fmt, edition_date=date(2026, 11, day), status=status)
    ed.numbers = numbers
    session.add(ed)
    session.commit()
    return ed


# --------------------------------------------------------------------------- #
# Numbering
# --------------------------------------------------------------------------- #

def test_first_episode_is_one(session):
    assert next_number(session, EN.key, "top_daily") == 1


def test_next_number_follows_highest_approved(session):
    _edition(session, 19, "approved", {EN.key: 1, BR.key: 1})
    _edition(session, 20, "approved", {EN.key: 2, BR.key: 2})
    assert next_number(session, EN.key, "top_daily") == 3


def test_rejected_and_pending_editions_do_not_burn_a_number(session):
    _edition(session, 19, "approved", {EN.key: 1})
    _edition(session, 20, "rejected", {EN.key: 2})
    _edition(session, 21, "rendered", {EN.key: 2})
    assert next_number(session, EN.key, "top_daily") == 2


def test_numbers_are_per_channel_and_per_format(session):
    _edition(session, 19, "approved", {EN.key: 5, BR.key: 2})
    _edition(session, 19, "approved", {EN.key: 9}, fmt="funny_daily")
    assert next_number(session, EN.key, "top_daily") == 6
    assert next_number(session, BR.key, "top_daily") == 3
    assert next_number(session, BR.key, "funny_daily") == 1


def test_assign_numbers_freezes_once(session):
    _edition(session, 19, "approved", {EN.key: 4, BR.key: 7})
    ed = _edition(session, 20, "planned", {})
    assert assign_numbers(session, ed, [EN, BR]) == {EN.key: 5, BR.key: 8}
    # Another approval later does not move an already frozen number.
    _edition(session, 18, "approved", {EN.key: 40})
    assert assign_numbers(session, ed, [EN, BR]) == {EN.key: 5, BR.key: 8}
    assert session.get(Edition, ed.id).numbers == {EN.key: 5, BR.key: 8}


def test_assign_numbers_fills_a_channel_added_later(session):
    ed = _edition(session, 20, "planned", {EN.key: 3})
    assert assign_numbers(session, ed, [EN, BR]) == {EN.key: 3, BR.key: 1}


# --------------------------------------------------------------------------- #
# Chapters
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("seconds, expected", [
    (0, "0:00"), (9.9, "0:09"), (65, "1:05"), (600, "10:00"), (3725, "1:02:05"),
])
def test_format_timestamp(seconds, expected):
    assert format_timestamp(seconds) == expected


def test_chapters_start_at_zero_and_shift_by_title_card():
    # Countdown of 4 clips, 30 s each, 0.3 s crossfades, 1.5 s title card.
    offsets = [0.0, 29.7, 59.4, 89.1]
    ranks = [4, 3, 2, 1]
    assert build_chapters(offsets, ranks, lead_in_s=1.5) == [
        "0:00 #4", "0:31 #3", "1:00 #2", "1:30 #1",
    ]


def test_clip_under_ten_seconds_joins_the_previous_chapter():
    # #3 lasts 6 s, so it has no marker and plays inside "#4"; #2 keeps its
    # own. YouTube disables every chapter when any one is under 10 s.
    offsets = [0.0, 30.0, 36.0, 70.0]
    assert build_chapters(offsets, [4, 3, 2, 1], lead_in_s=1.5) == [
        "0:00 #4", "0:37 #2", "1:11 #1",
    ]


def test_short_clip_does_not_take_the_next_clips_chapter():
    # Real Top edition: #7 lasts 9.9 s and sits between #8 and #6 (35 s).
    offsets = [0.0, 21.767, 31.367, 66.167, 93.835, 149.302, 209.012, 235.645]
    ranks = [8, 7, 6, 5, 4, 3, 2, 1]
    assert build_chapters(offsets, ranks, lead_in_s=1.5) == [
        "0:00 #8", "0:32 #6", "1:07 #5", "1:35 #4", "2:30 #3", "3:30 #2", "3:57 #1",
    ]


def test_short_first_clip_shares_the_opening_chapter():
    # The first chapter must start at 0:00, so a short first clip keeps it and
    # the next clip's marker is the one dropped.
    offsets = [0.0, 5.0, 40.0, 80.0]
    assert build_chapters(offsets, [4, 3, 2, 1], lead_in_s=1.5) == [
        "0:00 #4", "0:41 #2", "1:21 #1",
    ]


def test_fewer_than_three_chapters_means_none():
    assert build_chapters([0.0, 30.0], [2, 1], lead_in_s=1.5) == []


# --------------------------------------------------------------------------- #
# Metadata snapshots
# --------------------------------------------------------------------------- #

CHAPTERS = ["0:00 #3", "0:31 #2", "1:00 #1"]


def test_medium_metadata_en():
    meta = build_metadata(EN, "top_daily", 12, chapters=CHAPTERS)
    assert meta == VideoMetadata(
        title="GTA 6 Top Clips of the Day #12",
        description=(
            "Every clip belongs to the streamer credited on screen.\n"
            "\n"
            "0:00 #3\n"
            "0:31 #2\n"
            "1:00 #1\n"
            "\n"
            "#GTA6 #GTAVI"
        ),
        tags=EN.tags,
        default_language="en",
        category_id="20",
    )


def test_medium_metadata_pt_br():
    meta = build_metadata(BR, "funny_daily", 3, chapters=CHAPTERS)
    assert meta.title == "GTA 6 Momentos Engraçados #3"
    assert meta.description == (
        "Cada clipe pertence ao streamer creditado na tela.\n"
        "\n"
        "0:00 #3\n"
        "0:31 #2\n"
        "1:00 #1\n"
        "\n"
        "#GTA6 #GTAVI"
    )
    assert meta.default_language == "pt-BR"
    assert meta.tags == BR.tags


def test_short_metadata_has_no_chapters():
    meta = build_metadata(EN, "top_daily", 12)
    assert meta.title == "GTA 6 Top Clips of the Day #12"
    assert meta.description == "Every clip belongs to the streamer credited on screen.\n\n#GTA6 #GTAVI"


def test_metadata_is_a_youtube_snippet():
    snippet = build_metadata(BR, "top_daily", 1).snippet()
    assert snippet == {
        "title": "GTA 6 Melhores Clipes do Dia #1",
        "description": "Cada clipe pertence ao streamer creditado na tela.\n\n#GTA6 #GTAVI",
        "tags": BR.tags,
        "categoryId": "20",
        "defaultLanguage": "pt-BR",
    }
