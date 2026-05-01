"""Job queue routes: list, create, detail, retry."""
from __future__ import annotations

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.web.dependencies import get_job_queue_manager, get_db
from app.core.job_queue import JobQueueManager, JobQueueError
from app.db.models import Job
from sqlalchemy.orm import Session


router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/", response_class=HTMLResponse)
def list_jobs(
    request: Request,
    status: str | None = None,
    job_queue: JobQueueManager = Depends(get_job_queue_manager),
    db: Session = Depends(get_db),
):
    """Render jobs list page with optional status filter.

    Args:
        request: FastAPI request
        status: Optional status filter (pending/running/completed/failed)
        job_queue: Job queue manager dependency
        db: Database session

    Returns:
        Jobs list HTML page
    """
    from app.db.models import Account

    jobs = job_queue.get_job_history(limit=100, status=status)
    accounts = db.query(Account).filter_by(is_active=True).order_by(Account.name).all()

    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={
            "jobs": jobs,
            "status_filter": status,
            "accounts": accounts,
        }
    )


@router.post("/create")
def create_job(
    prompt: str = Form(...),
    priority: int = Form(0),
    account_id: int = Form(...),
    job_queue: JobQueueManager = Depends(get_job_queue_manager),
    db: Session = Depends(get_db),
):
    """Create new job.

    Args:
        prompt: Job prompt text
        priority: Job priority (higher = first)
        account_id: Required account ID to use
        job_queue: Job queue manager dependency
        db: Database session

    Returns:
        Redirect to jobs list
    """
    from app.db.models import Account

    # Validate account exists and is active
    account = db.query(Account).filter_by(id=account_id, is_active=True).first()
    if not account:
        raise HTTPException(status_code=400, detail="Invalid or inactive account selected")

    # Check if account has enough quota
    quota_needed = 1600  # Approximate quota for one video upload
    quota_available = account.daily_quota - account.quota_used
    if quota_available < quota_needed:
        raise HTTPException(
            status_code=400,
            detail=f"Account '{account.name}' doesn't have enough quota. Available: {quota_available}, needed: {quota_needed}"
        )

    job_queue.enqueue(prompt=prompt, priority=priority, account_id=account_id)
    return RedirectResponse(url="/jobs", status_code=303)


@router.get("/{job_id}", response_class=HTMLResponse)
def job_detail(
    request: Request,
    job_id: str,
    db: Session = Depends(get_db),
):
    """Render job detail page.

    Args:
        request: FastAPI request
        job_id: Job ID
        db: Database session

    Returns:
        Job detail HTML page
    """
    job = db.query(Job).filter_by(job_id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return templates.TemplateResponse(
        request=request,
        name="job_detail.html",
        context={
            "job": job,
        }
    )


@router.post("/{job_id}/retry")
def retry_job(
    job_id: str,
    job_queue: JobQueueManager = Depends(get_job_queue_manager),
):
    """Retry failed job.

    Args:
        job_id: Job ID
        job_queue: Job queue manager dependency

    Returns:
        Redirect to job detail page
    """
    try:
        job_queue.retry_failed_job(job_id)
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
    except JobQueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
