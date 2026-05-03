"""SQLAlchemy engine and session management."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# Base class for all models
Base = declarative_base()

# Default database URL from environment
_DEFAULT_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{Path(__file__).resolve().parent.parent.parent / 'data' / 'app.db'}"
)


def _apply_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """PRAGMAs de concorrência aplicados a TODA conexão SQLite nova.

    - journal_mode=WAL: leitores não bloqueiam escritor; writers ainda
      serializam mas sem bloquear leituras concorrentes.
    - busy_timeout=5000: aguarda até 5s em contenção antes de devolver
      SQLITE_BUSY. Evita que workers concorrentes exploquem sob load.
    """
    cursor = dbapi_connection.cursor()
    try:
        # PRAGMA journal_mode=WAL returns the resulting mode. On some filesystems
        # (network-mounted, read-only) the switch fails silently and the connection
        # keeps its previous mode — which would leave concurrent workers
        # deadlocking later with no obvious reason. Fail loudly at startup instead.
        resulting_mode = cursor.execute("PRAGMA journal_mode=WAL").fetchone()
        if resulting_mode is None or str(resulting_mode[0]).lower() != "wal":
            raise RuntimeError(
                f"SQLite refused to enable WAL mode (got {resulting_mode!r}). "
                "Ensure the database file's directory is writable and not on "
                "a filesystem that blocks WAL."
            )
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def get_engine(database_url: str | None = None):
    """Create SQLAlchemy engine.

    Args:
        database_url: Database connection string. If None, uses DATABASE_URL env var.

    Returns:
        Engine instance
    """
    url = database_url or _DEFAULT_DATABASE_URL

    # SQLite-specific: enable foreign keys
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}

    engine = create_engine(url, connect_args=connect_args, echo=False)

    # WAL + busy_timeout só pra SQLite. Outros drivers não têm esses pragmas.
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _apply_sqlite_pragmas)

    return engine


# Global engine and session factory (initialized on first import)
_engine = get_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@contextmanager
def get_session(engine=None) -> Session:
    """Context manager for database sessions.

    Usage:
        with get_session() as session:
            session.query(Account).all()

    Args:
        engine: Optional engine override (for testing)

    Yields:
        Session instance
    """
    if engine is not None:
        # Test override
        SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        session = SessionFactory()
    else:
        session = SessionLocal()

    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
