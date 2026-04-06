"""FastAPI application — single HTTP entrypoint for Cloud Scheduler.

Endpoints
─────────
GET  /health   Liveness probe (no auth required by design).
POST /sync     One sync cycle.  Cloud Scheduler calls this on a schedule.

Cloud Run injects PORT; uvicorn reads it in __main__.py.
"""
from __future__ import annotations

import ast
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from typing import Optional

from src import config as cfg_module
from src.auth import build_credentials, build_drive_service, build_gmail_service
from src.drive_client import (
    find_file_by_name,
    get_or_create_folder,
    upload_attachment,
    upsert_markdown,
)
from src.gmail_client import download_attachment, fetch_message, list_messages
from src.assignee import extract_assignees
from src.daily_writer import compose_daily, filename_for_date
from src.dashboard_writer import (
    compose_dashboard,
    compose_assignee_page,
    filename_for_dashboard,
    filename_for_assignee,
)
from src.logging_cfg import configure_logging
from src.md_writer import compose, filename_for, filename_for_subject
from src.summarizer import analyze_email, summarize
from src.monthly_writer import compose_monthly, filename_for_month
from src.weekly_writer import compose_weekly, filename_for_week

# Configure logging once at import time so the first uvicorn log is formatted.
configure_logging()
log = logging.getLogger(__name__)

app = FastAPI(
    title="TeamWorkHub",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
)


# ── Shared helper ──────────────────────────────────────────────────── #

def _collect_messages(
    c: cfg_module.Config,
    creds,
    gmail_q: str,
    run_id: str,
    label: str,
) -> list[tuple]:
    """Fetch and analyze Gmail messages matching *gmail_q* across all accounts.

    Args:
        c:        Loaded Config.
        creds:    Drive-level credentials (reused for single-account Gmail).
        gmail_q:  Gmail search query string (e.g. "after:... before:...").
        run_id:   8-char correlation ID for structured logs.
        label:    Human-readable endpoint label for log messages ("daily" | "weekly" | "monthly").

    Returns a list of (ParsedMessage, AnalysisResult) tuples; never raises.
    """
    account_list = list(c.gmail_accounts) if c.gmail_accounts else [
        cfg_module.AccountConfig(email="", refresh_token="")
    ]
    results: list[tuple] = []

    for account in account_list:
        try:
            if account.refresh_token:
                acc_creds = build_credentials(c, refresh_token=account.refresh_token)
                gmail_svc = build_gmail_service(acc_creds)
            else:
                gmail_svc = build_gmail_service(creds)
        except Exception as exc:
            log.error(f"{label} -- OAuth failed for account",
                      extra={"run_id": run_id, "account": account.email, "error": str(exc)})
            continue

        try:
            stubs = list_messages(gmail_svc, c.gmail_label_id, c.max_messages_per_run, q=gmail_q)
        except Exception as exc:
            log.error(f"{label} -- list_messages failed",
                      extra={"run_id": run_id, "account": account.email, "error": str(exc)})
            continue

        for stub in stubs:
            msg_id = stub.get("id", "")
            try:
                msg = fetch_message(gmail_svc, msg_id)
            except Exception as exc:
                log.error(f"{label} -- fetch_message failed",
                          extra={"run_id": run_id, "message_id": msg_id, "error": str(exc)})
                continue

            analysis = analyze_email(msg.subject, msg.sender, msg.body_text, c.gemini_api_key)
            if not analysis.assignees:
                analysis.assignees = extract_assignees(
                    msg.subject, msg.sender, msg.body_text, ""
                )
            results.append((msg, analysis))
            log.info(f"{label} -- message collected",
                     extra={"run_id": run_id, "message_id": msg_id})
            # Avoid Gemini free-tier rate limit (15 RPM)
            if c.gemini_api_key:
                time.sleep(1)

    return results


# ── Health ─────────────────────────────────────────────────────────── #

@app.get("/health", summary="Liveness probe")
def health() -> dict:
    """Returns 200 immediately.  No auth required.
    Cloud Run health-check and Cloud Scheduler OIDC pre-flight both use this."""
    return {"status": "ok", "service": "teamworkhub"}


# ── Sync ───────────────────────────────────────────────────────────── #

