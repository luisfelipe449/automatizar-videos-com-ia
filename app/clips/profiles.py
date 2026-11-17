"""Channel profiles: per-channel language, wording and publishing schedule.

One JSON file per YouTube channel in `assets/input/channels/<key>.json`;
`Account.channel_profile` holds the key. Every format in
`app.clips.formats.FORMATS` must have a title, a description and a
schedule entry, so a missing translation fails at load time instead of
surfacing as an empty title on a rendered video.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import INPUT_DIR
from app.clips.formats import FORMATS

CHANNELS_DIR: Path = INPUT_DIR / "channels"

# YouTube Data API limits on snippet fields.
_TITLE_MAX = 100
_DESCRIPTION_MAX = 5000
_TAGS_MAX = 500
# Widest episode number a title must still fit with.
_WIDEST_NUMBER = 9999


class ProfileNotFound(FileNotFoundError):
    """No profile file for the requested key."""


class PublishSlot(BaseModel):
    """Local wall-clock time in the audience's zone, e.g. 15:00 America/New_York.

    Stored in the audience's zone (not BRT) so daylight-saving changes move
    the BRT time automatically instead of moving the audience's time.
    """

    time: time
    timezone: str

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value


class ChannelProfile(BaseModel):
    key: str
    display_name: str
    language: str = Field(description="BCP-47 tag for defaultLanguage, e.g. pt-BR")
    tts_voice: str
    titles: dict[str, str]
    descriptions: dict[str, str]
    schedule: dict[str, PublishSlot]
    outro_cta: str
    hashtags: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _covers_every_format(self) -> "ChannelProfile":
        expected = set(FORMATS)
        for field in ("titles", "descriptions", "schedule"):
            got = set(getattr(self, field))
            if got != expected:
                missing = sorted(expected - got)
                unknown = sorted(got - expected)
                raise ValueError(f"{field}: missing {missing}, unknown {unknown}")
        return self

    @field_validator("titles")
    @classmethod
    def _titles_fit(cls, titles: dict[str, str]) -> dict[str, str]:
        for key, template in titles.items():
            if "{n}" not in template:
                raise ValueError(f"title for {key} must contain {{n}} (episode number)")
            widest = template.format(n=_WIDEST_NUMBER)
            if len(widest) > _TITLE_MAX:
                raise ValueError(f"title for {key} exceeds {_TITLE_MAX} chars: {widest!r}")
        return titles

    @field_validator("descriptions")
    @classmethod
    def _descriptions_fit(cls, descriptions: dict[str, str]) -> dict[str, str]:
        for key, text in descriptions.items():
            if len(text.encode("utf-8")) > _DESCRIPTION_MAX:
                raise ValueError(f"description for {key} exceeds {_DESCRIPTION_MAX} bytes")
        return descriptions

    @field_validator("hashtags")
    @classmethod
    def _hashtags_prefixed(cls, hashtags: list[str]) -> list[str]:
        bad = [h for h in hashtags if not h.startswith("#") or " " in h]
        if bad:
            raise ValueError(f"hashtags must start with # and have no spaces: {bad}")
        return hashtags

    @field_validator("tags")
    @classmethod
    def _tags_fit(cls, tags: list[str]) -> list[str]:
        # YouTube counts a comma between tags and quotes around tags with spaces.
        total = sum(len(t) + (2 if " " in t else 0) for t in tags) + max(len(tags) - 1, 0)
        if total > _TAGS_MAX:
            raise ValueError(f"tags total {total} chars, YouTube allows {_TAGS_MAX}")
        return tags

    def title_for(self, format_key: str, number: int) -> str:
        return self.titles[format_key].format(n=number)

    def publish_at(self, format_key: str, day: date) -> datetime:
        """Aware datetime of the publish slot on `day`, in the slot's zone."""
        slot = self.schedule[format_key]
        return datetime.combine(day, slot.time, tzinfo=ZoneInfo(slot.timezone))


def load_profile(key: str, *, channels_dir: Path | None = None) -> ChannelProfile:
    """Load and validate `<channels_dir>/<key>.json`."""
    base = (channels_dir or CHANNELS_DIR).resolve()
    path = (base / f"{key}.json").resolve()
    if path.parent != base or not path.is_file():
        raise ProfileNotFound(f"channel profile not found: {base / f'{key}.json'}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return ChannelProfile(key=key, **data)
