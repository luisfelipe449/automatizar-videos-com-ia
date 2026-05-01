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
from app.db import models  # Import models to register them with Base


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
