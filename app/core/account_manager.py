"""Account management with CRUD operations and quota tracking."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session

from app.db.models import Account
from app.services import youtube_service


class AccountManagerError(Exception):
    """Raised when account operations fail."""


class AccountManager:
    """Manages YouTube accounts: CRUD, quota tracking, selection."""

    def __init__(self, session: Session):
        """Initialize manager with database session.

        Args:
            session: SQLAlchemy session
        """
        self.session = session

    def add_account(
        self,
        name: str,
        email: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        daily_quota: int = 10000
    ) -> Account:
        """Add new YouTube account after validating credentials.

        Args:
            name: Account display name (must be unique)
            email: Google account email
            client_id: OAuth client ID
            client_secret: OAuth client secret
            refresh_token: OAuth refresh token
            daily_quota: Daily API quota limit (default 10000)

        Returns:
            Created Account instance

        Raises:
            AccountManagerError: If validation fails or name exists
        """
        # Check name uniqueness
        existing = self.session.query(Account).filter_by(name=name).first()
        if existing:
            raise AccountManagerError(f"Account with name {name!r} already exists")

        # Validate credentials
        try:
            # Set env vars temporarily for verification
            import os
            old_client_id = os.environ.get("YOUTUBE_CLIENT_ID")
            old_client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")
            old_refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN")

            os.environ["YOUTUBE_CLIENT_ID"] = client_id
            os.environ["YOUTUBE_CLIENT_SECRET"] = client_secret
            os.environ["YOUTUBE_REFRESH_TOKEN"] = refresh_token

            try:
                youtube_service.verify_credentials()
            finally:
                # Restore old values
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

        except Exception as exc:
            raise AccountManagerError(f"Credential validation failed: {exc}") from exc

        # Calculate next quota reset (24h from now, aligned to 00:00 PT would be better but this is simpler for MVP)
        quota_reset_at = datetime.now(timezone.utc) + timedelta(hours=24)

        # Create account
        account = Account(
            name=name,
            email=email,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            daily_quota=daily_quota,
            quota_used=0,
            quota_reset_at=quota_reset_at,
            is_active=True
        )

        self.session.add(account)
        self.session.commit()
        self.session.refresh(account)

        return account

    def get_available_account(self) -> Account | None:
        """Get account with most available quota.

        Returns:
            Account with highest quota remaining, or None if all exhausted
        """
        # Filter active accounts with enough quota for one upload (1600 units)
        accounts = (
            self.session.query(Account)
            .filter(Account.is_active == True)
            .filter(Account.quota_used + 1600 <= Account.daily_quota)
            .order_by((Account.daily_quota - Account.quota_used).desc())
            .all()
        )

        return accounts[0] if accounts else None

    def consume_quota(self, account_id: int, amount: int = 1600) -> None:
        """Increment quota usage after successful upload.

        Args:
            account_id: Account ID
            amount: Quota units consumed (default 1600 for videos.insert)

        Raises:
            AccountManagerError: If account not found
        """
        account = self.session.query(Account).filter_by(id=account_id).first()
        if not account:
            raise AccountManagerError(f"Account {account_id} not found")

        account.quota_used += amount
        account.updated_at = datetime.now(timezone.utc)
        self.session.commit()

    def reset_daily_quotas(self) -> int:
        """Reset quota_used=0 for accounts past their reset time.

        Returns:
            Number of accounts reset
        """
        now = datetime.now(timezone.utc)
        accounts = self.session.query(Account).filter(Account.quota_reset_at <= now).all()

        for account in accounts:
            account.quota_used = 0
            account.quota_reset_at = now + timedelta(hours=24)
            account.updated_at = now

        self.session.commit()
        return len(accounts)

    def list_accounts(self, active_only: bool = False) -> list[Account]:
        """List all accounts, optionally filtered by active status.

        Args:
            active_only: If True, only return is_active=True accounts

        Returns:
            List of Account instances
        """
        query = self.session.query(Account)
        if active_only:
            query = query.filter(Account.is_active == True)
        return query.order_by(Account.created_at).all()

    def toggle_active(self, account_id: int) -> Account:
        """Toggle is_active flag for an account.

        Args:
            account_id: Account ID

        Returns:
            Updated Account instance

        Raises:
            AccountManagerError: If account not found
        """
        account = self.session.query(Account).filter_by(id=account_id).first()
        if not account:
            raise AccountManagerError(f"Account {account_id} not found")

        account.is_active = not account.is_active
        account.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        self.session.refresh(account)

        return account
