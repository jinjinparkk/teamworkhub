"""POST /backup — backup Drive work folders as ZIP."""
from __future__ import annotations

import io
import logging
import uuid
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src import config as cfg_module
from src.auth import build_credentials, build_drive_service
from src.drive_client import (
    download_file_content,
    find_file_by_name,
    list_files_in_folder,
    upload_binary,
)

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/backup", summary="Backup Drive work folders as ZIP")
def backup() -> JSONResponse:
    """Downloads all files from 4 Drive work folders (TeamWorkHub, Daily,
    Weekly, Monthly), creates an in-memory ZIP archive, and uploads it to
    the backup folder on Drive.

    Idempotency: if backup_YYYY-MM-DD.zip already exists, the run is skipped.

    Response shape (always HTTP 200):
    {
      "status":       "ok" | "skipped" | "error",
      "run_id":       "<8-char id>",
      "backup_file":  "backup_2026-05-04.zip",
      "file_count":   <int>,
      "size_bytes":   <int>,
      "note":         "..."   // only when status != "ok"
    }
    """
    run_id = uuid.uuid4().hex[:8]
    c = cfg_module.load()

    log.info("backup started", extra={"run_id": run_id})

    # ── Config guard ─────────────────────────────────────────────────── #
    missing = cfg_module.validate_for_backup(c)
    if missing:
        log.warning("backup skipped -- missing required env vars",
                    extra={"run_id": run_id, "missing": missing})
        return JSONResponse(status_code=200, content={
            "status": "skipped",
            "run_id": run_id,
            "backup_file": "",
            "file_count": 0,
            "size_bytes": 0,
            "note": f"set these env vars to enable backup: {missing}",
        })

    # ── Determine backup filename ────────────────────────────────────── #
    try:
        tz = ZoneInfo(c.timezone)
    except Exception:
        tz = ZoneInfo("Asia/Seoul")

    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    backup_filename = f"backup_{today_str}.zip"

    # ── Build Drive service ──────────────────────────────────────────── #
    try:
        creds = build_credentials(c)
        drive_svc = build_drive_service(creds)
    except Exception as exc:
        log.error("backup aborted -- Drive auth failed",
                  extra={"run_id": run_id, "error": str(exc)})
        return JSONResponse(status_code=200, content={
            "status": "error",
            "run_id": run_id,
            "backup_file": backup_filename,
            "file_count": 0,
            "size_bytes": 0,
            "note": "OAuth credential refresh failed",
        })

    # ── Idempotency check ────────────────────────────────────────────── #
    try:
        existing = find_file_by_name(drive_svc, backup_filename, c.backup_output_folder_id)
        if existing:
            log.info("backup already exists -- skipped",
                     extra={"run_id": run_id, "backup_file": backup_filename})
            return JSONResponse(status_code=200, content={
                "status": "skipped",
                "run_id": run_id,
                "backup_file": backup_filename,
                "file_count": 0,
                "size_bytes": 0,
                "note": "backup already exists for today",
            })
    except Exception as exc:
        log.error("backup -- idempotency check failed",
                  extra={"run_id": run_id, "error": str(exc)})
        return JSONResponse(status_code=200, content={
            "status": "error",
            "run_id": run_id,
            "backup_file": backup_filename,
            "file_count": 0,
            "size_bytes": 0,
            "note": f"Drive query failed: {exc}",
        })

    # ── Collect files from work folders ──────────────────────────────── #
    folder_ids = [
        ("TeamWorkHub", c.drive_output_folder_id),
        ("TeamWorkHub_Daily", c.daily_output_folder_id),
        ("TeamWorkHub_Weekly", c.weekly_output_folder_id),
        ("TeamWorkHub_Monthly", c.monthly_output_folder_id),
    ]
    # Filter out unconfigured folders
    folder_ids = [(name, fid) for name, fid in folder_ids if fid]

    zip_buffer = io.BytesIO()
    file_count = 0

    try:
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for folder_name, folder_id in folder_ids:
                try:
                    files = list_files_in_folder(drive_svc, folder_id)
                except Exception as exc:
                    log.warning(f"backup -- could not list folder {folder_name}",
                                extra={"run_id": run_id, "folder_id": folder_id,
                                       "error": str(exc)})
                    continue

                for df in files:
                    try:
                        content = download_file_content(drive_svc, df.file_id)
                        zf.writestr(f"{folder_name}/{df.name}", content)
                        file_count += 1
                    except Exception as exc:
                        log.warning("backup -- file download failed",
                                    extra={"run_id": run_id, "file_id": df.file_id,
                                           "file_name": df.name, "error": str(exc)})
                        continue
    except Exception as exc:
        log.error("backup -- zip creation failed",
                  extra={"run_id": run_id, "error": str(exc)})
        return JSONResponse(status_code=200, content={
            "status": "error",
            "run_id": run_id,
            "backup_file": backup_filename,
            "file_count": file_count,
            "size_bytes": 0,
            "note": f"ZIP creation failed: {exc}",
        })

    zip_bytes = zip_buffer.getvalue()
    size_bytes = len(zip_bytes)

    # ── Upload ZIP to backup folder ──────────────────────────────────── #
    try:
        upload_binary(
            drive_svc,
            c.backup_output_folder_id,
            backup_filename,
            zip_bytes,
            "application/zip",
        )
        log.info("backup uploaded",
                 extra={"run_id": run_id, "backup_file": backup_filename,
                        "file_count": file_count, "size_bytes": size_bytes})
    except Exception as exc:
        log.error("backup -- upload failed",
                  extra={"run_id": run_id, "error": str(exc)})
        return JSONResponse(status_code=200, content={
            "status": "error",
            "run_id": run_id,
            "backup_file": backup_filename,
            "file_count": file_count,
            "size_bytes": size_bytes,
            "note": f"Drive upload failed: {exc}",
        })

    log.info("backup complete",
             extra={"run_id": run_id, "file_count": file_count, "size_bytes": size_bytes})
    return JSONResponse(status_code=200, content={
        "status": "ok",
        "run_id": run_id,
        "backup_file": backup_filename,
        "file_count": file_count,
        "size_bytes": size_bytes,
    })
