"""SQLAlchemy models for multi-account system and clips mode."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
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
    # Clips mode: name of assets/input/channels/<profile>.json (language,
    # timezone, titles, schedule). NULL for prompt-pipeline accounts, which
    # keep reading language settings from .env.
    channel_profile = Column(String(100), nullable=True)
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
    category = Column(Integer, default=24, nullable=False)  # YouTube category ID (default: Entertainment)
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


# --------------------------------------------------------------------------- #
# Clips mode
# --------------------------------------------------------------------------- #


class Clip(Base):
    """A third-party clip harvested from a streaming platform.

    Classification columns stay NULL until the classifier has watched the
    clip; `classified_at IS NULL` is what marks a clip as still to classify.
    """

    __tablename__ = "clips"
    __table_args__ = (UniqueConstraint("source", "source_clip_id", name="uq_clip_source_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(20), nullable=False)  # twitch | kick | youtube
    source_clip_id = Column(String(255), nullable=False)
    url = Column(Text, nullable=False)
    broadcaster_name = Column(String(255), nullable=False)
    broadcaster_url = Column(Text, nullable=True)
    title = Column(Text, nullable=False, default="")
    language = Column(String(20), nullable=True)
    view_count = Column(Integer, nullable=False, default=0)
    duration_s = Column(Float, nullable=False)
    created_at_source = Column(DateTime(timezone=True), nullable=False)
    harvested_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    local_path = Column(Text, nullable=True)  # NULL until downloaded

    category = Column(String(20), nullable=True)  # funny | fail | play | wtf | other
    funny_score = Column(Float, nullable=True)
    hype_score = Column(Float, nullable=True)
    needs_speech = Column(Boolean, nullable=True)
    classified_by = Column(String(100), nullable=True)  # model that produced the verdict
    classified_at = Column(DateTime(timezone=True), nullable=True)

    edition_items = relationship("EditionClip", back_populates="clip")

    def __repr__(self):
        return f"<Clip(id={self.id}, source={self.source!r}, source_clip_id={self.source_clip_id!r})>"


class Edition(Base):
    """One episode of a clips format (e.g. Top Clips of the Day, 2026-11-19).

    Shared by every channel: the same selection is rendered once per
    language. `numbers` holds the per-language episode number (#N in the
    title), frozen when the edition is rendered.
    """

    __tablename__ = "editions"
    __table_args__ = (UniqueConstraint("format_key", "edition_date", name="uq_edition_format_date"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    format_key = Column(String(50), nullable=False)
    edition_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, default="planned")  # planned|rendered|approved|rejected
    numbers_json = Column(Text, nullable=False, default="{}")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    rendered_at = Column(DateTime(timezone=True), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)

    items = relationship(
        "EditionClip",
        back_populates="edition",
        order_by="EditionClip.position",
        cascade="all, delete-orphan",
    )

    @property
    def numbers(self) -> dict[str, int]:
        return json.loads(self.numbers_json or "{}")

    @numbers.setter
    def numbers(self, value: dict[str, int]) -> None:
        self.numbers_json = json.dumps(value, sort_keys=True)

    def __repr__(self):
        return f"<Edition(id={self.id}, format_key={self.format_key!r}, date={self.edition_date})>"


class EditionClip(Base):
    """A clip's slot in an edition.

    `position` is the playback order. `in_short` marks the clip reused as
    the edition's Short; `trim_start_s` drops that many seconds from the
    clip's start when it would not fit the Short's duration cap.
    """

    __tablename__ = "edition_clips"
    __table_args__ = (UniqueConstraint("edition_id", "clip_id", name="uq_edition_clip"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    edition_id = Column(Integer, ForeignKey("editions.id"), nullable=False)
    clip_id = Column(Integer, ForeignKey("clips.id"), nullable=False)
    position = Column(Integer, nullable=False)
    in_short = Column(Boolean, nullable=False, default=False)
    trim_start_s = Column(Float, nullable=True)

    edition = relationship("Edition", back_populates="items")
    clip = relationship("Clip", back_populates="edition_items")

    def __repr__(self):
        return f"<EditionClip(edition_id={self.edition_id}, clip_id={self.clip_id}, position={self.position})>"
