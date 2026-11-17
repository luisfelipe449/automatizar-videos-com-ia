"""FastAPI application factory for video automation dashboard."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Engine

from app.db.database import get_session
from app.db.migrations import upgrade_schema
from app.web.dependencies import get_db
from app.web.routes import dashboard, accounts, jobs, api, editions


def create_app(engine: Engine | None = None) -> FastAPI:
    """Create and configure FastAPI application.

    `engine` (default: the app database) is the database the schema upgrade
    runs against at startup and every request's session is bound to.

    Returns:
        Configured FastAPI application instance
    """
    upgrade_schema(engine)

    app = FastAPI(
        title="Video Automation Dashboard",
        description="Multi-account YouTube video automation system",
        version="1.0.0",
    )

    # Mount static files
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Include routers
    app.include_router(dashboard.router, tags=["dashboard"])
    app.include_router(accounts.router, prefix="/accounts", tags=["accounts"])
    app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
    app.include_router(api.router, prefix="/api", tags=["api"])
    app.include_router(editions.router, prefix="/editions", tags=["editions"])

    if engine is not None:
        def get_db_on_engine():
            with get_session(engine) as session:
                yield session

        app.dependency_overrides[get_db] = get_db_on_engine

    return app


# Create application instance
app = create_app()
