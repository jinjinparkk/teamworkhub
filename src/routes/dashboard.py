"""POST /dashboard — generate Dataview-powered Dashboard.md."""
from __future__ import annotations

import ast
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src import config as cfg_module
from src.config import TEAM_MEMBERS
from src.dashboard_writer import (
    compose_dashboard,
    compose_assignee_page,
    filename_for_dashboard,
    filename_for_assignee,
)

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/dashboard", summary="Generate Dataview-powered Dashboard.md")
def dashboard() -> JSONResponse:
    """Writes Dashboard.md to the local dashboard folder.

    The dashboard uses Obsidian Dataview plugin queries to show live stats
    from all TeamWorkHub daily/weekly notes.

    Response shape (always HTTP 200):
    {
      "status":  "ok" | "skipped" | "error",
      "run_id":  "<8-char id>",
      "note":    "..."   // only when status != "ok"
    }
    """
    run_id = uuid.uuid4().hex[:8]
    c = cfg_module.load()

    log.info("dashboard started", extra={"run_id": run_id})

    if not c.local_dashboard_dir:
        return JSONResponse(status_code=200, content={
            "status": "skipped",
            "run_id": run_id,
            "note": "set LOCAL_DASHBOARD_DIR to enable dashboard generation",
        })

    try:
        tz = ZoneInfo(c.timezone)
    except Exception:
        tz = ZoneInfo("Asia/Seoul")

    today_str = datetime.now(tz).strftime("%Y-%m-%d")

    # Derive folder names from configured local dirs for Dataview queries.
    daily_folder = (
        Path(c.local_daily_output_dir).name
        if c.local_daily_output_dir
        else "TeamWorkHub_Daily"
    )
    weekly_folder = (
        Path(c.local_weekly_output_dir).name
        if c.local_weekly_output_dir
        else "TeamWorkHub_Weekly"
    )

    try:
        dash_dir = Path(c.local_dashboard_dir)
        dash_dir.mkdir(parents=True, exist_ok=True)
        dash_content = compose_dashboard(today_str, daily_folder, weekly_folder)
        (dash_dir / filename_for_dashboard()).write_text(dash_content, encoding="utf-8")
        log.info("dashboard written",
                 extra={"run_id": run_id, "path": str(dash_dir / filename_for_dashboard())})
    except Exception as exc:
        log.error("dashboard -- write failed", extra={"run_id": run_id, "error": str(exc)})
        return JSONResponse(status_code=200, content={
            "status": "error", "run_id": run_id, "note": str(exc),
        })

    # Scan all past Daily Notes to collect every historical assignee,
    # then create/refresh their pages in the dashboard folder.
    all_assignees: set[str] = set()
    if c.local_daily_output_dir:
        _assignee_re = re.compile(r"^assignees:\s*(\[.+\])", re.MULTILINE)
        for md_file in Path(c.local_daily_output_dir).glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                m = _assignee_re.search(content)
                if m:
                    names = ast.literal_eval(m.group(1))
                    all_assignees.update(n for n in names if isinstance(n, str) and n.strip())
            except Exception:
                pass  # skip unreadable / malformed files silently

    all_assignees = {n for n in all_assignees if n in TEAM_MEMBERS}

    if all_assignees:
        try:
            for assignee in sorted(all_assignees):
                page_content = compose_assignee_page(assignee, daily_folder)
                page_name = filename_for_assignee(assignee)
                (dash_dir / page_name).write_text(page_content, encoding="utf-8")
                log.info("assignee page refreshed",
                         extra={"run_id": run_id, "assignee": assignee})
        except Exception as exc:
            log.warning("dashboard -- assignee page write failed",
                        extra={"run_id": run_id, "error": str(exc)})

    assignee_count = len(all_assignees)
    log.info("dashboard complete",
             extra={"run_id": run_id, "assignee_pages": assignee_count})
    return JSONResponse(status_code=200, content={
        "status": "ok", "run_id": run_id, "assignee_pages": assignee_count,
    })
