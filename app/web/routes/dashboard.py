"""Dashboard route: main overview with metrics."""
from __future__ import annotations

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.web.dependencies import (
    get_account_manager,
    get_analytics_aggregator,
    get_job_queue_manager,
)
from app.core.account_manager import AccountManager
from app.core.analytics import AnalyticsAggregator
from app.core.job_queue import JobQueueManager


router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    account_mgr: AccountManager = Depends(get_account_manager),
    analytics: AnalyticsAggregator = Depends(get_analytics_aggregator),
    job_queue: JobQueueManager = Depends(get_job_queue_manager),
):
    """Render dashboard with account stats and job metrics.

    Args:
        request: FastAPI request
        account_mgr: Account manager dependency
        analytics: Analytics aggregator dependency
        job_queue: Job queue manager dependency

    Returns:
        Dashboard HTML page
    """
    # Get account statistics
    accounts = account_mgr.list_accounts()
    account_stats = analytics.get_account_stats()

    # Get job queue status
    pending_jobs = job_queue.get_job_history(limit=100, status="pending")
    running_jobs = job_queue.get_job_history(limit=100, status="running")
    recent_jobs = job_queue.get_job_history(limit=10)

    # Get daily summary for chart
    daily_summary = analytics.get_daily_summary(days=7)

    # Calculate totals
    total_accounts = len(accounts)
    active_accounts = len([a for a in accounts if a.is_active])
    total_quota_available = sum(a.daily_quota - a.quota_used for a in accounts if a.is_active)

    # Prepare chart data
    chart_labels = [str(day["date"]) for day in daily_summary]
    chart_data = {
        "completed": [day["completed_jobs"] for day in daily_summary],
        "failed": [day["failed_jobs"] for day in daily_summary],
        "pending": [day["pending_jobs"] for day in daily_summary],
    }

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "total_accounts": total_accounts,
            "active_accounts": active_accounts,
            "total_quota_available": total_quota_available,
            "pending_jobs_count": len(pending_jobs),
            "running_jobs_count": len(running_jobs),
            "recent_jobs": recent_jobs,
            "account_stats": account_stats,
            "chart_labels": chart_labels,
            "chart_data": chart_data,
        }
    )
