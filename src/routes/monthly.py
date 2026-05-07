"""POST /monthly — generate monthly email digest report."""
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
from src.monthly_writer import compose_monthly, filename_for_month
from src.dependencies import _collect_messages

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/monthly", summary="Generate monthly email digest report")
def monthly() -> JSONResponse:
    """Collects all emails from the previous calendar month (1st~last day)
    and writes a single Monthly Report: YYYY-MM.md.

    Intended to be triggered by Cloud Scheduler on the 1st of each month at 09:00.

    Response shape (always HTTP 200):
    {
      "status":      "ok" | "skipped" | "error",
      "run_id":      "<8-char id>",
      "month":       "2026-04",
      "email_count": <int>,
      "note":        "..."   // only when status != "ok"
    }
    """
    run_id = uuid.uuid4().hex[:8]
    c = cfg_module.load()

    log.info("monthly started", extra={"run_id": run_id})

    missing = cfg_module.validate_for_sync(c)
    if missing:
        return JSONResponse(status_code=200, content={
            "status": "skipped", "run_id": run_id, "month": "",
            "email_count": 0, "note": f"set these env vars to enable monthly: {missing}",
        })

    try:
        tz = ZoneInfo(c.timezone)
    except Exception:
        tz = ZoneInfo("Asia/Seoul")

    now = datetime.now(tz)
    # Report covers the *previous* calendar month.
    first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = first_of_this_month  # exclusive upper bound
    # First day of previous month
    if first_of_this_month.month == 1:
        month_start = first_of_this_month.replace(year=first_of_this_month.year - 1, month=12)
    else:
        month_start = first_of_this_month.replace(month=first_of_this_month.month - 1)

    month_str = month_start.strftime("%Y-%m")
    date_from = month_start.strftime("%Y-%m-%d")
    # Last day = day before first_of_this_month
    last_day = first_of_this_month - timedelta(days=1)
    date_to = last_day.strftime("%Y-%m-%d")

    gmail_q = f"after:{int(month_start.timestamp())} before:{int(last_month_end.timestamp())}"
    log.info("monthly -- time window",
             extra={"run_id": run_id, "month": month_str,
                    "date_from": date_from, "date_to": date_to, "gmail_q": gmail_q})

    try:
        creds = build_credentials(c)
        drive_svc = build_drive_service(creds)
    except Exception as exc:
        log.error("monthly aborted -- Drive auth failed", extra={"run_id": run_id, "error": str(exc)})
        return JSONResponse(status_code=200, content={
            "status": "error", "run_id": run_id, "month": month_str,
            "email_count": 0, "note": "OAuth credential refresh failed",
        })

    if c.drive_email_archive_folder_id:
        # Archive mode: scan Drive folders matching the month range.
        log.info("monthly -- using Drive archive",
                 extra={"run_id": run_id, "date_range": f"{date_from}~{date_to}"})
        messages_with_analysis = collect_archive_for_daily(
            drive_svc, c.drive_email_archive_folder_id,
            date_from, date_to,
            c.anthropic_api_key, c.local_output_dir, run_id,
            drive_output_folder_id=c.drive_output_folder_id,
        )
    else:
        messages_with_analysis = _collect_messages(c, creds, gmail_q, run_id, "monthly")

    email_count = len(messages_with_analysis)
    log.info("monthly -- collection complete", extra={"run_id": run_id, "email_count": email_count})

    md_name = filename_for_month(month_str)
    md_content = compose_monthly(messages_with_analysis, month_str, date_from, date_to, c.timezone)

    monthly_folder_id = (
        c.monthly_output_folder_id
        or c.weekly_output_folder_id
        or c.daily_output_folder_id
        or c.drive_output_folder_id
    )
    try:
        upsert_markdown(drive_svc, monthly_folder_id, md_name, md_content)
        log.info("monthly report upserted to Drive", extra={"run_id": run_id, "md_name": md_name})
    except Exception as exc:
        log.error("monthly -- upsert failed", extra={"run_id": run_id, "error": str(exc)})
        return JSONResponse(status_code=200, content={
            "status": "error", "run_id": run_id, "month": month_str,
            "email_count": email_count, "note": "Drive upsert failed",
        })

    local_monthly_dir = (
        c.local_monthly_output_dir
        or c.local_weekly_output_dir
        or c.local_daily_output_dir
        or c.local_output_dir
    )
    if local_monthly_dir:
        try:
            out_dir = Path(local_monthly_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / md_name).write_text(md_content, encoding="utf-8")
            log.info("monthly report written locally", extra={"run_id": run_id, "md_name": md_name})
        except Exception as exc:
            log.warning("monthly -- local write failed", extra={"run_id": run_id, "error": str(exc)})

    log.info("monthly complete", extra={"run_id": run_id, "month": month_str, "email_count": email_count})
    return JSONResponse(status_code=200, content={
        "status": "ok", "run_id": run_id, "month": month_str, "email_count": email_count,
    })
