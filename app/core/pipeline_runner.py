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

        Uses the account already assigned to the job. If job fails due to
        quota issues, marks it as failed with clear error message.

        Args:
            job_id: Job ID to execute

        Raises:
            PipelineRunnerError: If job not found or no account assigned
        """
        from app.db.models import Job, Account

        # Get job
        job = self.session.query(Job).filter_by(job_id=job_id).first()
        if not job:
            raise PipelineRunnerError(f"Job {job_id} not found")

        # Validate account is assigned
        if not job.account_id:
            self.job_queue.update_status(
                job_id=job_id,
                status="failed",
                error="No account assigned to job. Please select an account when creating the job."
            )
            return

        # Get assigned account
        account = self.session.query(Account).filter_by(id=job.account_id).first()
        if not account:
            self.job_queue.update_status(
                job_id=job_id,
                status="failed",
                error=f"Account ID {job.account_id} not found"
            )
            return

        # Check if account is active
        if not account.is_active:
            self.job_queue.update_status(
                job_id=job_id,
                status="failed",
                error=f"Account '{account.name}' is inactive. Please activate it or select a different account."
            )
            return

        # Check quota before starting
        quota_needed = 1600
        quota_available = account.daily_quota - account.quota_used
        if quota_available < quota_needed:
            self.job_queue.update_status(
                job_id=job_id,
                status="failed",
                error=f"Account '{account.name}' doesn't have enough quota. Available: {quota_available}, needed: {quota_needed}"
            )
            return

        # Mark as running
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
                # Check if video was rendered (for manual upload option)
                stages_completed = result["stages_completed"] if result["stages_completed"] else []
                last_stage = stages_completed[-1] if stages_completed else None

                # Mark as failed but preserve stage info for manual upload
                self.job_queue.update_status(
                    job_id=job_id,
                    status="failed",
                    stage=last_stage,
                    error=result["error"]
                )

        except YouTubeQuotaExceeded as exc:
            # Quota exceeded during upload - mark as failed but video is available for manual upload
            # The stage should be 'render' (last successful stage before upload)
            self.job_queue.update_status(
                job_id=job_id,
                status="failed",
                stage="render",  # Video was rendered successfully
                error=f"YouTube quota exceeded for account '{account.name}'. Video is available for manual upload."
            )

        except Exception as exc:
            # Other errors - mark job as failed
            self.job_queue.update_status(
                job_id=job_id,
                status="failed",
                error=str(exc)
            )
