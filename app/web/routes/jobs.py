"""Job queue routes: list, create, detail, retry."""
from __future__ import annotations

import json
from pathlib import Path
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.web.dependencies import get_job_queue_manager, get_db
from app.core.job_queue import JobQueueManager, JobQueueError
from app.db.models import Job
from app.services import script_service
from app.services.script_service import AnglesGenerationError, ScriptGenerationError
from app.utils import filepaths
from sqlalchemy.orm import Session

# Each YouTube videos.insert costs 1600 units; a 5-video theme batch needs 8000.
_QUOTA_PER_UPLOAD = 1600


class GenerateAnglesRequest(BaseModel):
    """Body for POST /jobs/generate-angles — theme preview, no DB writes."""

    theme: str = Field(min_length=1, max_length=500)
    count: int = Field(default=5, ge=1, le=10)


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
    category: int = Form(24),
    job_queue: JobQueueManager = Depends(get_job_queue_manager),
    db: Session = Depends(get_db),
):
    """Create new job.

    Args:
        prompt: Job prompt text
        priority: Job priority (higher = first)
        account_id: Required account ID to use
        category: YouTube category ID (default: 24 = Entertainment)
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

    job_queue.enqueue(prompt=prompt, priority=priority, account_id=account_id, category=category)
    return RedirectResponse(url="/jobs", status_code=303)


@router.post("/generate-angles")
def generate_angles_endpoint(payload: GenerateAnglesRequest):
    """Preview N viral angles for a theme. Stateless — no DB writes.

    Called by the "Tema" mode in the job-creation modal: the user enters a
    theme, clicks "Gerar ângulos", and this endpoint returns the proposed
    angles for them to edit / confirm / regenerate before any script is
    generated or any job is enqueued.
    """
    try:
        angles = script_service.generate_angles(payload.theme, n=payload.count)
    except AnglesGenerationError as exc:
        raise HTTPException(status_code=500, detail=f"Angle generation failed: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"angles": angles}


@router.post("/create-batch")
def create_batch(
    theme: str = Form(...),
    angles_json: str = Form(...),
    account_id: int = Form(...),
    category: int = Form(24),
    priority: int = Form(0),
    job_queue: JobQueueManager = Depends(get_job_queue_manager),
    db: Session = Depends(get_db),
):
    """Create N jobs from user-confirmed angles. All-or-nothing.

    Flow:
      1. Validate the account and that it has quota for N uploads.
      2. Call `generate_script` once per angle. If ANY angle fails, abort —
         zero jobs are enqueued and the caller gets an error indicating
         which angle failed.
      3. On full success, enqueue one job per angle with the SAME account,
         category, and priority, using each angle's `prompt` string.

    The `theme` field is stored only in the flash message for now — jobs
    themselves stay flat (no batch_id/theme column, by design).
    """
    from app.db.models import Account

    # --- Parse angles ------------------------------------------------------
    try:
        angles_raw = json.loads(angles_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid angles_json: {exc}")
    if not isinstance(angles_raw, list) or not angles_raw:
        raise HTTPException(status_code=400, detail="angles_json must be a non-empty list")

    angles: list[dict[str, str]] = []
    for idx, entry in enumerate(angles_raw, start=1):
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail=f"angle {idx} is not an object")
        title = (entry.get("title") or "").strip()
        prompt = (entry.get("prompt") or "").strip()
        if not title or not prompt:
            raise HTTPException(
                status_code=400,
                detail=f"angle {idx} has empty title or prompt",
            )
        angles.append({"title": title, "prompt": prompt})

    n = len(angles)

    # --- Validate account + quota BEFORE calling the LLM -------------------
    account = db.query(Account).filter_by(id=account_id, is_active=True).first()
    if not account:
        raise HTTPException(status_code=400, detail="Invalid or inactive account selected")

    quota_needed = _QUOTA_PER_UPLOAD * n
    quota_available = account.daily_quota - account.quota_used
    if quota_available < quota_needed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Account '{account.name}' doesn't have enough quota for {n} uploads. "
                f"Available: {quota_available}, needed: {quota_needed}"
            ),
        )

    # --- Generate all N scripts up front (all-or-nothing) ------------------
    # Derive job_ids first so we can report which one failed if anything blows up.
    planned: list[tuple[str, str]] = []  # (job_id, prompt)
    for angle in angles:
        job_id = filepaths.make_job_id(angle["prompt"])
        planned.append((job_id, angle["prompt"]))

    for idx, (job_id, prompt_text) in enumerate(planned, start=1):
        try:
            script_service.generate_script(
                prompt_text,
                job_id=job_id,
                video_title=angles[idx - 1]["title"],
            )
        except ScriptGenerationError as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Script generation failed for angle {idx} "
                    f"({angles[idx - 1]['title']!r}): {exc}. "
                    f"No jobs were enqueued."
                ),
            )

    # --- All scripts ready; enqueue the jobs -------------------------------
    # Pass the same job_id used for script generation so the worker reuses
    # the already-persisted script.json instead of regenerating.
    for job_id, prompt_text in planned:
        job_queue.enqueue(
            prompt=prompt_text,
            priority=priority,
            account_id=account_id,
            category=category,
            job_id=job_id,
        )

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

    # Check if video file exists for manual upload
    video_path = filepaths.final_video_path(job_id)
    video_exists = video_path.exists() if video_path else False

    # Load script data for title and description
    script_data = None
    if video_exists:
        script_path = filepaths.script_path(job_id)
        if script_path and script_path.exists():
            try:
                script_data = json.loads(script_path.read_text(encoding='utf-8'))
            except Exception:
                script_data = None

    return templates.TemplateResponse(
        request=request,
        name="job_detail.html",
        context={
            "job": job,
            "video_exists": video_exists,
            "script_data": script_data,
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


@router.get("/{job_id}/download")
def download_video(
    job_id: str,
    db: Session = Depends(get_db),
):
    """Download rendered video for manual upload.

    Args:
        job_id: Job ID
        db: Database session

    Returns:
        Video file download
    """
    job = db.query(Job).filter_by(job_id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Check if video exists
    video_path = filepaths.final_video_path(job_id)
    if not video_path or not video_path.exists():
        raise HTTPException(status_code=404, detail=f"Video file not found for job {job_id}")

    # Return video file for download
    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"{job_id}.mp4"
    )
