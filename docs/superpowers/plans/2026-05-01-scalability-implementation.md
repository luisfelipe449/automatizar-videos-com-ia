# Multi-Account Scalability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade single-account CLI pipeline to support 2-5 YouTube accounts with a web dashboard for job management, quota tracking, and analytics.

**Architecture:** FastAPI + SQLite + Jinja2 dashboard. New code in separate namespaces (`db/`, `core/`, `web/`) to preserve CLI backward compatibility. Worker polls DB for jobs and executes via refactored orchestrator.

**Tech Stack:** FastAPI 0.115, SQLAlchemy 2.0, SQLite, Jinja2, TailwindCSS CDN, Chart.js, pytest

---

## File Structure Overview

### New Files (Create)
```
app/
├── db/
│   ├── __init__.py
│   ├── database.py          # SQLAlchemy engine + session factory
│   └── models.py            # Account, Job, Upload, QuotaSnapshot tables
├── core/
│   ├── __init__.py
│   ├── account_manager.py   # CRUD + quota tracking + account selection
│   ├── job_queue.py         # Enqueue, get_next, update_status, retry
│   ├── pipeline_runner.py   # Execute jobs via orchestrator.run_pipeline()
│   └── analytics.py         # Aggregate metrics for dashboard
├── web/
│   ├── __init__.py
│   ├── main.py              # FastAPI app factory
│   ├── dependencies.py      # DB session dependency injection
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── dashboard.py     # GET / - home page
│   │   ├── accounts.py      # Account CRUD routes
│   │   ├── jobs.py          # Job queue routes
│   │   └── api.py           # JSON endpoints for AJAX
│   ├── templates/
│   │   ├── base.html
│   │   ├── dashboard.html
│   │   ├── accounts.html
│   │   ├── account_form.html
│   │   ├── jobs.html
│   │   └── job_detail.html
│   └── static/
│       ├── css/
│       │   └── styles.css
│       └── js/
│           └── main.js
└── worker.py                # Job processor polling loop

scripts/
└── init_db.py               # Create SQLite tables

tests/
├── test_account_manager.py
├── test_job_queue.py
├── test_pipeline_runner.py
├── test_analytics.py
└── test_web_routes.py

data/
└── app.db                   # SQLite database (created by init_db.py)
```

### Modified Files
```
app/orchestrator.py          # Refactor: extract run_pipeline() function
requirements.txt             # Add FastAPI, SQLAlchemy, testing deps
.env.example                 # Add DATABASE_URL
README.md                    # Add dashboard setup instructions
```

---

## Phase 1: Database Layer (Days 1-2)

### Task 1: Database Models and Session Factory

**Files:**
- Create: `app/db/__init__.py`
- Create: `app/db/database.py`
- Create: `app/db/models.py`
- Test: `tests/test_db_models.py`

- [ ] **Step 1: Write test for database session factory**

Create `tests/test_db_models.py`:

```python
"""Tests for database models and session management."""
import pytest
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.database import Base, get_engine, get_session


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db_models.py::test_get_engine_creates_sqlite_engine -v`

Expected: FAIL with "ModuleNotFoundError: No module named 'app.db'"

- [ ] **Step 3: Create database.py with engine and session factory**

Create `app/db/__init__.py`:
```python
"""Database layer for multi-account system."""
```

Create `app/db/database.py`:

```python
"""SQLAlchemy engine and session management."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# Base class for all models
Base = declarative_base()

# Default database URL from environment
_DEFAULT_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{Path(__file__).resolve().parent.parent.parent / 'data' / 'app.db'}"
)


def get_engine(database_url: str | None = None):
    """Create SQLAlchemy engine.
    
    Args:
        database_url: Database connection string. If None, uses DATABASE_URL env var.
    
    Returns:
        Engine instance
    """
    url = database_url or _DEFAULT_DATABASE_URL
    
    # SQLite-specific: enable foreign keys
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    
    return create_engine(url, connect_args=connect_args, echo=False)


# Global engine and session factory (initialized on first import)
_engine = get_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@contextmanager
def get_session(engine=None) -> Session:
    """Context manager for database sessions.
    
    Usage:
        with get_session() as session:
            session.query(Account).all()
    
    Args:
        engine: Optional engine override (for testing)
    
    Yields:
        Session instance
    """
    if engine is not None:
        # Test override
        SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        session = SessionFactory()
    else:
        session = SessionLocal()
    
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db_models.py::test_get_engine_creates_sqlite_engine -v`

