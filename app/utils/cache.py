"""Artifact-level caching helpers.

Each expensive pipeline step writes a sibling `.hash` file containing the
sha256 of its input. On rerun, the stage compares hashes and short-circuits
if the artifact already matches — cheap, dependency-free idempotency.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def hash_text(text: str) -> str:
    """Return the full lowercase hex sha256 digest of `text` (utf-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_hash_file(path: Path) -> str | None:
    """Return the stored hash, or None if the file is absent."""
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def write_hash_file(path: Path, hash_value: str) -> None:
    """Persist `hash_value` to `path`, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(hash_value, encoding="utf-8")


def artifact_fresh(artifact_path: Path, hash_path: Path, expected_hash: str) -> bool:
    """True iff artifact + hash files exist, hash matches, and artifact is non-empty."""
    if not artifact_path.exists() or not hash_path.exists():
        return False
    try:
        if artifact_path.stat().st_size == 0:
            return False
    except OSError:
        return False
    stored = read_hash_file(hash_path)
    return stored == expected_hash
