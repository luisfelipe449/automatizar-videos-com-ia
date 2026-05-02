"""Job queue management: enqueue, dequeue, status updates."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.db.models import Account, Job, Upload


class JobQueueError(Exception):
    """Raised when job queue operations fail."""


class JobQueueManager:
    """Manages job queue: enqueue, get_next, update status, retry."""

    def __init__(self, session: Session):
        """Initialize manager with database session.

        Args:
            session: SQLAlchemy session
        """
        self.session = session

    def enqueue(
        self,
        prompt: str,
        priority: int = 0,
        account_id: int | None = None,
        category: int = 24
    ) -> Job:
        """Create new pending job.

        Args:
            prompt: User input text
            priority: Higher values execute first (default 0)
            account_id: Optional account assignment (None = auto-assign by worker)
            category: YouTube category ID (default: 24 = Entertainment)

        Returns:
            Created Job instance
        """
        # Generate job_id: YYYY-MM-DD_slug_hash
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")

        # Create slug from prompt (first 3 words, lowercase, replace spaces with hyphens)
        slug = re.sub(r'[^\w\s-]', '', prompt.lower())[:50]
        slug = re.sub(r'[-\s]+', '-', slug).strip('-')
        words = slug.split('-')[:3]
        slug = '-'.join(words) if words else 'untitled'

        # Add hash for uniqueness
        hash_short = hashlib.sha256(prompt.encode()).hexdigest()[:6]
        job_id = f"{date_str}_{slug}_{hash_short}"

        job = Job(
            job_id=job_id,
            account_id=account_id,
            prompt=prompt,
            category=category,
            status="pending",
            priority=priority
        )

        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)

        return job

    def get_next_pending(self) -> Job | None:
        """Get highest priority pending job.

        Returns:
            Next job to process, or None if queue empty
        """
        job = (
            self.session.query(Job)
            .filter(Job.status == "pending")
            .order_by(Job.priority.desc(), Job.created_at.asc())
            .first()
        )
        return job

    def update_status(
        self,
        job_id: str,
        status: str,
        stage: str | None = None,
        error: str | None = None
    ) -> None:
        """Update job execution state.

        Args:
            job_id: Job ID
            status: New status (pending/running/completed/failed)
            stage: Current pipeline stage (script/tts/captions/visual/render/upload)
            error: Error message if status=failed

        Raises:
            JobQueueError: If job not found
        """
        job = self.session.query(Job).filter_by(job_id=job_id).first()
        if not job:
            raise JobQueueError(f"Job {job_id} not found")

        job.status = status
        if stage is not None:
            job.stage = stage
        if error is not None:
            job.error_message = error

        # Update timestamps
        if status == "running" and job.started_at is None:
            job.started_at = datetime.now(timezone.utc)
        elif status in ("completed", "failed"):
            job.completed_at = datetime.now(timezone.utc)

        self.session.commit()

    def mark_completed(self, job_id: str, video_id: str) -> None:
        """Finalize job and create upload record.

        Args:
            job_id: Job ID
            video_id: YouTube video ID

        Raises:
            JobQueueError: If job not found or not associated with account
        """
        job = self.session.query(Job).filter_by(job_id=job_id).first()
        if not job:
            raise JobQueueError(f"Job {job_id} not found")
        if not job.account_id:
            raise JobQueueError(f"Job {job_id} has no associated account")

        # Update job status
        job.status = "completed"
        job.stage = "upload"
        job.completed_at = datetime.now(timezone.utc)

        # Create upload record
        upload = Upload(
            job_id=job.job_id,
            account_id=job.account_id,
            video_id=video_id,
            title=job.prompt[:255],  # truncate if needed
            privacy_status="private",  # default, can be overridden later
            views=0
        )

        self.session.add(upload)
        self.session.commit()

    def get_job_history(self, limit: int = 50, status: str | None = None) -> list[Job]:
        """Retrieve recent jobs, optionally filtered by status.

        Args:
            limit: Maximum number of jobs to return
            status: Filter by status (None = all)

        Returns:
            List of Job instances, newest first
        """
        query = self.session.query(Job)
        if status:
            query = query.filter(Job.status == status)
        return query.order_by(Job.created_at.desc()).limit(limit).all()

    def retry_failed_job(self, job_id: str) -> Job:
        """Reset failed job to pending for retry.

        Args:
            job_id: Job ID

        Returns:
            Updated Job instance

        Raises:
            JobQueueError: If job not found or not in failed state
        """
        job = self.session.query(Job).filter_by(job_id=job_id).first()
        if not job:
            raise JobQueueError(f"Job {job_id} not found")
        if job.status != "failed":
            raise JobQueueError(f"Job {job_id} is not in failed state (current: {job.status})")

        job.status = "pending"
        job.stage = None
        job.error_message = None
        job.account_id = None  # unassign, let worker pick new account
        job.started_at = None
        job.completed_at = None

        self.session.commit()
        self.session.refresh(job)

        return job
