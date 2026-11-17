"""In-place schema upgrades for databases created by older versions.

`Base.metadata.create_all` creates missing tables but never alters an
existing one, so a column added to an existing model needs an explicit
`ALTER TABLE` here. Each function is idempotent and returns a list of the
changes it actually made (empty when the schema was already current).
"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.db import models


def upgrade_schema(engine: Engine | None = None) -> list[str]:
    """Run every upgrade below against `engine` (default: the app database).

    Entry points that query the ORM (dashboard, worker, clips CLI) call this
    at startup, so pulling a version with a new column never leaves them
    failing on `no such column` until someone runs a script by hand.
    """
    if engine is None:
        from app.db.database import _engine as engine
    return add_clips_schema(engine)


def add_clips_schema(engine: Engine) -> list[str]:
    """Add `accounts.channel_profile` and the clips tables when missing."""
    changes: list[str] = []
    insp = inspect(engine)

    if "accounts" in insp.get_table_names():
        columns = {c["name"] for c in insp.get_columns("accounts")}
        if "channel_profile" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN channel_profile VARCHAR(100)"))
            changes.append("accounts.channel_profile")

    existing = set(insp.get_table_names())
    clips_tables = [models.Clip.__table__, models.Edition.__table__, models.EditionClip.__table__]
    missing = [t for t in clips_tables if t.name not in existing]
    if missing:
        models.Base.metadata.create_all(engine, tables=missing)
        changes.extend(t.name for t in missing)

    return changes
