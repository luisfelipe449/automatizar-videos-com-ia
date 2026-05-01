"""Tests for AccountManager."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch
from pathlib import Path

from app.core.account_manager import AccountManager, AccountManagerError
from app.db.database import Base, get_engine, get_session
from app.db.models import Account


@pytest.fixture
def test_db(tmp_path):
    """Create test database."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


def test_add_account_creates_account_with_valid_token(test_db):
    """add_account should create account when token is valid."""
    with patch("app.services.youtube_service.verify_credentials") as mock_verify:
        mock_verify.return_value = {"token_refreshed": True, "scopes": ["youtube.upload"]}

        with get_session(test_db) as session:
            manager = AccountManager(session)

            account = manager.add_account(
                name="Test Account",
                email="test@example.com",
                client_id="client123",
                client_secret="secret123",
                refresh_token="refresh123"
            )

            assert account.id is not None
            assert account.name == "Test Account"
            assert account.email == "test@example.com"
            assert account.is_active is True
            assert account.daily_quota == 10000
            assert account.quota_used == 0

            # Verify verify_credentials was called
            mock_verify.assert_called_once()


def test_add_account_raises_error_on_duplicate_name(test_db):
    """add_account should raise error if name already exists."""
    with patch("app.services.youtube_service.verify_credentials") as mock_verify:
        mock_verify.return_value = {"token_refreshed": True}

        with get_session(test_db) as session:
            manager = AccountManager(session)

            manager.add_account("Duplicate", "a@example.com", "c", "s", "r")

            with pytest.raises(AccountManagerError, match="already exists"):
                manager.add_account("Duplicate", "b@example.com", "c2", "s2", "r2")


def test_get_available_account_returns_account_with_most_quota(test_db):
    """get_available_account should return account with highest remaining quota."""
    reset_time = datetime.now(timezone.utc) + timedelta(hours=24)

    with get_session(test_db) as session:
        # Create 3 accounts with different quota usage
        acc1 = Account(name="Low", email="low@example.com", client_id="c", client_secret="s",
                       refresh_token="r", quota_used=8000, quota_reset_at=reset_time)
        acc2 = Account(name="High", email="high@example.com", client_id="c", client_secret="s",
                       refresh_token="r", quota_used=1000, quota_reset_at=reset_time)
        acc3 = Account(name="Medium", email="med@example.com", client_id="c", client_secret="s",
                       refresh_token="r", quota_used=5000, quota_reset_at=reset_time)
        session.add_all([acc1, acc2, acc3])
        session.commit()

        manager = AccountManager(session)
        available = manager.get_available_account()

        assert available is not None
        assert available.name == "High"  # 10000 - 1000 = 9000 remaining


def test_get_available_account_returns_none_if_all_exhausted(test_db):
    """get_available_account should return None if all accounts exhausted."""
    reset_time = datetime.now(timezone.utc) + timedelta(hours=24)

    with get_session(test_db) as session:
        # Account with not enough quota (needs 1600 for upload)
        acc = Account(name="Exhausted", email="ex@example.com", client_id="c", client_secret="s",
                      refresh_token="r", quota_used=8500, quota_reset_at=reset_time)
        session.add(acc)
        session.commit()

        manager = AccountManager(session)
        available = manager.get_available_account()

        assert available is None


def test_consume_quota_increments_quota_used(test_db):
    """consume_quota should increment quota_used by specified amount."""
    reset_time = datetime.now(timezone.utc) + timedelta(hours=24)

    with get_session(test_db) as session:
        acc = Account(name="Test", email="test@example.com", client_id="c", client_secret="s",
                      refresh_token="r", quota_used=0, quota_reset_at=reset_time)
        session.add(acc)
        session.commit()
        account_id = acc.id

        manager = AccountManager(session)
        manager.consume_quota(account_id, 1600)

        session.refresh(acc)
        assert acc.quota_used == 1600


def test_reset_daily_quotas_resets_accounts_past_reset_time(test_db):
    """reset_daily_quotas should reset quota_used for accounts past reset time."""
    past_time = datetime.now(timezone.utc) - timedelta(hours=1)
    future_time = datetime.now(timezone.utc) + timedelta(hours=23)

    with get_session(test_db) as session:
        acc1 = Account(name="Past", email="past@example.com", client_id="c", client_secret="s",
                       refresh_token="r", quota_used=5000, quota_reset_at=past_time)
        acc2 = Account(name="Future", email="future@example.com", client_id="c", client_secret="s",
                       refresh_token="r", quota_used=3000, quota_reset_at=future_time)
        session.add_all([acc1, acc2])
        session.commit()

        manager = AccountManager(session)
        reset_count = manager.reset_daily_quotas()

        assert reset_count == 1
        session.refresh(acc1)
        session.refresh(acc2)
        assert acc1.quota_used == 0  # reset
        assert acc2.quota_used == 3000  # not reset


def test_toggle_active_switches_is_active_flag(test_db):
    """toggle_active should flip is_active boolean."""
    reset_time = datetime.now(timezone.utc) + timedelta(hours=24)

    with get_session(test_db) as session:
        acc = Account(name="Toggle", email="toggle@example.com", client_id="c", client_secret="s",
                      refresh_token="r", is_active=True, quota_reset_at=reset_time)
        session.add(acc)
        session.commit()
        account_id = acc.id

        manager = AccountManager(session)

        # Toggle off
        result = manager.toggle_active(account_id)
        assert result.is_active is False

        # Toggle on
        result = manager.toggle_active(account_id)
        assert result.is_active is True
