"""Database engine configuration: WAL mode and busy_timeout."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from app.db.database import get_engine


def test_sqlite_engine_enables_wal_mode(tmp_path: Path) -> None:
    """get_engine() applied to a sqlite URL must set journal_mode=WAL."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert mode == "wal", f"expected WAL mode, got {mode!r}"


def test_sqlite_engine_sets_busy_timeout(tmp_path: Path) -> None:
    """get_engine() must configure a non-zero busy_timeout so contention
    doesn't immediately raise SQLITE_BUSY."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        timeout_ms = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert timeout_ms == 5000, f"expected busy_timeout=5000, got {timeout_ms}"
