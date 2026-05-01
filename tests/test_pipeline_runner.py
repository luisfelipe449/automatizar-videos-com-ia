"""Tests for PipelineRunner."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch, call
from pathlib import Path

from app.core.pipeline_runner import PipelineRunner, PipelineRunnerError
from app.services.youtube_service import YouTubeQuotaExceeded
from app.core.account_manager import AccountManager
from app.core.job_queue import JobQueueManager
from app.db.database import Base, get_engine, get_session
from app.db.models import Account, Job


@pytest.fixture
def test_db(tmp_path):
    """Create test database."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def account_with_quota(test_db):
    """Create account with available quota."""
    with get_session(test_db) as session:
        account = Account(
            name="Test Account",
            email="test@example.com",
            client_id="client123",
            client_secret="secret123",
            refresh_token="refresh123",
            daily_quota=10000,
            quota_used=0,
            quota_reset_at=datetime.now(timezone.utc) + timedelta(hours=24),
            is_active=True
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        return account.id


@pytest.fixture
def pending_job(test_db):
    """Create pending job without account assignment."""
    with get_session(test_db) as session:
        job_queue = JobQueueManager(session)
        job = job_queue.enqueue(prompt="Test video about cats")
        return job.job_id


def test_run_job_assigns_account_and_updates_status(test_db, account_with_quota, pending_job):
    """run_job should assign available account and update job status."""
    with patch("app.core.pipeline_runner.run_pipeline") as mock_pipeline:
        mock_pipeline.return_value = {
            "success": True,
            "video_id": "abc123",
            "stages_completed": ["script", "tts", "captions", "clips", "render", "upload"],
            "error": None
        }

        with get_session(test_db) as session:
            runner = PipelineRunner(session)
            runner.run_job(pending_job)

            # Verify job was assigned account and marked running
            job = session.query(Job).filter_by(job_id=pending_job).first()
            assert job.account_id == account_with_quota
            assert job.status == "completed"
            assert job.started_at is not None
            assert job.completed_at is not None

            # Verify orchestrator was called with account
            mock_pipeline.assert_called_once()
            call_kwargs = mock_pipeline.call_args.kwargs
            assert call_kwargs["prompt"] == "Test video about cats"
            assert call_kwargs["job_id"] == pending_job
            assert "account" in call_kwargs


def test_run_job_consumes_quota_on_success(test_db, account_with_quota, pending_job):
    """run_job should consume quota after successful upload."""
    with patch("app.core.pipeline_runner.run_pipeline") as mock_pipeline:
        mock_pipeline.return_value = {
            "success": True,
            "video_id": "abc123",
            "stages_completed": ["script", "tts", "captions", "clips", "render", "upload"],
            "error": None
        }

        with get_session(test_db) as session:
            runner = PipelineRunner(session)
            runner.run_job(pending_job)

            # Verify quota was consumed
            account = session.query(Account).filter_by(id=account_with_quota).first()
            assert account.quota_used == 1600  # Default video upload cost


def test_run_job_updates_stages_during_execution(test_db, account_with_quota, pending_job):
    """run_job should update job stage as pipeline progresses."""
    with patch("app.core.pipeline_runner.run_pipeline") as mock_pipeline:
        # Simulate pipeline returning partial completion
        mock_pipeline.return_value = {
            "success": True,
            "video_id": "abc123",
            "stages_completed": ["script", "tts", "captions", "clips", "render", "upload"],
            "error": None
        }

        with get_session(test_db) as session:
            runner = PipelineRunner(session)
            runner.run_job(pending_job)

            # Verify final stage was recorded
            job = session.query(Job).filter_by(job_id=pending_job).first()
            assert job.stage == "upload"
            assert job.status == "completed"


def test_quota_exceeded_resets_job_with_different_account(test_db, pending_job):
    """run_job should reset job to pending with different account on quota exceeded."""
    # Create two accounts
    with get_session(test_db) as session:
        account1 = Account(
            name="Account 1",
            email="acc1@example.com",
            client_id="client1",
            client_secret="secret1",
            refresh_token="refresh1",
            daily_quota=10000,
            quota_used=8500,  # Close to quota limit
            quota_reset_at=datetime.now(timezone.utc) + timedelta(hours=24),
            is_active=True
        )
        account2 = Account(
            name="Account 2",
            email="acc2@example.com",
            client_id="client2",
            client_secret="secret2",
            refresh_token="refresh2",
            daily_quota=10000,
            quota_used=0,  # Has plenty of quota
            quota_reset_at=datetime.now(timezone.utc) + timedelta(hours=24),
            is_active=True
        )
        session.add_all([account1, account2])
        session.commit()
        session.refresh(account1)
        session.refresh(account2)
        acc1_id = account1.id
        acc2_id = account2.id

    # First attempt with account1 fails with quota exceeded
    # Second attempt with account2 succeeds
    with patch("app.core.pipeline_runner.run_pipeline") as mock_pipeline:
        # First call raises quota exceeded
        mock_pipeline.side_effect = [
            YouTubeQuotaExceeded("quotaExceeded"),
            {
                "success": True,
                "video_id": "abc123",
                "stages_completed": ["script", "tts", "captions", "clips", "render", "upload"],
                "error": None
            }
        ]

        with get_session(test_db) as session:
            runner = PipelineRunner(session)

            # First attempt
            runner.run_job(pending_job)

            # Verify job was reset to pending after quota exceeded
            job = session.query(Job).filter_by(job_id=pending_job).first()
            assert job.status == "pending"
            assert job.account_id is None  # Unassigned

            # Second attempt with different account
            runner.run_job(pending_job)

            # Verify job completed with second account
            job = session.query(Job).filter_by(job_id=pending_job).first()
            assert job.status == "completed"
            assert job.account_id == acc2_id  # Different account


def test_run_job_handles_no_available_account(test_db, pending_job):
    """run_job should mark job as failed when no account available."""
    # Create account with exhausted quota
    with get_session(test_db) as session:
        account = Account(
            name="Exhausted Account",
            email="exhausted@example.com",
            client_id="client_exhausted",
            client_secret="secret_exhausted",
            refresh_token="refresh_exhausted",
            daily_quota=10000,
            quota_used=10000,  # Fully exhausted
            quota_reset_at=datetime.now(timezone.utc) + timedelta(hours=24),
            is_active=True
        )
        session.add(account)
        session.commit()

    with get_session(test_db) as session:
        runner = PipelineRunner(session)
        runner.run_job(pending_job)

        # Verify job was marked as failed
        job = session.query(Job).filter_by(job_id=pending_job).first()
        assert job.status == "failed"
        assert "no available account" in job.error_message.lower()


def test_run_job_handles_pipeline_failure(test_db, account_with_quota, pending_job):
    """run_job should mark job as failed when pipeline fails."""
    with patch("app.core.pipeline_runner.run_pipeline") as mock_pipeline:
        mock_pipeline.side_effect = RuntimeError("Pipeline stage failed")

        with get_session(test_db) as session:
            runner = PipelineRunner(session)
            runner.run_job(pending_job)

            # Verify job was marked as failed
            job = session.query(Job).filter_by(job_id=pending_job).first()
            assert job.status == "failed"
            assert "Pipeline stage failed" in job.error_message


def test_run_job_handles_job_not_found(test_db):
    """run_job should raise error for non-existent job."""
    with get_session(test_db) as session:
        runner = PipelineRunner(session)

        with pytest.raises(PipelineRunnerError, match="Job .* not found"):
            runner.run_job("non-existent-job-id")


def test_run_job_marks_completed_creates_upload_record(test_db, account_with_quota, pending_job):
    """run_job should create upload record on successful completion."""
    with patch("app.core.pipeline_runner.run_pipeline") as mock_pipeline:
        mock_pipeline.return_value = {
            "success": True,
            "video_id": "xyz789",
            "stages_completed": ["script", "tts", "captions", "clips", "render", "upload"],
            "error": None
        }

        with get_session(test_db) as session:
            runner = PipelineRunner(session)
            runner.run_job(pending_job)

            # Verify job completed
            job = session.query(Job).filter_by(job_id=pending_job).first()
            assert job.status == "completed"

            # Verify upload record exists
            from app.db.models import Upload
            upload = session.query(Upload).filter_by(job_id=pending_job).first()
            assert upload is not None
            assert upload.video_id == "xyz789"
            assert upload.account_id == account_with_quota


def test_run_job_partial_failure_marks_as_failed(test_db, account_with_quota, pending_job):
    """run_job should mark job as failed when pipeline returns success=False."""
    with patch("app.core.pipeline_runner.run_pipeline") as mock_pipeline:
        mock_pipeline.return_value = {
            "success": False,
            "video_id": None,
            "stages_completed": ["script", "tts"],
            "error": "TTS generation failed"
        }

        with get_session(test_db) as session:
            runner = PipelineRunner(session)
            runner.run_job(pending_job)

            # Verify job was marked as failed
            job = session.query(Job).filter_by(job_id=pending_job).first()
            assert job.status == "failed"
            assert job.error_message == "TTS generation failed"
            assert job.stage == "tts"  # Last completed stage


def test_run_job_does_not_consume_quota_on_failure(test_db, account_with_quota, pending_job):
    """run_job should not consume quota if upload fails."""
    with patch("app.core.pipeline_runner.run_pipeline") as mock_pipeline:
        mock_pipeline.return_value = {
            "success": False,
            "video_id": None,
            "stages_completed": ["script", "tts", "captions", "clips", "render"],
            "error": "Upload failed"
        }

        with get_session(test_db) as session:
            runner = PipelineRunner(session)
            runner.run_job(pending_job)

            # Verify quota was NOT consumed
            account = session.query(Account).filter_by(id=account_with_quota).first()
            assert account.quota_used == 0
