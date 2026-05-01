"""SQLAlchemy engine and session management."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# Base class for all models
Base = declarative_base()

# Default database URL from environment
_DEFAULT_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{Path(__file__).resolve().parent.parent.parent / 'data' / 'app.db'}"
)


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

    return create_engine(url, connect_args=connect_args, echo=False)


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
