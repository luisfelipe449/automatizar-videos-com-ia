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