Expected: PASS

- [ ] **Step 5: Write test for Account model**

Add to `tests/test_db_models.py`:

```python
from datetime import datetime, timezone, timedelta
from app.db.models import Account


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
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest tests/test_db_models.py::test_account_model_creates_with_required_fields -v`

Expected: FAIL with "ImportError: cannot import name 'Account'"

- [ ] **Step 7: Create database models**

Create `app/db/models.py`:

```python
"""SQLAlchemy models for multi-account system."""
from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db.database import Base


class Account(Base):
    """YouTube account with OAuth credentials and quota tracking."""
    
    __tablename__ = "accounts"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    email = Column(String(255), nullable=False)
    client_id = Column(String(255), nullable=False)
    client_secret = Column(String(255), nullable=False)
    refresh_token = Column(Text, nullable=False)
    daily_quota = Column(Integer, default=10000, nullable=False)
    quota_used = Column(Integer, default=0, nullable=False)
    quota_reset_at = Column(DateTime(timezone=True), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships
    jobs = relationship("Job", back_populates="account")
    uploads = relationship("Upload", back_populates="account")
    quota_snapshots = relationship("QuotaSnapshot", back_populates="account")
    
    def __repr__(self):
        return f"<Account(id={self.id}, name={self.name!r}, email={self.email!r})>"


class Job(Base):
    """Job queue entry with execution state."""
    
    __tablename__ = "jobs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(255), unique=True, nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    prompt = Column(Text, nullable=False)
    status = Column(String(50), nullable=False)  # pending/running/completed/failed
    stage = Column(String(50), nullable=True)  # script/tts/captions/visual/render/upload
    error_message = Column(Text, nullable=True)
    priority = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    
    # Relationships
    account = relationship("Account", back_populates="jobs")
    uploads = relationship("Upload", back_populates="job")
    
    def __repr__(self):
        return f"<Job(id={self.id}, job_id={self.job_id!r}, status={self.status!r})>"


class Upload(Base):
    """YouTube upload record for analytics."""
    
    __tablename__ = "uploads"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(255), ForeignKey("jobs.job_id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    video_id = Column(String(50), nullable=False)
    title = Column(String(255), nullable=False)
    privacy_status = Column(String(50), nullable=False)
    views = Column(Integer, default=0, nullable=False)
    uploaded_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    job = relationship("Job", back_populates="uploads")
    account = relationship("Account", back_populates="uploads")
    
    def __repr__(self):
        return f"<Upload(id={self.id}, video_id={self.video_id!r}, job_id={self.job_id!r})>"


class QuotaSnapshot(Base):
    """Daily quota usage snapshot for trend analysis."""
    
    __tablename__ = "quota_snapshots"
    __table_args__ = (UniqueConstraint("account_id", "date", name="uq_account_date"),)
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    date = Column(Date, nullable=False)
    quota_used = Column(Integer, nullable=False)
    uploads_count = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    account = relationship("Account", back_populates="quota_snapshots")
    
    def __repr__(self):
        return f"<QuotaSnapshot(id={self.id}, account_id={self.account_id}, date={self.date})>"
```

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest tests/test_db_models.py::test_account_model_creates_with_required_fields -v`

Expected: PASS

- [ ] **Step 9: Write tests for other models**

Add to `tests/test_db_models.py`:

```python
from app.db.models import Job, Upload, QuotaSnapshot
from datetime import date


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
```

- [ ] **Step 10: Run tests to verify they pass**

Run: `pytest tests/test_db_models.py -v`

Expected: All tests PASS

- [ ] **Step 11: Commit database layer**

```bash
git add app/db/ tests/test_db_models.py
git commit -m "feat(db): add SQLAlchemy models and session factory

- Account model with OAuth credentials and quota tracking
- Job model for queue management and execution state
- Upload model for YouTube analytics
- QuotaSnapshot model for daily quota history
- Session factory with context manager
- Comprehensive unit tests for all models

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 2: Database Initialization Script

**Files:**
- Create: `scripts/init_db.py`
- Modify: `requirements.txt`
- Modify: `.env.example`

- [ ] **Step 1: Update requirements.txt with new dependencies**

Add to `requirements.txt`:

