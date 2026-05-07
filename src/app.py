"""FastAPI application — single HTTP entrypoint for Cloud Scheduler.

All endpoint logic lives in src/routes/*.py; this file only wires routers.
"""
from fastapi import FastAPI

from src.logging_cfg import configure_logging
from src.routes import health, sync, daily, weekly, monthly, dashboard, archive, backup

# Configure logging once at import time so the first uvicorn log is formatted.
configure_logging()

app = FastAPI(
    title="TeamWorkHub",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
)

app.include_router(health.router)
app.include_router(sync.router)
app.include_router(daily.router)
app.include_router(weekly.router)
app.include_router(monthly.router)
app.include_router(dashboard.router)
app.include_router(archive.router)
app.include_router(backup.router)
