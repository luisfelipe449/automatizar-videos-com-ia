"""Channel profiles (assets/input/channels/*.json) and clip format specs."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.clips.formats import FORMATS, SHORT_MAX_S, formats_due_on
from app.clips.profiles import (
    CHANNELS_DIR,
    ChannelProfile,
    ProfileNotFound,
    load_profile,
)

BRT = ZoneInfo("America/Sao_Paulo")


def _valid_payload(**overrides) -> dict:
    payload = {
        "display_name": "Test Channel",
        "language": "en",
        "tts_voice": "Kore",
        "titles": {key: f"GTA 6 {key} #{{n}}" for key in FORMATS},
        "descriptions": {key: f"About {key}." for key in FORMATS},
        "schedule": {
            key: {"time": "15:00", "timezone": "America/New_York"} for key in FORMATS
        },
        "outro_cta": "Subscribe for tomorrow's best moments!",
        "hashtags": ["#GTA6", "#GTAVI"],
        "tags": ["gta 6", "gta vi"],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Shipped profiles
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("key, language", [
    ("five-star-clips", "en"),
    ("five-star-clips-br", "pt-BR"),
])
def test_shipped_profiles_load(key, language):
    profile = load_profile(key)
    assert profile.key == key
    assert profile.language == language
    assert set(profile.titles) == set(FORMATS)


def test_shipped_titles_match_agreed_wording():
    en = load_profile("five-star-clips")
    br = load_profile("five-star-clips-br")
    assert en.title_for("top_daily", 1) == "GTA 6 Top Clips of the Day #1"
    assert en.title_for("funny_daily", 12) == "GTA 6 Funny Moments #12"
    assert br.title_for("top_daily", 1) == "GTA 6 Melhores Clipes do Dia #1"
    assert br.title_for("funny_weekly", 2) == "GTA 6 Momentos Engraçados da Semana #2"


def test_shipped_schedule_targets_school_dismissal():
    """pt-BR posts at 11h/17h BRT; en at 15h ET then 15h PT (17h/20h BRT)."""
    en = load_profile("five-star-clips")
    br = load_profile("five-star-clips-br")
    day = date(2026, 11, 19)

    def brt_hour(profile, fmt):
        return profile.publish_at(fmt, day).astimezone(BRT).hour

    assert brt_hour(br, "top_daily") == 11
    assert brt_hour(br, "funny_daily") == 17
    assert brt_hour(en, "top_daily") == 17
    assert brt_hour(en, "funny_daily") == 20


def test_publish_at_follows_us_daylight_saving():
    """Stored in the audience's zone, so DST shifts the BRT hour, not the local one."""
    en = load_profile("five-star-clips")
    summer = en.publish_at("top_daily", date(2027, 6, 10))
    assert summer.hour == 15
    assert summer.astimezone(BRT).hour == 16


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def test_title_template_must_contain_episode_number():
    payload = _valid_payload()
    payload["titles"]["top_daily"] = "GTA 6 Top Clips of the Day"
    with pytest.raises(ValidationError, match="{n}"):
        ChannelProfile(key="x", **payload)


def test_every_format_needs_title_description_and_schedule():
    payload = _valid_payload()
    del payload["schedule"]["funny_weekly"]
    with pytest.raises(ValidationError, match="funny_weekly"):
        ChannelProfile(key="x", **payload)


def test_unknown_format_is_rejected():
    payload = _valid_payload()
    payload["titles"]["wtf_daily"] = "WTF #{n}"
    with pytest.raises(ValidationError, match="wtf_daily"):
        ChannelProfile(key="x", **payload)


def test_title_longer_than_youtube_limit_is_rejected():
    payload = _valid_payload()
    payload["titles"]["top_daily"] = "x" * 95 + " #{n}"
    with pytest.raises(ValidationError, match="100"):
        ChannelProfile(key="x", **payload)


def test_unknown_timezone_is_rejected():
    payload = _valid_payload()
    payload["schedule"]["top_daily"] = {"time": "15:00", "timezone": "Mars/Olympus"}
    with pytest.raises(ValidationError, match="Mars/Olympus"):
        ChannelProfile(key="x", **payload)


def test_bad_time_is_rejected():
    payload = _valid_payload()
    payload["schedule"]["top_daily"] = {"time": "25:00", "timezone": "America/New_York"}
    with pytest.raises(ValidationError):
        ChannelProfile(key="x", **payload)


def test_hashtags_need_hash_prefix():
    with pytest.raises(ValidationError, match="#"):
        ChannelProfile(key="x", **_valid_payload(hashtags=["GTA6"]))


def test_tags_over_youtube_budget_are_rejected():
    with pytest.raises(ValidationError, match="500"):
        ChannelProfile(key="x", **_valid_payload(tags=["a" * 100] * 6))


def test_load_profile_reads_from_given_dir(tmp_path):
    (tmp_path / "custom.json").write_text(json.dumps(_valid_payload()), encoding="utf-8")
    assert load_profile("custom", channels_dir=tmp_path).display_name == "Test Channel"


def test_missing_profile_names_the_path(tmp_path):
    with pytest.raises(ProfileNotFound, match="nope.json"):
        load_profile("nope", channels_dir=tmp_path)


def test_profile_key_cannot_escape_channels_dir(tmp_path):
    with pytest.raises(ProfileNotFound):
        load_profile("../secrets", channels_dir=tmp_path)


def test_channels_dir_is_versioned_location():
    assert CHANNELS_DIR.parts[-3:] == ("assets", "input", "channels")


# --------------------------------------------------------------------------- #
# Formats
# --------------------------------------------------------------------------- #

def test_formats_are_the_four_agreed():
    assert set(FORMATS) == {"top_daily", "funny_daily", "top10_weekly", "funny_weekly"}


def test_daily_formats_have_a_short_and_a_medium():
    for key in ("top_daily", "funny_daily"):
        spec = FORMATS[key]
        assert spec.has_short
        assert spec.window == timedelta(hours=24)
        assert (spec.min_s, spec.max_s) == (180, 300)


def test_weekly_formats_are_long_only():
    for key in ("top10_weekly", "funny_weekly"):
        spec = FORMATS[key]
        assert not spec.has_short
        assert spec.window == timedelta(days=7)
        assert (spec.min_s, spec.max_s) == (480, 600)


def test_top10_weekly_holds_exactly_ten_clips():
    spec = FORMATS["top10_weekly"]
    assert spec.min_clips == spec.max_clips == 10


def test_short_cap_stays_under_content_id_block_threshold():
    assert SHORT_MAX_S < 60


@pytest.mark.parametrize("day, expected", [
    (date(2026, 11, 18), set()),                                         # before launch
    (date(2026, 11, 19), {"top_daily", "funny_daily"}),                  # launch Thursday: no week yet
    (date(2026, 11, 20), {"top_daily", "funny_daily"}),                  # Friday, still no week
    (date(2026, 11, 25), {"top_daily", "funny_daily"}),                  # Wednesday
    (date(2026, 11, 26), {"top_daily", "funny_daily", "top10_weekly"}),  # first weekly Thursday
    (date(2026, 11, 27), {"top_daily", "funny_daily", "funny_weekly"}),  # first weekly Friday
    (date(2026, 11, 29), {"top_daily", "funny_daily"}),                  # Sunday
])
def test_formats_due_on(day, expected):
    assert {spec.key for spec in formats_due_on(day)} == expected
