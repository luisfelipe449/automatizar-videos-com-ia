"""Suite-wide setup: keep every test off the real database and clips drive.

`app.db.database` reads DATABASE_URL once, at import, to build the default
engine, and `app.web.main` runs the schema upgrade on that engine when it is
imported. This conftest is loaded before any test module imports `app`, so
pointing DATABASE_URL at a throwaway file here means nothing in the suite
(module-level app, worker, default sessions) can read or write data/app.db.
`load_dotenv` does not override a variable that is already set, so an empty
CLIPS_DIR here also keeps a developer's `.env` from sending clips-mode
defaults to another drive; tests that write files pass their own tmp dirs.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_DB_DIR = Path(tempfile.mkdtemp(prefix="pytest-appdb-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_DB_DIR / 'app.db').as_posix()}"
os.environ["CLIPS_DIR"] = ""


def pytest_unconfigure(config) -> None:
    from app.db import database

    database._engine.dispose()
    shutil.rmtree(_DB_DIR, ignore_errors=True)
