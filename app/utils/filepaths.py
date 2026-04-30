"""Deterministic filesystem paths per job_id.

Pure functions — no side effects beyond `ensure_job_dirs`, which only creates
directories. Every other helper merely constructs a `Path`.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

from app.config import GENERATED_DIR, RENDERED_DIR

_SLUG_MAX_LEN = 40
_HASH_LEN = 6


def job_dir(job_id: str) -> Path:
    return GENERATED_DIR / job_id


def rendered_dir(job_id: str) -> Path:
    return RENDERED_DIR / job_id


def script_path(job_id: str) -> Path:
    return job_dir(job_id) / "script.json"


def narration_path(job_id: str) -> Path:
    return job_dir(job_id) / "narration.wav"


def narration_hash_path(job_id: str) -> Path:
    return job_dir(job_id) / "narration.hash"


def captions_path(job_id: str) -> Path:
    return job_dir(job_id) / "captions.srt"


def scene_image_path(job_id: str, scene_id: int) -> Path:
    return job_dir(job_id) / f"scene_{scene_id:02d}.png"


def scene_image_hash_path(job_id: str, scene_id: int) -> Path:
    return job_dir(job_id) / f"scene_{scene_id:02d}.hash"


def final_video_path(job_id: str) -> Path:
    return rendered_dir(job_id) / "final.mp4"


def status_path(job_id: str) -> Path:
    return job_dir(job_id) / "status.json"


def upload_record_path(job_id: str) -> Path:
    return job_dir(job_id) / "upload.json"


def ensure_job_dirs(job_id: str) -> None:
    """Create both the generated and rendered directories for `job_id`."""
    job_dir(job_id).mkdir(parents=True, exist_ok=True)
    rendered_dir(job_id).mkdir(parents=True, exist_ok=True)


def _slugify(text: str) -> str:
    """Lowercase, non-alnum → '-', collapse repeats, strip edges, max 40 chars."""
    lowered = text.lower()
    replaced = re.sub(r"[^a-z0-9]+", "-", lowered)
    collapsed = re.sub(r"-+", "-", replaced).strip("-")
    return collapsed[:_SLUG_MAX_LEN].strip("-")


def make_job_id(prompt: str, now: datetime | None = None) -> str:
    """Build a deterministic, filesystem-safe job_id.

    Format: `<YYYY-MM-DD>_<slug>_<hash6>` — the hash guarantees uniqueness even
    when two prompts slugify to the same token on the same day.
    """
    stamp = (now or datetime.now()).strftime("%Y-%m-%d")
    slug = _slugify(prompt) or "job"
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:_HASH_LEN]
    return f"{stamp}_{slug}_{digest}"
