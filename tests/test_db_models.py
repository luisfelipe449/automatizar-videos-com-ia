"""Tests for database models and session management."""
import pytest
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta

from app.db.database import Base, get_engine, get_session
from app.db.models import Account, Job, Upload, QuotaSnapshot
from datetime import date


def test_get_engine_creates_sqlite_engine(tmp_path):
    """Engine should connect to SQLite database."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")

    assert engine is not None
    assert "sqlite" in str(engine.url)


def test_get_session_yields_session(tmp_path):
    """get_session should yield a SQLAlchemy Session."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    with get_session(engine) as session:
        assert isinstance(session, Session)


def test_account_model_creates_with_required_fields(tmp_path):
    """Account should be creatable with all required fields."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    reset_time = datetime.now(timezone.utc) + timedelta(hours=24)

    with get_session(engine) as session:
        account = Account(
            name="Test Account",
            email="test@example.com",
            client_id="client123",
            client_secret="secret123",
            refresh_token="refresh123",
            quota_reset_at=reset_time
        )
        session.add(account)
        session.commit()

        assert account.id is not None
        assert account.name == "Test Account"
        assert account.daily_quota == 10000  # default
        assert account.quota_used == 0  # default
        assert account.is_active is True  # default


def test_job_model_creates_with_required_fields(tmp_path):
    """Job should be creatable with required fields."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    with get_session(engine) as session:
        job = Job(
            job_id="2026-05-01_test_abc123",
            prompt="Test prompt",
            status="pending"
        )
        session.add(job)
        session.commit()

        assert job.id is not None
        assert job.job_id == "2026-05-01_test_abc123"
        assert job.status == "pending"
        assert job.priority == 0  # default


def test_upload_model_creates_with_required_fields(tmp_path):
    """Upload should be creatable with required fields."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    reset_time = datetime.now(timezone.utc) + timedelta(hours=24)

    with get_session(engine) as session:
        # Create account and job first (foreign keys)
        account = Account(
            name="Test", email="test@example.com",
            client_id="c", client_secret="s", refresh_token="r",
            quota_reset_at=reset_time
        )
        job = Job(job_id="job123", prompt="test", status="completed")
        session.add_all([account, job])
        session.commit()

        upload = Upload(
            job_id=job.job_id,
            account_id=account.id,
            video_id="yt_video_123",
            title="Test Video",
            privacy_status="private"
        )
        session.add(upload)
        session.commit()

        assert upload.id is not None
        assert upload.video_id == "yt_video_123"
        assert upload.views == 0  # default


def test_quota_snapshot_model_creates_with_required_fields(tmp_path):
    """QuotaSnapshot should be creatable with required fields."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    reset_time = datetime.now(timezone.utc) + timedelta(hours=24)

    with get_session(engine) as session:
        account = Account(
            name="Test", email="test@example.com",
            client_id="c", client_secret="s", refresh_token="r",
            quota_reset_at=reset_time
        )
        session.add(account)
        session.commit()

        snapshot = QuotaSnapshot(
            account_id=account.id,
            date=date(2026, 5, 1),
            quota_used=3200,
            uploads_count=2
        )
        session.add(snapshot)
        session.commit()

        assert snapshot.id is not None
        assert snapshot.quota_used == 3200
        assert snapshot.uploads_count == 2
