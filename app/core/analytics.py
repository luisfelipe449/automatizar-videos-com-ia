"""Analytics aggregation for dashboard metrics."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from sqlalchemy import func, case
from sqlalchemy.orm import Session

from app.db.models import Account, Job, Upload


class AnalyticsAggregator:
    """Aggregates metrics for dashboard display."""

    def __init__(self, session: Session):
        """Initialize aggregator with database session.

        Args:
            session: SQLAlchemy session
        """
        self.session = session

    def get_account_stats(self) -> list[dict]:
        """Get upload count and total views per account.

        Returns:
            List of dicts with keys: account_id, account_name, upload_count, total_views
        """
        # Query: JOIN accounts with uploads, aggregate views and count
        results = (
            self.session.query(
                Account.id.label("account_id"),
                Account.name.label("account_name"),
                func.count(Upload.id).label("upload_count"),
                func.sum(Upload.views).label("total_views")
            )
            .outerjoin(Upload, Account.id == Upload.account_id)
            .group_by(Account.id, Account.name)
            .all()
        )

        # Convert to list of dicts
        stats = []
        for row in results:
            stats.append({
                "account_id": row.account_id,
                "account_name": row.account_name,
                "upload_count": row.upload_count,
                "total_views": row.total_views or 0  # NULL sum becomes 0
            })

        return stats

    def get_daily_summary(self, days: int = 7) -> list[dict]:
        """Count jobs grouped by date and status.

        Args:
            days: Number of days to include (default 7)

        Returns:
            List of dicts with keys: date, total_jobs, completed_jobs, failed_jobs, pending_jobs
        """
        # Calculate cutoff date
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Query: Group jobs by date and status
        results = (
            self.session.query(
                func.date(Job.created_at).label("date"),
                func.count(Job.id).label("total_jobs"),
                func.sum(case((Job.status == "completed", 1), else_=0)).label("completed_jobs"),
                func.sum(case((Job.status == "failed", 1), else_=0)).label("failed_jobs"),
                func.sum(case((Job.status == "pending", 1), else_=0)).label("pending_jobs")
            )
            .filter(Job.created_at >= cutoff)
            .group_by(func.date(Job.created_at))
            .order_by(func.date(Job.created_at))
            .all()
        )

        # Convert to list of dicts
        summary = []
        for row in results:
            # SQLite returns date as string, convert to date object
            date_obj = row.date
            if isinstance(date_obj, str):
                date_obj = datetime.strptime(date_obj, "%Y-%m-%d").date()

            summary.append({
                "date": date_obj,
                "total_jobs": row.total_jobs,
                "completed_jobs": row.completed_jobs or 0,
                "failed_jobs": row.failed_jobs or 0,
                "pending_jobs": row.pending_jobs or 0
            })

        return summary
