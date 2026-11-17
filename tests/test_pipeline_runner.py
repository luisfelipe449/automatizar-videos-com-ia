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


def _make_account(session, *, name: str, quota_used: int = 0, is_active: bool = True) -> int:
    account = Account(
        name=name,
        email=f"{name.lower().replace(' ', '')}@example.com",
        client_id=f"client-{name}",
        client_secret=f"secret-{name}",
        refresh_token=f"refresh-{name}",
        daily_quota=10000,
        quota_used=quota_used,
        quota_reset_at=datetime.now(timezone.utc) + timedelta(hours=24),
        is_active=is_active,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account.id


def _enqueue(test_db, account_id: int | None) -> str:
    with get_session(test_db) as session:
        return JobQueueManager(session).enqueue(
            prompt="Test video about cats", account_id=account_id
        ).job_id


@pytest.fixture
def pending_job(test_db, account_with_quota):
    """Pending job assigned to `account_with_quota` — accounts are chosen at
    enqueue time; the runner never picks one itself."""
    return _enqueue(test_db, account_with_quota)


def test_run_job_uses_assigned_account_and_updates_status(test_db, account_with_quota, pending_job):
    """run_job should run with the job's account and record completion."""
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

            # Verify job kept its account and completed
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


def test_quota_exceeded_during_upload_leaves_video_for_manual_upload(
    test_db, account_with_quota, pending_job
):
    """A YouTube quota error fails the job at `render` so the rendered video
    can be uploaded by hand, and consumes no quota."""
    with patch("app.core.pipeline_runner.run_pipeline") as mock_pipeline:
        mock_pipeline.side_effect = YouTubeQuotaExceeded("quotaExceeded")

        with get_session(test_db) as session:
            PipelineRunner(session).run_job(pending_job)

            job = session.query(Job).filter_by(job_id=pending_job).first()
            assert job.status == "failed"
            assert job.stage == "render"
            assert job.account_id == account_with_quota
            assert "manual upload" in job.error_message.lower()

            account = session.query(Account).filter_by(id=account_with_quota).first()
            assert account.quota_used == 0


def test_run_job_fails_when_job_has_no_account(test_db):
    """Jobs without an account fail immediately instead of borrowing one."""
    with get_session(test_db) as session:
        _make_account(session, name="Idle Account")  # available, but must not be used
    job_id = _enqueue(test_db, account_id=None)

    with patch("app.core.pipeline_runner.run_pipeline") as mock_pipeline:
        with get_session(test_db) as session:
            PipelineRunner(session).run_job(job_id)

            job = session.query(Job).filter_by(job_id=job_id).first()
            assert job.status == "failed"
            assert job.account_id is None
            assert "no account assigned" in job.error_message.lower()
        mock_pipeline.assert_not_called()


def test_run_job_fails_when_account_lacks_quota(test_db):
    """An account below one upload's worth of quota fails the job up front."""
    with get_session(test_db) as session:
        acc_id = _make_account(session, name="Exhausted Account", quota_used=9000)
    job_id = _enqueue(test_db, acc_id)

    with patch("app.core.pipeline_runner.run_pipeline") as mock_pipeline:
        with get_session(test_db) as session:
            PipelineRunner(session).run_job(job_id)

            job = session.query(Job).filter_by(job_id=job_id).first()
            assert job.status == "failed"
            assert "doesn't have enough quota" in job.error_message
        mock_pipeline.assert_not_called()


def test_run_job_fails_when_account_inactive(test_db):
    """A deactivated account fails the job up front."""
    with get_session(test_db) as session:
        acc_id = _make_account(session, name="Paused Account", is_active=False)
    job_id = _enqueue(test_db, acc_id)

    with patch("app.core.pipeline_runner.run_pipeline") as mock_pipeline:
        with get_session(test_db) as session:
            PipelineRunner(session).run_job(job_id)

            job = session.query(Job).filter_by(job_id=job_id).first()
            assert job.status == "failed"
            assert "inactive" in job.error_message
        mock_pipeline.assert_not_called()


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
