"""FastAPI dependencies for database sessions and managers."""
from __future__ import annotations

from typing import Generator

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.core.account_manager import AccountManager
from app.core.job_queue import JobQueueManager
from app.core.analytics import AnalyticsAggregator


def get_db() -> Generator[Session, None, None]:
    """Dependency for FastAPI routes to get DB session.

    Yields:
        Database session
    """
    with get_session() as session:
        yield session


def get_account_manager(db: Session = Depends(get_db)) -> AccountManager:
    """Dependency to get AccountManager instance.

    Args:
        db: Database session

    Returns:
        AccountManager instance
    """
    return AccountManager(db)


def get_job_queue_manager(db: Session = Depends(get_db)) -> JobQueueManager:
    """Dependency to get JobQueueManager instance.

    Args:
        db: Database session

    Returns:
        JobQueueManager instance
    """
    return JobQueueManager(db)


def get_analytics_aggregator(db: Session = Depends(get_db)) -> AnalyticsAggregator:
    """Dependency to get AnalyticsAggregator instance.

    Args:
        db: Database session

    Returns:
        AnalyticsAggregator instance
    """
    return AnalyticsAggregator(db)
