"""Tests for AnalyticsAggregator."""
import pytest
from datetime import datetime, timezone, timedelta

from app.core.analytics import AnalyticsAggregator
from app.db.database import Base, get_engine, get_session
from app.db.models import Account, Job, Upload


@pytest.fixture
def test_db(tmp_path):
    """Create test database."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


def test_get_account_stats_aggregates_uploads_and_views(test_db):
    """get_account_stats should return upload count and total views per account."""
    reset_time = datetime.now(timezone.utc) + timedelta(hours=24)

    with get_session(test_db) as session:
        # Create accounts
        acc1 = Account(name="Account1", email="acc1@example.com", client_id="c1",
                       client_secret="s1", refresh_token="r1", quota_reset_at=reset_time)
        acc2 = Account(name="Account2", email="acc2@example.com", client_id="c2",
                       client_secret="s2", refresh_token="r2", quota_reset_at=reset_time)
        session.add_all([acc1, acc2])
        session.commit()

        # Create jobs
        job1 = Job(job_id="job1", account_id=acc1.id, prompt="Test 1", status="completed")
        job2 = Job(job_id="job2", account_id=acc1.id, prompt="Test 2", status="completed")
        job3 = Job(job_id="job3", account_id=acc2.id, prompt="Test 3", status="completed")
        session.add_all([job1, job2, job3])
        session.commit()

        # Create uploads with views
        upload1 = Upload(job_id="job1", account_id=acc1.id, video_id="vid1",
                         title="Video 1", privacy_status="public", views=100)
        upload2 = Upload(job_id="job2", account_id=acc1.id, video_id="vid2",
                         title="Video 2", privacy_status="public", views=250)
        upload3 = Upload(job_id="job3", account_id=acc2.id, video_id="vid3",
                         title="Video 3", privacy_status="public", views=50)
        session.add_all([upload1, upload2, upload3])
        session.commit()

        aggregator = AnalyticsAggregator(session)
        stats = aggregator.get_account_stats()

        # Should return list of dicts with account info and aggregated data
        assert len(stats) == 2

        # Find stats for each account
        acc1_stats = next(s for s in stats if s["account_id"] == acc1.id)
        acc2_stats = next(s for s in stats if s["account_id"] == acc2.id)

        # Account 1: 2 uploads, 350 total views
        assert acc1_stats["account_name"] == "Account1"
        assert acc1_stats["upload_count"] == 2
        assert acc1_stats["total_views"] == 350

        # Account 2: 1 upload, 50 total views
        assert acc2_stats["account_name"] == "Account2"
        assert acc2_stats["upload_count"] == 1
        assert acc2_stats["total_views"] == 50


def test_get_daily_summary_counts_jobs_by_date(test_db):
    """get_daily_summary should count jobs grouped by date."""
    with get_session(test_db) as session:
        # Create jobs on different dates
        today = datetime.now(timezone.utc)
        yesterday = today - timedelta(days=1)
        two_days_ago = today - timedelta(days=2)

        job1 = Job(job_id="job1", prompt="Test 1", status="completed", created_at=two_days_ago)
        job2 = Job(job_id="job2", prompt="Test 2", status="completed", created_at=yesterday)
        job3 = Job(job_id="job3", prompt="Test 3", status="completed", created_at=yesterday)
        job4 = Job(job_id="job4", prompt="Test 4", status="pending", created_at=today)
        job5 = Job(job_id="job5", prompt="Test 5", status="failed", created_at=today)
        session.add_all([job1, job2, job3, job4, job5])
        session.commit()

        aggregator = AnalyticsAggregator(session)
        summary = aggregator.get_daily_summary(days=3)

        # Should return list of dicts with date and counts
        assert len(summary) == 3

        # Find summary for each date
        today_summary = next(s for s in summary if s["date"] == today.date())
        yesterday_summary = next(s for s in summary if s["date"] == yesterday.date())
        two_days_ago_summary = next(s for s in summary if s["date"] == two_days_ago.date())

        # Two days ago: 1 completed
        assert two_days_ago_summary["total_jobs"] == 1
        assert two_days_ago_summary["completed_jobs"] == 1
        assert two_days_ago_summary["failed_jobs"] == 0
        assert two_days_ago_summary["pending_jobs"] == 0

        # Yesterday: 2 completed
        assert yesterday_summary["total_jobs"] == 2
        assert yesterday_summary["completed_jobs"] == 2
        assert yesterday_summary["failed_jobs"] == 0
        assert yesterday_summary["pending_jobs"] == 0

        # Today: 1 pending, 1 failed
        assert today_summary["total_jobs"] == 2
        assert today_summary["completed_jobs"] == 0
        assert today_summary["failed_jobs"] == 1
        assert today_summary["pending_jobs"] == 1


def test_get_account_stats_handles_empty_database(test_db):
    """get_account_stats should return empty list when no accounts exist."""
    with get_session(test_db) as session:
        aggregator = AnalyticsAggregator(session)
        stats = aggregator.get_account_stats()

        assert stats == []


def test_get_daily_summary_handles_empty_database(test_db):
    """get_daily_summary should return empty list when no jobs exist."""
    with get_session(test_db) as session:
        aggregator = AnalyticsAggregator(session)
        summary = aggregator.get_daily_summary(days=7)

        assert summary == []