@app.post("/sync", summary="Run one Gmail→Drive sync cycle")
def sync() -> JSONResponse:
    """Triggered by Cloud Scheduler (HTTP POST with OIDC token).

    Supports both single-account mode (GOOGLE_OAUTH_REFRESH_TOKEN) and
    multi-account mode (GMAIL_ACCOUNTS_JSON array).

    Response shape (always HTTP 200):
    {
      "status":    "ok" | "skipped" | "partial" | "error",
      "run_id":    "<8-char correlation id>",
      "processed": <int>,
      "skipped":   <int>,
      "errors":    <int>,
      "note":      "<human-readable message>"   // present when status != "ok"
    }

    Errors inside the sync loop are counted and returned in the JSON body
    rather than raising HTTP 5xx — this prevents Cloud Scheduler from
    retrying on partial failures.
    """
    run_id = uuid.uuid4().hex[:8]
    c = cfg_module.load()

    log.info("sync started", extra={"run_id": run_id, "label": c.gmail_label_id})

    # ── Config guard ────────────────────────────────────────────────── #
    missing = cfg_module.validate_for_sync(c)
    if missing:
        log.warning(
            "sync skipped — missing required env vars",
            extra={"run_id": run_id, "missing": missing},
        )
        return JSONResponse(
            status_code=200,
            content={
                "status": "skipped",
                "run_id": run_id,
                "processed": 0,
                "skipped": 0,
                "errors": 0,
                "note": f"set these env vars to enable sync: {missing}",
            },
        )

    # ── Build Drive service (shared across all accounts) ─────────────── #
    try:
        creds = build_credentials(c)
        drive_svc = build_drive_service(creds)
    except Exception as exc:
        log.error(
            "sync aborted — could not build Drive service",
            extra={"run_id": run_id, "error": str(exc)},
        )
        return JSONResponse(
            status_code=200,
            content={
                "status": "error",
                "run_id": run_id,
                "processed": 0,
                "skipped": 0,
                "errors": 1,
                "note": "OAuth credential refresh failed — check token env vars",
            },
        )

    # ── Determine Gmail accounts ─────────────────────────────────────── #
    # Multi-account: each AccountConfig has its own refresh_token for Gmail.
    # Single-account (backward compat): reuse the Drive credentials for Gmail.
    if c.gmail_accounts:
        account_list = list(c.gmail_accounts)
    else:
        account_list = [cfg_module.AccountConfig(email="", refresh_token="")]

    processed_at = datetime.now(tz=timezone.utc).isoformat()
    processed = 0
    skipped = 0
    errors = 0

    # ── Per-account loop ─────────────────────────────────────────────── #
    for account in account_list:

        # Build Gmail service for this account.
        try:
            if account.refresh_token:
                # Multi-account: fresh credentials per account.
                acc_creds = build_credentials(c, refresh_token=account.refresh_token)
                gmail_svc = build_gmail_service(acc_creds)
            else:
                # Single-account: reuse the Drive credentials already built.
                gmail_svc = build_gmail_service(creds)
        except Exception as exc:
            log.error(
                "OAuth failed for account — skipping",
                extra={"run_id": run_id, "account": account.email, "error": str(exc)},
            )
            errors += 1
            continue

        # List messages for this account.
        try:
            message_stubs = list_messages(
                gmail_svc, c.gmail_label_id, c.max_messages_per_run
            )
        except Exception as exc:
            log.error(
                "list_messages failed — skipping account",
                extra={"run_id": run_id, "account": account.email, "error": str(exc)},
            )
            errors += 1
            continue

        # ── Per-message pipeline ──────────────────────────────────────── #
        for stub in message_stubs:
            msg_id = stub.get("id", "")
            md_name = filename_for(msg_id, account.email)

            # Idempotency check: if the .md commit-marker already exists, skip.
            try:
                existing_md = find_file_by_name(drive_svc, md_name, c.drive_output_folder_id)
            except Exception as exc:
                log.error(
                    "Drive find_file failed — skipping message",
                    extra={"run_id": run_id, "message_id": msg_id, "error": str(exc)},
                )
                errors += 1
                continue

            if existing_md is not None:
                log.info(
                    "message already synced — skipped",
                    extra={"run_id": run_id, "message_id": msg_id},
                )
                skipped += 1
                # Local migration: old files were saved as twh_*.md.
                # If a subject-based file does not yet exist, create it from
                # the legacy local file so Obsidian wiki-links resolve.
                if c.local_output_dir:
                    old_local = Path(c.local_output_dir) / md_name
                    if old_local.exists():
                        try:
                            content = old_local.read_text(encoding="utf-8")
                            # Extract subject value from old YAML frontmatter.
                            _subj_re = re.compile(
                                r'^subject:\s*"?(.*?)"?\s*$', re.MULTILINE
                            )
                            m_subj = _subj_re.search(content)
                            if m_subj:
                                raw_subj = m_subj.group(1).strip()
                                if raw_subj:
                                    new_local = (
                                        Path(c.local_output_dir)
                                        / filename_for_subject(raw_subj)
                                    )
                                    if not new_local.exists():
                                        new_local.write_text(content, encoding="utf-8")
                                        log.info(
                                            "local note migrated to subject-based name",
                                            extra={
                                                "run_id": run_id,
                                                "message_id": msg_id,
                                                "new_name": new_local.name,
                                            },
                                        )
                        except Exception as exc:
                            log.debug(
                                "local migration skipped",
                                extra={"run_id": run_id, "error": str(exc)},
                            )
                continue

            # Fetch full message.
            try:
                msg = fetch_message(gmail_svc, msg_id)
            except Exception as exc:
                log.error(
                    "fetch_message failed — skipping message",
                    extra={"run_id": run_id, "message_id": msg_id, "error": str(exc)},
                )
                errors += 1
                continue

            # Upload attachments (idempotent — drive_client checks before writing).
            drive_files = []
            for att in msg.attachments:
                try:
                    raw_bytes = download_attachment(gmail_svc, msg_id, att.attachment_id)
                    df = upload_attachment(
                        drive_svc,
                        c.drive_output_folder_id,
                        msg_id,
                        att.filename,
                        raw_bytes,
                        att.mime_type,
                    )
                    drive_files.append(df)
                except Exception as exc:
                    log.error(
                        "attachment upload failed — continuing without it",
                        extra={
                            "run_id": run_id,
                            "message_id": msg_id,
                            "att_filename": att.filename,
                            "error": str(exc),
                        },
                    )
                    # Non-fatal: continue with remaining attachments.

            # Analyze with Gemini (optional — defaults when key not set).
            ar = analyze_email(msg.subject, msg.sender, msg.body_text, c.gemini_api_key)
            if not ar.assignees:
                ar.assignees = extract_assignees(
                    msg.subject, msg.sender, msg.body_text, ""
                )

            # Compose and upsert Markdown to Drive (commit point).
            # Drive uses md_name (twh_{msgId}.md) for idempotency check.
            try:
                md_content = compose(msg, drive_files, processed_at, ar.summary, account.email, ar)
                upsert_markdown(drive_svc, c.drive_output_folder_id, md_name, md_content)
            except Exception as exc:
                log.error(
                    "upsert_markdown failed",
                    extra={"run_id": run_id, "message_id": msg_id, "error": str(exc)},
                )
                errors += 1
                continue

            # Write .md to local Obsidian vault if configured.
            # Local file uses subject-based name for Obsidian wiki-link compatibility.
            if c.local_output_dir:
                try:
                    out_dir = Path(c.local_output_dir)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    local_name = filename_for_subject(msg.subject)
                    (out_dir / local_name).write_text(md_content, encoding="utf-8")
                    log.info("markdown written locally", extra={"run_id": run_id, "message_id": msg_id})
                except Exception as exc:
                    log.warning("local write failed -- Drive copy still saved",
                                extra={"run_id": run_id, "message_id": msg_id, "error": str(exc)})

            processed += 1
            log.info(
                "message synced",
                extra={
                    "run_id": run_id,
                    "message_id": msg_id,
                    "account": account.email,
                    "attachments": len(drive_files),
                },
            )

    # ── Final status ─────────────────────────────────────────────────── #
    if errors == 0:
        status = "ok"
        note = ""
    elif processed > 0:
        status = "partial"
        note = f"{errors} message(s) failed; {processed} succeeded"
    else:
        status = "error"
        note = f"all {errors} message(s) failed"

    log.info(
        "sync complete",
        extra={
            "run_id": run_id,
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
        },
    )

    body: dict = {
        "status": status,
        "run_id": run_id,
        "processed": processed,
        "skipped": skipped,
        "errors": errors,
    }
    if note:
        body["note"] = note

    return JSONResponse(status_code=200, content=body)

    # Phase 2 (do NOT implement): Gmail watch + Pub/Sub push → replace polling above.


