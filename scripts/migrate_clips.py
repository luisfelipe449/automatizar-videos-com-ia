"""Upgrade an existing database for clips mode. Safe to run repeatedly.

The dashboard and the worker already run this at startup; the script is for
upgrading a database without starting either.

Usage:
    python scripts/migrate_clips.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import get_engine
from app.db.migrations import add_clips_schema


def main() -> int:
    engine = get_engine()
    changes = add_clips_schema(engine)
    if changes:
        print(f"[migrate_clips] Added: {', '.join(changes)}")
    else:
        print("[migrate_clips] Schema already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
