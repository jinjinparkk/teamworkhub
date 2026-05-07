"""POST /sync — one Gmail→Drive sync cycle."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src import config as cfg_module
from src.auth import build_credentials, build_drive_service, build_gmail_service
from src.drive_client import find_file_by_name, upload_attachment, upsert_markdown
from src.gmail_client import download_attachment, fetch_message, list_messages
from src.assignee import extract_assignees
from src.md_writer import compose, filename_for, filename_for_subject, parse_preserved_fields
from src.summarizer import AnalysisResult, _fallback_summary, analyze_email

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/sync", summary="Run one Gmail→Drive sync cycle")
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
            "sync skipped -- missing required env vars",
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
            "sync aborted -- could not build Drive service",
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
                "note": "OAuth credential refresh failed -- check token env vars",
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
                "OAuth failed for account -- skipping",
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
                "list_messages failed -- skipping account",
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
                    "Drive find_file failed -- skipping message",
                    extra={"run_id": run_id, "message_id": msg_id, "error": str(exc)},
                )
                errors += 1
                continue

            if existing_md is not None:
                log.info(
                    "message already synced -- skipped",
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
                    "fetch_message failed -- skipping message",
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
                        "attachment upload failed -- continuing without it",
                        extra={
                            "run_id": run_id,
                            "message_id": msg_id,
                            "att_filename": att.filename,
                            "error": str(exc),
                        },
                    )
                    # Non-fatal: continue with remaining attachments.

            # Upload inline images and build cid_map.
            cid_map: dict[str, str] = {}
            for img in msg.inline_images:
                if not img.data:
                    continue
                try:
                    df = upload_attachment(
                        drive_svc,
                        c.drive_output_folder_id,
                        msg_id,
                        img.filename,
                        img.data,
                        img.mime_type,
                    )
                    cid_map[img.content_id] = df.web_view_link
                except Exception as exc:
                    log.warning(
                        "inline image upload failed -- continuing without it",
                        extra={
                            "run_id": run_id,
                            "message_id": msg_id,
                            "img_filename": img.filename,
                            "error": str(exc),
                        },
                    )

            # Analyze with Claude (optional — defaults when key not set).
            try:
                ar = analyze_email(msg.subject, msg.sender, msg.body_text, c.anthropic_api_key, msg.to, msg.cc)
                if not ar.assignees:
                    ar.assignees = extract_assignees(
                        msg.subject, msg.sender, msg.body_text, "", msg.to, msg.cc
                    )
            except Exception as exc:
                log.warning("analyze/extract failed, using fallback",
                            extra={"run_id": run_id, "message_id": msg_id, "error": str(exc)})
                ar = AnalysisResult(summary=_fallback_summary(msg.body_text))

            # Compose and upsert Markdown to Drive (commit point).
            # Drive uses md_name (twh_{msgId}.md) for idempotency check.
            # Preserve user-edited result/link fields from existing local note.
            pf: dict[str, str] | None = None
            if c.local_output_dir:
                local_prev = Path(c.local_output_dir) / filename_for_subject(msg.subject)
                if local_prev.exists():
                    try:
                        pf = parse_preserved_fields(local_prev.read_text(encoding="utf-8"))
                    except Exception:
                        pass
            try:
                md_content = compose(msg, drive_files, processed_at, ar.summary, account.email, ar, preserved_fields=pf, cid_map=cid_map or None)
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
