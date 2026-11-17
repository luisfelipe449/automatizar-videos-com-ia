"""Clips-mode tables (Clip, Edition, EditionClip) and their migration."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.db.database import Base, get_engine, get_session
from app.db.migrations import add_clips_schema
from app.db.models import Account, Clip, Edition, EditionClip


@pytest.fixture
def engine(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    return eng


def _clip(clip_id: str, *, source: str = "twitch", views: int = 100) -> Clip:
    return Clip(
        source=source,
        source_clip_id=clip_id,
        url=f"https://clips.twitch.tv/{clip_id}",
        broadcaster_name="alanzoka",
        broadcaster_url="https://twitch.tv/alanzoka",
        title="insane jump",
        language="pt",
        view_count=views,
        duration_s=28.5,
        created_at_source=datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc),
    )


def test_clip_defaults_leave_classification_empty(engine):
    with get_session(engine) as session:
        session.add(_clip("abc"))
        session.commit()
        clip = session.query(Clip).one()
        assert clip.category is None
        assert clip.classified_at is None
        assert clip.local_path is None
        assert clip.harvested_at is not None


def test_clip_is_unique_per_source_and_id(engine):
    with get_session(engine) as session:
        session.add(_clip("abc"))
        session.commit()
        session.add(_clip("abc"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()  # get_session commits on exit


def test_same_clip_id_on_another_source_is_a_different_clip(engine):
    with get_session(engine) as session:
        session.add_all([_clip("abc"), _clip("abc", source="kick")])
        session.commit()
        assert session.query(Clip).count() == 2


def test_edition_is_unique_per_format_and_date(engine):
    with get_session(engine) as session:
        session.add(Edition(format_key="top_daily", edition_date=date(2026, 11, 19)))
        session.commit()
        session.add(Edition(format_key="top_daily", edition_date=date(2026, 11, 19)))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()  # get_session commits on exit


def test_edition_starts_planned_with_no_numbers(engine):
    with get_session(engine) as session:
        session.add(Edition(format_key="funny_daily", edition_date=date(2026, 11, 19)))
        session.commit()
        edition = session.query(Edition).one()
        assert edition.status == "planned"
        assert edition.numbers == {}


def test_edition_numbers_round_trip_per_channel(engine):
    with get_session(engine) as session:
        edition = Edition(format_key="top_daily", edition_date=date(2026, 11, 19))
        edition.numbers = {"five-star-clips-br": 3, "five-star-clips": 4}
        session.add(edition)
        session.commit()
        assert session.query(Edition).one().numbers == {"five-star-clips-br": 3, "five-star-clips": 4}


def test_edition_clips_come_back_in_position_order(engine):
    with get_session(engine) as session:
        a, b, c = _clip("a"), _clip("b"), _clip("c")
        edition = Edition(format_key="top_daily", edition_date=date(2026, 11, 19))
        session.add_all([a, b, c, edition])
        session.flush()
        # Inserted out of order on purpose.
        session.add_all([
            EditionClip(edition_id=edition.id, clip_id=c.id, position=3),
            EditionClip(edition_id=edition.id, clip_id=a.id, position=1, in_short=True),
            EditionClip(edition_id=edition.id, clip_id=b.id, position=2),
        ])
        session.commit()
        session.expire_all()

        edition = session.query(Edition).one()
        assert [ec.clip.source_clip_id for ec in edition.items] == ["a", "b", "c"]
        assert [ec.in_short for ec in edition.items] == [True, False, False]
        assert edition.items[0].trim_start_s is None


def test_a_clip_appears_at_most_once_per_edition(engine):
    with get_session(engine) as session:
        clip = _clip("a")
        edition = Edition(format_key="top_daily", edition_date=date(2026, 11, 19))
        session.add_all([clip, edition])
        session.flush()
        session.add_all([
            EditionClip(edition_id=edition.id, clip_id=clip.id, position=1),
            EditionClip(edition_id=edition.id, clip_id=clip.id, position=2),
        ])
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()  # get_session commits on exit


def test_account_channel_profile_is_optional(engine):
    with get_session(engine) as session:
        session.add(Account(
            name="Five Star Clips", email="x@example.com", client_id="c",
            client_secret="s", refresh_token="r",
            quota_reset_at=datetime.now(timezone.utc),
        ))
        session.commit()
        account = session.query(Account).one()
        assert account.channel_profile is None
        account.channel_profile = "five-star-clips"
        session.commit()
        assert session.query(Account).one().channel_profile == "five-star-clips"


# --------------------------------------------------------------------------- #
# Migration of a database created before clips mode existed
# --------------------------------------------------------------------------- #

_LEGACY_ACCOUNTS_DDL = """
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(255) NOT NULL UNIQUE,
    email VARCHAR(255) NOT NULL,
    client_id VARCHAR(255) NOT NULL,
    client_secret VARCHAR(255) NOT NULL,
    refresh_token TEXT NOT NULL,
    daily_quota INTEGER NOT NULL,
    quota_used INTEGER NOT NULL,
    quota_reset_at DATETIME NOT NULL,
    is_active BOOLEAN NOT NULL,
    created_at DATETIME,
    updated_at DATETIME
)
"""


@pytest.fixture
def legacy_engine(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(_LEGACY_ACCOUNTS_DDL)
    conn.execute(
        "INSERT INTO accounts (name, email, client_id, client_secret, refresh_token,"
        " daily_quota, quota_used, quota_reset_at, is_active)"
        " VALUES ('old', 'o@example.com', 'c', 's', 'r', 10000, 0, '2026-11-18 00:00:00', 1)"
    )
    conn.commit()
    conn.close()
    return get_engine(f"sqlite:///{db_path}")


def test_migration_adds_column_and_tables_and_keeps_rows(legacy_engine):
    changes = add_clips_schema(legacy_engine)

    insp = inspect(legacy_engine)
    assert "channel_profile" in {c["name"] for c in insp.get_columns("accounts")}
    assert {"clips", "editions", "edition_clips"} <= set(insp.get_table_names())
    assert changes  # reported what it did

    with get_session(legacy_engine) as session:
        account = session.query(Account).one()
        assert account.name == "old"
        assert account.channel_profile is None


def test_migration_is_idempotent(legacy_engine):
    add_clips_schema(legacy_engine)
    assert add_clips_schema(legacy_engine) == []
