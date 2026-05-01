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
):
    """Render jobs list page with optional status filter.

    Args:
        request: FastAPI request
        status: Optional status filter (pending/running/completed/failed)
        job_queue: Job queue manager dependency

    Returns:
        Jobs list HTML page
    """
    jobs = job_queue.get_job_history(limit=100, status=status)

    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={
            "jobs": jobs,
            "status_filter": status,
        }
    )


@router.post("/create")
def create_job(
    prompt: str = Form(...),
    priority: int = Form(0),
    job_queue: JobQueueManager = Depends(get_job_queue_manager),
):
    """Create new job.

    Args:
        prompt: Job prompt text
        priority: Job priority (higher = first)
        job_queue: Job queue manager dependency

    Returns:
        Redirect to jobs list
    """
    job_queue.enqueue(prompt=prompt, priority=priority)
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
