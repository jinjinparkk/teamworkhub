"""POST /scan-archive — scan Drive mail archive and create Obsidian notes."""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src import config as cfg_module
from src.auth import build_credentials, build_drive_service
from src.archive_scanner import scan_archive_folders

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/scan-archive", summary="Scan Drive mail archive and create Obsidian notes")
def scan_archive() -> JSONResponse:
    """Reads sub-folders from a shared Drive folder (mail archive), downloads
    본문.md from each, analyzes with Claude, and writes Obsidian notes locally.

    Response shape (always HTTP 200):
    {
      "status":    "ok" | "skipped" | "partial" | "error",
      "run_id":    "<8-char id>",
      "processed": <int>,
      "skipped":   <int>,
      "errors":    <int>,
      "note":      "..."   // only when status != "ok"
    }
    """
    run_id = uuid.uuid4().hex[:8]
    c = cfg_module.load()

    log.info("scan-archive started", extra={"run_id": run_id})

    # ── Config guard ─────────────────────────────────────────────────── #
    missing = cfg_module.validate_for_scan_archive(c)
    if missing:
        log.warning("scan-archive skipped -- missing required env vars",
                    extra={"run_id": run_id, "missing": missing})
        return JSONResponse(status_code=200, content={
            "status": "skipped",
            "run_id": run_id,
            "processed": 0,
            "skipped": 0,
            "errors": 0,
            "note": f"set these env vars to enable scan-archive: {missing}",
        })

    # ── Build Drive service ──────────────────────────────────────────── #
    try:
        creds = build_credentials(c)
        drive_svc = build_drive_service(creds)
    except Exception as exc:
        log.error("scan-archive aborted -- Drive auth failed",
                  extra={"run_id": run_id, "error": str(exc)})
        return JSONResponse(status_code=200, content={
            "status": "error",
            "run_id": run_id,
            "processed": 0,
            "skipped": 0,
            "errors": 1,
            "note": "OAuth credential refresh failed",
        })

    # ── Scan ─────────────────────────────────────────────────────────── #
    local_dir = c.local_output_dir
    sr = scan_archive_folders(
        drive_svc, c.drive_email_archive_folder_id,
        c.anthropic_api_key, local_dir, run_id,
    )

    # ── Final status ─────────────────────────────────────────────────── #
    if sr.errors == 0:
        status = "ok"
        note = ""
    elif sr.processed > 0:
        status = "partial"
        note = f"{sr.errors} folder(s) failed; {sr.processed} succeeded"
    else:
        status = "error"
        note = f"all {sr.errors} folder(s) failed"

    log.info("scan-archive complete", extra={
        "run_id": run_id, "processed": sr.processed,
        "skipped": sr.skipped, "errors": sr.errors,
    })

    body: dict = {
        "status": status,
        "run_id": run_id,
        "processed": sr.processed,
        "skipped": sr.skipped,
        "errors": sr.errors,
    }
    if note:
        body["note"] = note

    return JSONResponse(status_code=200, content=body)
