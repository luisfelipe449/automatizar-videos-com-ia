"""FastAPI application factory for video automation dashboard."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.web.routes import dashboard, accounts, jobs, api


def create_app() -> FastAPI:
    """Create and configure FastAPI application.

    Returns:
        Configured FastAPI application instance
    """
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

    return app


# Create application instance
app = create_app()
