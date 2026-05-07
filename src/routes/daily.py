"""POST /daily — generate overnight email digest (Daily Note)."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from src import config as cfg_module
from src.config import TEAM_MEMBERS
from src.auth import build_credentials, build_drive_service
from src.drive_client import download_file_content, find_file_by_name, upsert_markdown
from src.md_writer import compose, filename_for_subject, parse_preserved_fields, parse_todo_checks
from src.summarizer import _fallback_summary
from src.archive_scanner import collect_archive_for_daily
from src.daily_writer import compose_daily, filename_for_date, merge_daily, parse_checked_items
from src.dashboard_writer import compose_assignee_page, filename_for_assignee
from src.dependencies import _collect_messages

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/daily", summary="Generate overnight email digest (Daily Note)")
def daily(
    date: str | None = Query(
        None,
        description="Override target date (YYYY-MM-DD). Defaults to today. "
                    "Use this to regenerate a past day's note.",
    ),
) -> JSONResponse:
    """Collects emails from 18:00 of the previous day to 08:59 of the target date
    and writes a single Obsidian Daily Note: YYYY-MM-DD.md.

    Intended to be triggered by Cloud Scheduler at 09:00 every weekday.
    Pass ?date=YYYY-MM-DD to regenerate a specific past day.

    Response shape (always HTTP 200):
    {
      "status":      "ok" | "skipped" | "error",
      "run_id":      "<8-char id>",
      "date":        "2025-04-02",
      "email_count": <int>,
      "note":        "..."   // only when status != "ok"
    }
    """
    run_id = uuid.uuid4().hex[:8]
    c = cfg_module.load()

    log.info("daily started", extra={"run_id": run_id})

    # ── Config guard ─────────────────────────────────────────────────── #
    missing = cfg_module.validate_for_sync(c)
    if missing:
        return JSONResponse(
            status_code=200,
            content={
                "status": "skipped",
                "run_id": run_id,
                "date": "",
                "email_count": 0,
                "note": f"set these env vars to enable daily: {missing}",
            },
        )

    # ── Time window ──────────────────────────────────────────────────── #
    try:
        tz = ZoneInfo(c.timezone)
    except Exception:
        tz = ZoneInfo("Asia/Seoul")

    if date:
        try:
            target = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            return JSONResponse(status_code=200, content={
                "status": "error", "run_id": run_id, "date": date,
                "email_count": 0, "note": "date must be YYYY-MM-DD format",
            })
    else:
        target = datetime.now(tz)

    now = target
    date_str = now.strftime("%Y-%m-%d")

    # ── Weekend guard: 토/일은 Daily Note 생성하지 않음 ──────────────── #
    if now.weekday() in (5, 6):  # Saturday=5, Sunday=6
        return JSONResponse(status_code=200, content={
            "status": "skipped", "run_id": run_id, "date": date_str,
            "email_count": 0, "note": "weekend — no daily note generated",
        })

    # Determine email collection window based on day of week.
    # Collection window: previous day 18:00 ~ today 09:00
    # Monday:    Friday  18:00 ~ Monday 09:00 (covers weekend)
    # Tue–Fri:   Yesterday 18:00 ~ Today 09:00
    # Sat/Sun:   Friday 18:00 ~ Today 09:00 (fallback, normally not scheduled)
    if now.weekday() == 0:  # Monday
        range_start_day = now - timedelta(days=3)  # Friday
    elif now.weekday() in (5, 6):  # Saturday/Sunday
        days_since_fri = now.weekday() - 4
        range_start_day = now - timedelta(days=days_since_fri)
    else:  # Tue–Fri
        range_start_day = now - timedelta(days=1)
    period_start = range_start_day.replace(hour=18, minute=0, second=0, microsecond=0)
    period_end = now  # 현재 시각까지 (2시간마다 누적 업데이트)
    period_label_start = period_start.strftime("%Y-%m-%d %H:%M")
    period_label_end = period_end.strftime("%Y-%m-%d %H:%M")

    # Gmail query: Unix timestamp range
    gmail_q = f"after:{int(period_start.timestamp())} before:{int(period_end.timestamp())}"
    log.info("daily -- time window",
             extra={"run_id": run_id, "period_start": period_label_start,
                    "period_end": period_label_end, "gmail_q": gmail_q})

    # ── Build Drive service ──────────────────────────────────────────── #
    try:
        creds = build_credentials(c)
        drive_svc = build_drive_service(creds)
    except Exception as exc:
        log.error("daily aborted -- Drive auth failed",
                  extra={"run_id": run_id, "error": str(exc)})
        return JSONResponse(
            status_code=200,
            content={
                "status": "error",
                "run_id": run_id,
                "date": date_str,
                "email_count": 0,
                "note": "OAuth credential refresh failed",
            },
        )

    # ── Collect messages ─────────────────────────────────────────────── #
    # When DRIVE_EMAIL_ARCHIVE_FOLDER_ID is set, use the shared Drive
    # archive as data source instead of Gmail.
    if c.drive_email_archive_folder_id:
        # Archive mode: scan Drive folders matching the date range.
        # Archive folders only have date granularity (YYMMDD), so use the
        # target date only.  Previous day's emails are already covered by
        # that day's daily note; including them here would cause duplicates.
        date_range_start = date_str
        date_range_end = date_str
        log.info("daily -- using Drive archive",
                 extra={"run_id": run_id, "date_range": f"{date_range_start}~{date_range_end}"})
        messages_with_summaries = collect_archive_for_daily(
            drive_svc, c.drive_email_archive_folder_id,
            date_range_start, date_range_end,
            c.anthropic_api_key, c.local_output_dir, run_id,
            drive_output_folder_id=c.drive_output_folder_id,
        )
    else:
        # Gmail mode: collect overnight messages from all accounts.
        messages_with_summaries = _collect_messages(c, creds, gmail_q, run_id, "daily")

        # Create/update individual email notes so daily wiki-links resolve.
        local_note_dir = c.local_output_dir
        if local_note_dir and messages_with_summaries:
            note_dir = Path(local_note_dir)
            note_dir.mkdir(parents=True, exist_ok=True)
            individual_at = datetime.now(tz=timezone.utc).isoformat()
            for msg, ar in messages_with_summaries:
                try:
                    local_name = filename_for_subject(msg.subject)
                    local_path = note_dir / local_name
                    # Preserve user-edited fields from existing note.
                    pf_note: dict[str, str] | None = None
                    checked_todos: set[str] = set()
                    if local_path.exists():
                        try:
                            existing_text = local_path.read_text(encoding="utf-8")
                            pf_note = parse_preserved_fields(existing_text)
                            checked_todos = parse_todo_checks(existing_text)
                        except Exception:
                            pass
                    summary = ar.summary
                    if not summary and msg.body_text and msg.body_text.strip():
                        summary = _fallback_summary(msg.body_text)
                    note_md = compose(
                        msg, [], individual_at, summary, "", ar,
                        preserved_fields=pf_note, checked_todos=checked_todos,
                    )
                    local_path.write_text(note_md, encoding="utf-8")
                    log.info("individual note written",
                             extra={"run_id": run_id, "file": local_name})
                except Exception as exc:
                    log.warning("individual note write failed",
                                extra={"run_id": run_id, "error": str(exc)})

    email_count = len(messages_with_summaries)
    log.info("daily -- collection complete",
             extra={"run_id": run_id, "email_count": email_count})

    # ── Compose & write Daily Note ───────────────────────────────────── #
    local_daily_dir = c.local_daily_output_dir or c.local_output_dir
    daily_folder_name = Path(local_daily_dir).name if local_daily_dir else "TeamWorkHub_Daily"
    note_folder_name = Path(c.local_output_dir).name if c.local_output_dir else "TeamWorkHub"

    md_name = filename_for_date(date_str)

    # Read existing daily note (local → Drive fallback).
    existing_content: str | None = None
    if local_daily_dir:
        local_daily_path = Path(local_daily_dir) / md_name
        if local_daily_path.exists():
            try:
                existing_content = local_daily_path.read_text(encoding="utf-8")
            except Exception as exc:
                log.debug("daily -- could not read local daily note",
                          extra={"run_id": run_id, "error": str(exc)})
    if existing_content is None:
        # Fallback: try reading from Drive.
        daily_folder_id = c.daily_output_folder_id or c.drive_output_folder_id
        try:
            existing_file = find_file_by_name(drive_svc, md_name, daily_folder_id)
            if existing_file is not None:
                existing_content = download_file_content(drive_svc, existing_file.id)
        except Exception as exc:
            log.debug("daily -- could not read Drive daily note",
                      extra={"run_id": run_id, "error": str(exc)})

    if existing_content is not None:
        md_content = merge_daily(
            existing_content, messages_with_summaries,
            period_label_start, period_label_end, c.timezone,
            note_folder_name,
        )
        log.info("daily -- merged into existing note",
                 extra={"run_id": run_id, "date": date_str})
    else:
        md_content = compose_daily(
            messages_with_summaries, date_str,
            period_label_start, period_label_end, c.timezone,
            daily_folder_name,
            note_folder_name,
        )

    daily_folder_id = c.daily_output_folder_id or c.drive_output_folder_id
    try:
        upsert_markdown(drive_svc, daily_folder_id, md_name, md_content)
        log.info("daily note upserted to Drive",
                 extra={"run_id": run_id, "md_name": md_name})
    except Exception as exc:
        log.error("daily -- upsert failed",
                  extra={"run_id": run_id, "error": str(exc)})
        return JSONResponse(
            status_code=200,
            content={
                "status": "error",
                "run_id": run_id,
                "date": date_str,
                "email_count": email_count,
                "note": "Drive upsert failed",
            },
        )
    if local_daily_dir:
        try:
            out_dir = Path(local_daily_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / md_name).write_text(md_content, encoding="utf-8")
            log.info("daily note written locally",
                     extra={"run_id": run_id, "md_name": md_name})
        except Exception as exc:
            log.warning("daily -- local write failed",
                        extra={"run_id": run_id, "error": str(exc)})

    # Write per-assignee pages to dashboard dir if configured (team members only).
    if c.local_dashboard_dir:
        unique_assignees = sorted({
            name for _, ar in messages_with_summaries for name in ar.assignees
            if name in TEAM_MEMBERS
        })
        try:
            dash_dir = Path(c.local_dashboard_dir)
            dash_dir.mkdir(parents=True, exist_ok=True)
            for assignee in unique_assignees:
                page_content = compose_assignee_page(assignee, daily_folder_name, note_folder=note_folder_name)
                page_name = filename_for_assignee(assignee)
                (dash_dir / page_name).write_text(page_content, encoding="utf-8")
                log.info("assignee page written",
                         extra={"run_id": run_id, "assignee": assignee})
        except Exception as exc:
            log.warning("daily -- assignee page write failed",
                        extra={"run_id": run_id, "error": str(exc)})

    log.info("daily complete",
             extra={"run_id": run_id, "date": date_str, "email_count": email_count})

    return JSONResponse(
        status_code=200,
        content={"status": "ok", "run_id": run_id, "date": date_str, "email_count": email_count},
    )