# ── Daily digest ───────────────────────────────────────────────────── #

@app.post("/daily", summary="Generate overnight email digest (Daily Note)")
def daily(
    date: Optional[str] = Query(
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
    period_end = now.replace(hour=8, minute=59, second=59, microsecond=0)
    period_start = (now - timedelta(days=1)).replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    period_label_start = period_start.strftime("%Y-%m-%d %H:%M")
    period_label_end = period_end.strftime("%Y-%m-%d %H:%M")

    # Gmail query: Unix timestamp range
    gmail_q = f"after:{int(period_start.timestamp())} before:{int(period_end.timestamp())}"

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

    # ── Collect overnight messages from all accounts ─────────────────── #
    messages_with_summaries = _collect_messages(c, creds, gmail_q, run_id, "daily")
    email_count = len(messages_with_summaries)
    log.info("daily -- collection complete",
             extra={"run_id": run_id, "email_count": email_count})

    # ── Compose & write Daily Note ───────────────────────────────────── #
    local_daily_dir = c.local_daily_output_dir or c.local_output_dir
    daily_folder_name = Path(local_daily_dir).name if local_daily_dir else "TeamWorkHub_Daily"

    md_name = filename_for_date(date_str)
    md_content = compose_daily(
        messages_with_summaries, date_str,
        period_label_start, period_label_end, c.timezone,
        daily_folder_name,
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

    # Write per-assignee pages to dashboard dir if configured.
    if c.local_dashboard_dir:
        unique_assignees = sorted({
            name for _, ar in messages_with_summaries for name in ar.assignees
        })
        try:
            dash_dir = Path(c.local_dashboard_dir)
            dash_dir.mkdir(parents=True, exist_ok=True)
            for assignee in unique_assignees:
                page_content = compose_assignee_page(assignee, daily_folder_name)
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


# ── Weekly digest ───────────────────────────────────────────────────── #

@app.post("/weekly", summary="Generate weekly email digest report")
def weekly() -> JSONResponse:
    """Collects emails from Monday 00:00 to Friday 23:59 (configured timezone)
    and writes a single Weekly Report: YYYY-WNN.md.

    Intended to be triggered by Cloud Scheduler at 18:00 every Friday.

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
    # Monday of current week
    monday = now - timedelta(days=now.weekday())
    week_start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
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


# ── Monthly digest ──────────────────────────────────────────────────── #

@app.post("/monthly", summary="Generate monthly email digest report")
def monthly() -> JSONResponse:
    """Collects emails for the current calendar month and writes YYYY-MM.md.

    Intended to be triggered by Cloud Scheduler on the last day of each month,
    or called manually at any time during the month.

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
    # First day of current month
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Last day of current month
    if now.month == 12:
        next_month_start = month_start.replace(year=now.year + 1, month=1)
    else:
        next_month_start = month_start.replace(month=now.month + 1)
    month_end = next_month_start - timedelta(seconds=1)

    month_str = now.strftime("%Y-%m")
    date_from = month_start.strftime("%Y-%m-%d")
    date_to = month_end.strftime("%Y-%m-%d")
    gmail_q = f"after:{int(month_start.timestamp())} before:{int(month_end.timestamp())}"

    try:
        creds = build_credentials(c)
        drive_svc = build_drive_service(creds)
    except Exception as exc:
        log.error("monthly aborted -- Drive auth failed", extra={"run_id": run_id, "error": str(exc)})
        return JSONResponse(status_code=200, content={
            "status": "error", "run_id": run_id, "month": month_str,
            "email_count": 0, "note": "OAuth credential refresh failed",
        })

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


# ── Dashboard ───────────────────────────────────────────────────────── #

@app.post("/dashboard", summary="Generate Dataview-powered Dashboard.md")
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