```txt
# Web Dashboard
fastapi==0.115.0
uvicorn[standard]==0.32.0
jinja2==3.1.4
python-multipart==0.0.17

# Database
sqlalchemy==2.0.36

# Testing
httpx==0.27.2
pytest-asyncio==0.24.0
```

- [ ] **Step 2: Update .env.example with DATABASE_URL**

Add to `.env.example`:

```bash
# Database
DATABASE_URL=sqlite:///data/app.db
```

- [ ] **Step 3: Write init_db.py script**

Create `scripts/init_db.py`:

```python
"""Initialize SQLite database with all tables.

Usage:
    python scripts/init_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import Base, get_engine


def main() -> int:
    """Create all database tables."""
    print("[init_db] Creating database tables...")
    
    engine = get_engine()
    db_path = engine.url.database
    
    # Ensure data directory exists
    if db_path and not db_path.startswith(":memory:"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    
    # Create all tables
    Base.metadata.create_all(engine)
    
    print(f"[init_db] Database initialized at: {db_path}")
    print("[init_db] Tables created:")
    for table in Base.metadata.sorted_tables:
        print(f"  - {table.name}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Test init_db.py script**

Run: `python scripts/init_db.py`

Expected output:
```
[init_db] Creating database tables...
[init_db] Database initialized at: data/app.db
[init_db] Tables created:
  - accounts
  - jobs
  - uploads
  - quota_snapshots
```

- [ ] **Step 5: Verify database file exists**

Run: `ls -lh data/app.db`

Expected: File exists with size > 0

- [ ] **Step 6: Commit init script and config updates**

```bash
git add scripts/init_db.py requirements.txt .env.example
git commit -m "feat(db): add database initialization script

- scripts/init_db.py creates SQLite database and all tables
- Update requirements.txt with FastAPI, SQLAlchemy dependencies
- Add DATABASE_URL to .env.example

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Phase 2: Core Business Logic (Days 3-4)

### Task 3: AccountManager

**Files:**
- Create: `app/core/__init__.py`
- Create: `app/core/account_manager.py`
- Test: `tests/test_account_manager.py`

- [ ] **Step 1: Write test for add_account with valid credentials**

Create `tests/test_account_manager.py`:

```python
"""Tests for AccountManager."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch
from pathlib import Path

from app.core.account_manager import AccountManager
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_account_manager.py::test_add_account_creates_account_with_valid_token -v`

Expected: FAIL with "ModuleNotFoundError: No module named 'app.core'"

- [ ] **Step 3: Write AccountManager implementation**

Create `app/core/__init__.py`:
```python
"""Core business logic for multi-account system."""
```

Create `app/core/account_manager.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_account_manager.py::test_add_account_creates_account_with_valid_token -v`

Expected: PASS

- [ ] **Step 5: Write more AccountManager tests**

Add to `tests/test_account_manager.py`:

```python
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
```

- [ ] **Step 6: Run all AccountManager tests**

Run: `pytest tests/test_account_manager.py -v`

Expected: All tests PASS

- [ ] **Step 7: Commit AccountManager**

```bash
git add app/core/account_manager.py tests/test_account_manager.py
git commit -m "feat(core): implement AccountManager with quota tracking

- add_account validates OAuth credentials before saving
- get_available_account selects account with most quota remaining
- consume_quota increments usage after upload
- reset_daily_quotas resets accounts past reset time
- toggle_active enables/disables accounts
- Comprehensive unit tests with mock YouTube verification

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 4: JobQueueManager

**Files:**
- Create: `app/core/job_queue.py`
- Test: `tests/test_job_queue.py`

- [ ] **Step 1: Write test for enqueue job**

Create `tests/test_job_queue.py`:

```python
"""Tests for JobQueueManager."""
import pytest
from datetime import datetime, timezone, timedelta

