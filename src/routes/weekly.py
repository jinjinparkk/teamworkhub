"""POST /weekly — generate weekly email digest report."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src import config as cfg_module
from src.auth import build_credentials, build_drive_service
from src.drive_client import upsert_markdown
from src.archive_scanner import collect_archive_for_daily
from src.weekly_writer import compose_weekly, filename_for_week
from src.dependencies import _collect_messages

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/weekly", summary="Generate weekly email digest report")
def weekly() -> JSONResponse:
    """Collects emails from Monday 00:00 to Friday 23:59 (configured timezone)
    and writes a single Weekly Report: YYYY-WNN.md.

    Intended to be triggered by Cloud Scheduler every Monday at 09:00.

    Response shape (always HTTP 200):
    {
      "status":      "ok" | "skipped" | "error",
      "run_id":      "<8-char id>",
      "week":        "2026-W14",
      "email_count": <int>,
      "note":        "..."   // only when status != "ok"
    }
    """
    run_id = uuid.uuid4().hex[:8]
    c = cfg_module.load()

    log.info("weekly started", extra={"run_id": run_id})

    missing = cfg_module.validate_for_sync(c)
    if missing:
        return JSONResponse(status_code=200, content={
            "status": "skipped", "run_id": run_id, "week": "",
            "email_count": 0, "note": f"set these env vars to enable weekly: {missing}",
        })

    try:
        tz = ZoneInfo(c.timezone)
    except Exception:
        tz = ZoneInfo("Asia/Seoul")

    now = datetime.now(tz)
    # Runs on Monday → report covers *previous* week (Mon~Fri).
    # If today is Monday, go back 7 days to get last Monday.
    # Otherwise, find the most recent Monday and go back 7 days.
    days_since_monday = now.weekday()  # 0=Mon, 6=Sun
    this_monday = now - timedelta(days=days_since_monday)
    last_monday = this_monday - timedelta(days=7)
    week_start = last_monday.replace(hour=0, minute=0, second=0, microsecond=0)
    # week_end = Saturday 00:00:00 (exclusive boundary for Gmail 'before:' query)
    # This ensures all Friday messages are included.
    week_end = week_start + timedelta(days=5)
    # Use ISO 8601 week format (%G = ISO year, %V = ISO week 01-53)
    # Avoids year-boundary mismatch that %Y + %W can produce in late December.
    week_str = week_start.strftime("%G-W%V")
    date_from = week_start.strftime("%Y-%m-%d (월)")
    date_to = (week_start + timedelta(days=4)).strftime("%Y-%m-%d (금)")
    gmail_q = f"after:{int(week_start.timestamp())} before:{int(week_end.timestamp())}"

    try:
        creds = build_credentials(c)
        drive_svc = build_drive_service(creds)
    except Exception as exc:
        log.error("weekly aborted -- Drive auth failed", extra={"run_id": run_id, "error": str(exc)})
        return JSONResponse(status_code=200, content={
            "status": "error", "run_id": run_id, "week": week_str,
            "email_count": 0, "note": "OAuth credential refresh failed",
        })

    if c.drive_email_archive_folder_id:
        # Archive mode: scan Drive folders matching the week range.
        date_range_start = week_start.strftime("%Y-%m-%d")
        date_range_end = (week_start + timedelta(days=4)).strftime("%Y-%m-%d")
        log.info("weekly -- using Drive archive",
                 extra={"run_id": run_id, "date_range": f"{date_range_start}~{date_range_end}"})
        messages_with_analysis = collect_archive_for_daily(
            drive_svc, c.drive_email_archive_folder_id,
            date_range_start, date_range_end,
            c.anthropic_api_key, c.local_output_dir, run_id,
            drive_output_folder_id=c.drive_output_folder_id,
        )
    else:
        messages_with_analysis = _collect_messages(c, creds, gmail_q, run_id, "weekly")

    email_count = len(messages_with_analysis)
    log.info("weekly -- collection complete", extra={"run_id": run_id, "email_count": email_count})

    md_name = filename_for_week(week_str)
    md_content = compose_weekly(messages_with_analysis, week_str, date_from, date_to, c.timezone)

    weekly_folder_id = c.weekly_output_folder_id or c.daily_output_folder_id or c.drive_output_folder_id
    try:
        upsert_markdown(drive_svc, weekly_folder_id, md_name, md_content)
        log.info("weekly report upserted to Drive", extra={"run_id": run_id, "md_name": md_name})
    except Exception as exc:
        log.error("weekly -- upsert failed", extra={"run_id": run_id, "error": str(exc)})
        return JSONResponse(status_code=200, content={
            "status": "error", "run_id": run_id, "week": week_str,
            "email_count": email_count, "note": "Drive upsert failed",
        })

    local_weekly_dir = c.local_weekly_output_dir or c.local_daily_output_dir or c.local_output_dir
    if local_weekly_dir:
        try:
            out_dir = Path(local_weekly_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / md_name).write_text(md_content, encoding="utf-8")
            log.info("weekly report written locally", extra={"run_id": run_id, "md_name": md_name})
        except Exception as exc:
            log.warning("weekly -- local write failed", extra={"run_id": run_id, "error": str(exc)})

    log.info("weekly complete", extra={"run_id": run_id, "week": week_str, "email_count": email_count})
    return JSONResponse(status_code=200, content={
        "status": "ok", "run_id": run_id, "week": week_str, "email_count": email_count,
    })
