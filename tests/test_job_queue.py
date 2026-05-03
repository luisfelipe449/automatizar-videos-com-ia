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


def test_reset_orphan_running_resets_only_given_account(test_db):
    """reset_orphan_running(N) deve resetar só jobs com account_id=N em running."""
    with get_session(test_db) as session:
        acc_a = Account(
            name="A", email="a@x", client_id="ca", client_secret="sa",
            refresh_token="ra", quota_reset_at=datetime.now(timezone.utc),
        )
        acc_b = Account(
            name="B", email="b@x", client_id="cb", client_secret="sb",
            refresh_token="rb", quota_reset_at=datetime.now(timezone.utc),
        )
        session.add_all([acc_a, acc_b])
        session.commit()

        running_a = Job(
            job_id="job-a-running", account_id=acc_a.id, prompt="a1",
            status="running", started_at=datetime.now(timezone.utc),
        )
        pending_a = Job(
            job_id="job-a-pending", account_id=acc_a.id, prompt="a2",
            status="pending",
        )
        running_b = Job(
            job_id="job-b-running", account_id=acc_b.id, prompt="b1",
            status="running", started_at=datetime.now(timezone.utc),
        )
        session.add_all([running_a, pending_a, running_b])
        session.commit()

        acc_a_id = acc_a.id

        manager = JobQueueManager(session)
        count = manager.reset_orphan_running(account_id=acc_a_id)

        assert count == 1

        # Re-fetch to verify state
        running_a_after = session.query(Job).filter_by(job_id="job-a-running").one()
        running_b_after = session.query(Job).filter_by(job_id="job-b-running").one()
        pending_a_after = session.query(Job).filter_by(job_id="job-a-pending").one()
        assert running_a_after.status == "pending"
        assert running_a_after.started_at is None
        assert running_b_after.status == "running"  # not touched — different account
        assert pending_a_after.status == "pending"  # not touched — was already pending


def test_reset_orphan_running_is_idempotent(test_db):
    """Chamar duas vezes: primeira reseta, segunda retorna 0."""
    with get_session(test_db) as session:
        acc = Account(
            name="C", email="c@x", client_id="cc", client_secret="sc",
            refresh_token="rc", quota_reset_at=datetime.now(timezone.utc),
        )
        session.add(acc)
        session.commit()

        job = Job(
            job_id="job-c", account_id=acc.id, prompt="c",
            status="running", started_at=datetime.now(timezone.utc),
        )
        session.add(job)
        session.commit()

        acc_id = acc.id

        manager = JobQueueManager(session)
        assert manager.reset_orphan_running(account_id=acc_id) == 1
        assert manager.reset_orphan_running(account_id=acc_id) == 0


def test_claim_next_pending_marks_job_running_and_returns_it(test_db):
    """Claim atômico: retorna o job e muda status pra 'running' no mesmo passo."""
    with get_session(test_db) as session:
        acc = Account(
            name="A", email="a@x", client_id="ca", client_secret="sa",
            refresh_token="ra", quota_reset_at=datetime.now(timezone.utc),
        )
        session.add(acc)
        session.commit()

        job = Job(
            job_id="job-1", account_id=acc.id, prompt="p",
            status="pending", priority=0,
        )
        session.add(job)
        session.commit()

        acc_id = acc.id

        manager = JobQueueManager(session)
        claimed = manager.claim_next_pending(account_id=acc_id)

        assert claimed is not None
        assert claimed.job_id == "job-1"
        assert claimed.status == "running"
        assert claimed.started_at is not None


def test_claim_next_pending_returns_none_when_queue_empty(test_db):
    """Sem jobs pendentes pra conta → retorna None."""
    with get_session(test_db) as session:
        acc = Account(
            name="A", email="a@x", client_id="ca", client_secret="sa",
            refresh_token="ra", quota_reset_at=datetime.now(timezone.utc),
        )
        session.add(acc)
        session.commit()
        acc_id = acc.id

        manager = JobQueueManager(session)
        assert manager.claim_next_pending(account_id=acc_id) is None


def test_claim_next_pending_respects_account_isolation(test_db):
    """Worker da conta A nunca pega job da conta B."""
    with get_session(test_db) as session:
        acc_a = Account(
            name="A", email="a@x", client_id="ca", client_secret="sa",
            refresh_token="ra", quota_reset_at=datetime.now(timezone.utc),
        )
        acc_b = Account(
            name="B", email="b@x", client_id="cb", client_secret="sb",
            refresh_token="rb", quota_reset_at=datetime.now(timezone.utc),
        )
        session.add_all([acc_a, acc_b])
        session.commit()

        job_b = Job(job_id="job-b", account_id=acc_b.id, prompt="p", status="pending")
        session.add(job_b)
        session.commit()

        acc_a_id = acc_a.id

        manager = JobQueueManager(session)
        assert manager.claim_next_pending(account_id=acc_a_id) is None

        job_b_after = session.query(Job).filter_by(job_id="job-b").one()
        assert job_b_after.status == "pending"


def test_claim_next_pending_orders_by_priority_then_created_at(test_db):
    """Prioridade maior primeiro; em empate, mais antigo primeiro."""
    with get_session(test_db) as session:
        acc = Account(
            name="A", email="a@x", client_id="ca", client_secret="sa",
            refresh_token="ra", quota_reset_at=datetime.now(timezone.utc),
        )
        session.add(acc)
        session.commit()

        now = datetime.now(timezone.utc)
        low_old = Job(
            job_id="low-old", account_id=acc.id, prompt="p", status="pending",
            priority=0, created_at=now - timedelta(minutes=10),
        )
        high_new = Job(
            job_id="high-new", account_id=acc.id, prompt="p", status="pending",
            priority=5, created_at=now,
        )
        session.add_all([low_old, high_new])
        session.commit()

        acc_id = acc.id

        manager = JobQueueManager(session)
        claimed = manager.claim_next_pending(account_id=acc_id)
        assert claimed.job_id == "high-new"


def test_claim_next_pending_is_atomic_under_concurrency(tmp_path):
    """Duas threads claim simultâneo em 1 job pendente: só 1 ganha."""
    import threading
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from app.db.database import _apply_sqlite_pragmas

    # File-backed SQLite so threads share state.
    db_path = tmp_path / "concurrent.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    Base.metadata.create_all(engine)

    Session = sessionmaker(bind=engine)

    # Seed: one account, one pending job.
    seed = Session()
    acc = Account(
        name="A", email="a@x", client_id="ca", client_secret="sa",
        refresh_token="ra", quota_reset_at=datetime.now(timezone.utc),
    )
    seed.add(acc)
    seed.commit()
    seed.add(Job(job_id="only", account_id=acc.id, prompt="p", status="pending"))
    seed.commit()
    account_id = acc.id
    seed.close()

    # Barrier so both threads fire the UPDATE at (almost) the same instant.
    barrier = threading.Barrier(2)
    results: list = [None, None]

    def worker(idx: int) -> None:
        session = Session()
        try:
            mgr = JobQueueManager(session)
            barrier.wait(timeout=5)
            results[idx] = mgr.claim_next_pending(account_id=account_id)
        finally:
            session.close()

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Exactly one won.
    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1, f"expected 1 winner, got {len(winners)}"
    assert len(losers) == 1, f"expected 1 loser, got {len(losers)}"
    assert winners[0].job_id == "only"
    assert winners[0].status == "running"