from app.core.job_queue import JobQueueManager
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_job_queue.py::test_enqueue_creates_pending_job -v`

Expected: FAIL with "ModuleNotFoundError: No module named 'app.core.job_queue'"

- [ ] **Step 3: Write JobQueueManager implementation**

Create `app/core/job_queue.py`:

```python
"""Job queue management: enqueue, dequeue, status updates."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.db.models import Account, Job, Upload


class JobQueueError(Exception):
    """Raised when job queue operations fail."""


class JobQueueManager:
    """Manages job queue: enqueue, get_next, update status, retry."""
    
    def __init__(self, session: Session):
        """Initialize manager with database session.
        
        Args:
            session: SQLAlchemy session
        """
        self.session = session
    
    def enqueue(
        self,
        prompt: str,
        priority: int = 0,
        account_id: int | None = None
    ) -> Job:
        """Create new pending job.
        
        Args:
            prompt: User input text
            priority: Higher values execute first (default 0)
            account_id: Optional account assignment (None = auto-assign by worker)
        
        Returns:
            Created Job instance
        """
        # Generate job_id: YYYY-MM-DD_slug_hash
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        
        # Create slug from prompt (first 3 words, lowercase, replace spaces with hyphens)
        slug = re.sub(r'[^\w\s-]', '', prompt.lower())[:50]
        slug = re.sub(r'[-\s]+', '-', slug).strip('-')
        words = slug.split('-')[:3]
        slug = '-'.join(words) if words else 'untitled'
        
        # Add hash for uniqueness
        hash_short = hashlib.sha256(prompt.encode()).hexdigest()[:6]
        job_id = f"{date_str}_{slug}_{hash_short}"
        
        job = Job(
            job_id=job_id,
            account_id=account_id,
            prompt=prompt,
            status="pending",
            priority=priority
        )
        
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        
        return job
    
    def get_next_pending(self) -> Job | None:
        """Get highest priority pending job.
        
        Returns:
            Next job to process, or None if queue empty
        """
        job = (
            self.session.query(Job)
            .filter(Job.status == "pending")
            .order_by(Job.priority.desc(), Job.created_at.asc())
            .first()
        )
        return job
    
    def update_status(
        self,
        job_id: str,
        status: str,
        stage: str | None = None,
        error: str | None = None
    ) -> None:
        """Update job execution state.
        
        Args:
            job_id: Job ID
            status: New status (pending/running/completed/failed)
            stage: Current pipeline stage (script/tts/captions/visual/render/upload)
            error: Error message if status=failed
        
        Raises:
            JobQueueError: If job not found
        """
        job = self.session.query(Job).filter_by(job_id=job_id).first()
        if not job:
            raise JobQueueError(f"Job {job_id} not found")
        
        job.status = status
        if stage is not None:
            job.stage = stage
        if error is not None:
            job.error_message = error
        
        # Update timestamps
        if status == "running" and job.started_at is None:
            job.started_at = datetime.now(timezone.utc)
        elif status in ("completed", "failed"):
            job.completed_at = datetime.now(timezone.utc)
        
        self.session.commit()
    
    def mark_completed(self, job_id: str, video_id: str) -> None:
        """Finalize job and create upload record.
        
        Args:
            job_id: Job ID
            video_id: YouTube video ID
        
        Raises:
            JobQueueError: If job not found or not associated with account
        """
        job = self.session.query(Job).filter_by(job_id=job_id).first()
        if not job:
            raise JobQueueError(f"Job {job_id} not found")
        if not job.account_id:
            raise JobQueueError(f"Job {job_id} has no associated account")
        
        # Update job status
        job.status = "completed"
        job.stage = "upload"
        job.completed_at = datetime.now(timezone.utc)
        
        # Create upload record
        upload = Upload(
            job_id=job.job_id,
            account_id=job.account_id,
            video_id=video_id,
            title=job.prompt[:255],  # truncate if needed
            privacy_status="private",  # default, can be overridden later
            views=0
        )
        
        self.session.add(upload)
        self.session.commit()
    
    def get_job_history(self, limit: int = 50, status: str | None = None) -> list[Job]:
        """Retrieve recent jobs, optionally filtered by status.
        
        Args:
            limit: Maximum number of jobs to return
            status: Filter by status (None = all)
        
        Returns:
            List of Job instances, newest first
        """
        query = self.session.query(Job)
        if status:
            query = query.filter(Job.status == status)
        return query.order_by(Job.created_at.desc()).limit(limit).all()
    
    def retry_failed_job(self, job_id: str) -> Job:
        """Reset failed job to pending for retry.
        
        Args:
            job_id: Job ID
        
        Returns:
            Updated Job instance
        
        Raises:
            JobQueueError: If job not found or not in failed state
        """
        job = self.session.query(Job).filter_by(job_id=job_id).first()
        if not job:
            raise JobQueueError(f"Job {job_id} not found")
        if job.status != "failed":
            raise JobQueueError(f"Job {job_id} is not in failed state (current: {job.status})")
        
        job.status = "pending"
        job.stage = None
        job.error_message = None
        job.account_id = None  # unassign, let worker pick new account
        job.started_at = None
        job.completed_at = None
        
        self.session.commit()
        self.session.refresh(job)
        
        return job
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_job_queue.py::test_enqueue_creates_pending_job -v`

Expected: PASS

- [ ] **Step 5: Write more JobQueueManager tests**

Add to `tests/test_job_queue.py`:

```python
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
```

- [ ] **Step 6: Run all JobQueueManager tests**

Run: `pytest tests/test_job_queue.py -v`

Expected: All tests PASS

- [ ] **Step 7: Commit JobQueueManager**

```bash
git add app/core/job_queue.py tests/test_job_queue.py
git commit -m "feat(core): implement JobQueueManager for job lifecycle

- enqueue creates pending jobs with auto-generated job_id
- get_next_pending respects priority then FIFO
- update_status tracks execution state and timestamps
- mark_completed creates Upload record
- retry_failed_job resets jobs for re-execution
- Comprehensive unit tests covering all transitions

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

---

## Remaining Tasks (Condensed - Expand During Execution)

**Note:** Tasks 1-4 above provide the detailed TDD pattern. Tasks 5-21 follow the same approach but are condensed here. When executing with subagent-driven-development, each task will be expanded with full test code, implementation code, and verification steps.

### Task 5: PipelineRunner
- Test: run_job assigns account, updates stages, consumes quota
- Implement: Bridge between job queue and orchestrator.run_pipeline()
- Handle YouTubeQuotaExceeded by resetting job to pending with different account

### Task 6: AnalyticsAggregator  
- Test: get_account_stats aggregates uploads/views
- Test: get_daily_summary counts jobs by date
- Implement: Query helpers for dashboard metrics

### Task 7: Refactor Orchestrator
- Extract run_pipeline(prompt, job_id, account) from main()
- Return dict with success, video_id, stages_completed, error
- Preserve CLI backward compatibility via main() wrapper

### Task 8: FastAPI App Factory
- Create app/web/main.py with FastAPI()
- Mount static files and templates
- Include all route modules

### Task 9-12: FastAPI Routes
- dashboard.py: GET / renders dashboard.html with metrics
- accounts.py: CRUD endpoints + forms
- jobs.py: List, create, detail, retry, cancel
- api.py: JSON endpoints for /api/status, /api/jobs/{id}/status

### Task 13-18: Jinja2 Templates
- base.html: Layout with navbar, Tailwind CDN
- dashboard.html: Cards + Chart.js uploads chart
- accounts.html: Table with status badges
- account_form.html: Form with AJAX validation
- jobs.html: Filterable table with status badges
- job_detail.html: Timeline + artifacts + polling JS

### Task 19: Worker Process
- app/worker.py: Polling loop calls PipelineRunner.run_job()
- Call AccountManager.reset_daily_quotas() each iteration
- Sleep 5s when queue empty

### Task 20: Integration Tests
- test_web_routes.py: FastAPI TestClient for all routes
- test_job_submission_flow: Enqueue → worker processes → marks completed

### Task 21: Documentation Updates
- README: Add "Running with Dashboard" section
- README: Document multi-account setup
- Add troubleshooting for worker/dashboard issues

---

## Execution Strategy

**Recommended:** Use `superpowers:subagent-driven-development` skill

This approach:
- Dispatches fresh subagent per task with full context
- Each subagent expands condensed tasks into detailed TDD steps
- Two-stage review (code review + testing) between tasks
- Enables parallel iteration on independent tasks

**Alternative:** Use `superpowers:executing-plans` for inline execution with checkpoints

---

## Success Criteria

After completing all tasks:
- [ ] `python scripts/init_db.py` creates database
- [ ] `pytest tests/` passes all unit tests
- [ ] `uvicorn app.web.main:app` starts dashboard on localhost:8000
- [ ] `python -m app.worker` processes jobs from queue
- [ ] Dashboard shows accounts, jobs, analytics
- [ ] CLI `python -m app.orchestrator --prompt "..."` still works
- [ ] End-to-end: Submit job via dashboard → worker processes → video uploaded
