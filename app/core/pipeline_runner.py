"""Pipeline runner: bridges job queue and orchestrator."""
from __future__ import annotations

import os
from sqlalchemy.orm import Session

from app.core.account_manager import AccountManager
from app.core.job_queue import JobQueueManager
from app.orchestrator import run_pipeline
from app.services.youtube_service import YouTubeQuotaExceeded


class PipelineRunnerError(Exception):
    """Raised when pipeline runner operations fail."""


class PipelineRunner:
    """Runs pipeline jobs with account management and quota tracking."""

    def __init__(self, session: Session):
        """Initialize runner with database session.

        Args:
            session: SQLAlchemy session
        """
        self.session = session
        self.account_mgr = AccountManager(session)
        self.job_queue = JobQueueManager(session)

    def run_job(self, job_id: str) -> None:
        """Execute pipeline for given job.

        Assigns available account, runs pipeline, updates job status,
        and consumes quota on success. Handles quota exceeded by
        resetting job to pending with different account.

        Args:
            job_id: Job ID to execute

        Raises:
            PipelineRunnerError: If job not found
        """
        from app.db.models import Job

        # Get job
        job = self.session.query(Job).filter_by(job_id=job_id).first()
        if not job:
            raise PipelineRunnerError(f"Job {job_id} not found")

        # Get available account
        account = self.account_mgr.get_available_account()
        if not account:
            self.job_queue.update_status(
                job_id=job_id,
                status="failed",
                error="No available account with sufficient quota"
            )
            return

        # Assign account and mark as running
        job.account_id = account.id
        self.job_queue.update_status(job_id=job_id, status="running")

        # Prepare account credentials for orchestrator
        account_dict = {
            "id": account.id,
            "name": account.name,
            "email": account.email,
            "client_id": account.client_id,
            "client_secret": account.client_secret,
            "refresh_token": account.refresh_token,
        }

        try:
            # Set environment variables for the pipeline
            # Store old values to restore later
            old_client_id = os.environ.get("YOUTUBE_CLIENT_ID")
            old_client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")
            old_refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN")

            os.environ["YOUTUBE_CLIENT_ID"] = account.client_id
            os.environ["YOUTUBE_CLIENT_SECRET"] = account.client_secret
            os.environ["YOUTUBE_REFRESH_TOKEN"] = account.refresh_token

            try:
                # Run pipeline
                result = run_pipeline(
                    prompt=job.prompt,
                    job_id=job_id,
                    skip_upload=False,
                    account=account_dict
                )
            finally:
                # Restore environment variables
                if old_client_id:
                    os.environ["YOUTUBE_CLIENT_ID"] = old_client_id
                else:
                    os.environ.pop("YOUTUBE_CLIENT_ID", None)
                if old_client_secret:
                    os.environ["YOUTUBE_CLIENT_SECRET"] = old_client_secret
                else:
                    os.environ.pop("YOUTUBE_CLIENT_SECRET", None)
                if old_refresh_token:
                    os.environ["YOUTUBE_REFRESH_TOKEN"] = old_refresh_token
                else:
                    os.environ.pop("YOUTUBE_REFRESH_TOKEN", None)

            # Update job based on result
            if result["success"]:
                # Update last completed stage
                if result["stages_completed"]:
                    last_stage = result["stages_completed"][-1]
                    self.job_queue.update_status(
                        job_id=job_id,
                        status="completed",
                        stage=last_stage
                    )

                # Mark completed and create upload record
                if result["video_id"]:
                    self.job_queue.mark_completed(job_id=job_id, video_id=result["video_id"])

                # Consume quota on successful upload
                self.account_mgr.consume_quota(account_id=account.id, amount=1600)
            else:
                # Pipeline returned failure
                last_stage = result["stages_completed"][-1] if result["stages_completed"] else None
                self.job_queue.update_status(
                    job_id=job_id,
                    status="failed",
                    stage=last_stage,
                    error=result["error"]
                )

        except YouTubeQuotaExceeded:
            # Quota exceeded during upload - reset job to pending
            # and unassign account so it can be retried with different account
            job.status = "pending"
            job.account_id = None
            job.stage = None
            job.error_message = None
            job.started_at = None
            self.session.commit()

        except Exception as exc:
            # Other errors - mark job as failed
            self.job_queue.update_status(
                job_id=job_id,
                status="failed",
                error=str(exc)
            )
