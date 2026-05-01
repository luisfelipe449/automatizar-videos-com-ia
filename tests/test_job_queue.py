"""Tests for JobQueueManager."""
import pytest
from datetime import datetime, timezone, timedelta

from app.core.job_queue import JobQueueManager, JobQueueError
from app.db.database import Base, get_engine, get_session
from app.db.models import Account, Job, Upload


@pytest.fixture
def test_db(tmp_path):
    """Create test database."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


def test_enqueue_creates_pending_job(test_db):
    """enqueue should create job with status=pending."""
    with get_session(test_db) as session:
        manager = JobQueueManager(session)

        job = manager.enqueue(prompt="Test prompt", priority=5)

        assert job.id is not None
        assert job.job_id.startswith("2026-")  # YYYY-MM-DD format
        assert job.prompt == "Test prompt"
        assert job.status == "pending"
        assert job.priority == 5
        assert job.account_id is None  # not assigned yet


def test_get_next_pending_respects_priority(test_db):
    """get_next_pending should return highest priority job."""
    with get_session(test_db) as session:
        manager = JobQueueManager(session)

        # Enqueue jobs with different priorities
        job1 = manager.enqueue("Low priority", priority=1)
        job2 = manager.enqueue("High priority", priority=10)
        job3 = manager.enqueue("Medium priority", priority=5)

        next_job = manager.get_next_pending()

        assert next_job.job_id == job2.job_id  # highest priority


def test_get_next_pending_returns_none_if_empty(test_db):
    """get_next_pending should return None if no pending jobs."""
    with get_session(test_db) as session:
        manager = JobQueueManager(session)

        next_job = manager.get_next_pending()

        assert next_job is None


def test_update_status_transitions_correctly(test_db):
    """update_status should update job fields."""
    with get_session(test_db) as session:
        manager = JobQueueManager(session)
        job = manager.enqueue("Test")

        # pending → running
        manager.update_status(job.job_id, status="running", stage="script")
        session.refresh(job)
        assert job.status == "running"
        assert job.stage == "script"
        assert job.started_at is not None

        # running → completed
        manager.update_status(job.job_id, status="completed", stage="upload")
        session.refresh(job)
        assert job.status == "completed"
        assert job.stage == "upload"
        assert job.completed_at is not None


def test_mark_completed_creates_upload_record(test_db):
    """mark_completed should create Upload record."""
    reset_time = datetime.now(timezone.utc) + timedelta(hours=24)

    with get_session(test_db) as session:
        # Create account and job
        acc = Account(name="Test", email="test@example.com", client_id="c", client_secret="s",
                      refresh_token="r", quota_reset_at=reset_time)
        session.add(acc)
        session.commit()

        manager = JobQueueManager(session)
        job = manager.enqueue("Test", account_id=acc.id)
        job.status = "running"  # simulate processing
        session.commit()

        # Mark completed
        manager.mark_completed(job.job_id, video_id="yt_abc123")

        # Verify job updated
        session.refresh(job)
        assert job.status == "completed"
        assert job.completed_at is not None

        # Verify upload record created
        upload = session.query(Upload).filter_by(job_id=job.job_id).first()
        assert upload is not None
        assert upload.video_id == "yt_abc123"
        assert upload.account_id == acc.id


def test_retry_failed_job_resets_to_pending(test_db):
    """retry_failed_job should reset failed job to pending."""
    with get_session(test_db) as session:
        manager = JobQueueManager(session)
        job = manager.enqueue("Test")

        # Simulate failure
        manager.update_status(job.job_id, status="failed", error="Simulated error")
        session.refresh(job)
        assert job.status == "failed"

        # Retry
        retried_job = manager.retry_failed_job(job.job_id)

        assert retried_job.status == "pending"
        assert retried_job.error_message is None
        assert retried_job.account_id is None
        assert retried_job.started_at is None
