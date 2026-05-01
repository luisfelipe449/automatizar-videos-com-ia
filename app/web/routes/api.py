"""JSON API routes for AJAX polling."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.web.dependencies import get_db
from app.db.models import Job, Account


router = APIRouter()


@router.get("/status")
def api_status(db: Session = Depends(get_db)):
    """Get system status summary.

    Args:
        db: Database session

    Returns:
        JSON with system stats
    """
    accounts = db.query(Account).all()
    jobs_pending = db.query(Job).filter_by(status="pending").count()
    jobs_running = db.query(Job).filter_by(status="running").count()
    jobs_completed = db.query(Job).filter_by(status="completed").count()
    jobs_failed = db.query(Job).filter_by(status="failed").count()

    active_accounts = [a for a in accounts if a.is_active]
    total_quota_available = sum(a.daily_quota - a.quota_used for a in active_accounts)

    return {
        "accounts": {
            "total": len(accounts),
            "active": len(active_accounts),
            "quota_available": total_quota_available,
        },
        "jobs": {
            "pending": jobs_pending,
            "running": jobs_running,
            "completed": jobs_completed,
            "failed": jobs_failed,
        }
    }


@router.get("/jobs/{job_id}/status")
def api_job_status(job_id: str, db: Session = Depends(get_db)):
    """Get job status for polling.

    Args:
        job_id: Job ID
        db: Database session

    Returns:
        JSON with job status
    """
    job = db.query(Job).filter_by(job_id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return {
        "job_id": job.job_id,
        "status": job.status,
        "stage": job.stage,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
