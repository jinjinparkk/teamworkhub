"""FastAPI application — single HTTP entrypoint for Cloud Scheduler.

All endpoint logic lives in src/routes/*.py; this file only wires routers.
The MCP vault server is mounted at /mcp for Streamable HTTP access.
"""
import os

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.logging_cfg import configure_logging
from src.mcp_vault_drive import mcp as mcp_server
from src.routes import health, sync, daily, weekly, monthly, dashboard, archive, backup

# Configure logging once at import time so the first uvicorn log is formatted.
configure_logging()


# ---------------------------------------------------------------------------
# MCP ASGI sub-application
# ---------------------------------------------------------------------------

mcp_app = mcp_server.http_app(path="/")


# ---------------------------------------------------------------------------
# Bearer-token auth middleware — protects /mcp/* only
# ---------------------------------------------------------------------------

class MCPAuthMiddleware(BaseHTTPMiddleware):
    """Reject unauthenticated requests to /mcp/* paths."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            api_key = os.environ.get("MCP_API_KEY", "")
            if api_key:
                auth = request.headers.get("authorization", "")
                if auth != f"Bearer {api_key}":
                    return JSONResponse(
                        {"detail": "Invalid or missing MCP API key"},
                        status_code=401,
                    )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Main FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="TeamWorkHub",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(MCPAuthMiddleware)

app.include_router(health.router)
app.include_router(sync.router)
app.include_router(daily.router)
app.include_router(weekly.router)
app.include_router(monthly.router)
app.include_router(dashboard.router)
app.include_router(archive.router)
app.include_router(backup.router)

app.mount("/mcp", mcp_app)
