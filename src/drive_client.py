"""Google Drive API client — step 4.

Idempotency contract (never violate):
─────────────────────────────────────
• Attachment filename pattern: ``{messageId}_{safe_original_name}``
  The filename is deterministic, so the same attachment is never uploaded twice.

• .md filename: caller-supplied (md_writer.filename_for, step 5); key is messageId.

• Before any write, query Drive for an existing file with that name in the
  target folder:
    found     → return as-is (DriveFile.created=False) — zero bytes written
    not found → create and return (DriveFile.created=True)

• upsert_markdown:  existing → update content (no duplicate); new → create.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

log = logging.getLogger(__name__)

_DRIVE_FIELDS  = "id,name,webViewLink"
_FOLDER_MIME   = "application/vnd.google-apps.folder"
_MD_MIME       = "text/markdown"
_MAX_NAME_LEN  = 100          # conservative; Drive's hard limit is 255
_UNSAFE_CHARS  = re.compile(r'[<>:"/\\|?*\x00-\x1f\s]')


# ── Data model ─────────────────────────────────────────────────────── #

@dataclass
class DriveFile:
    file_id: str
    name: str
    web_view_link: str
    created: bool   # True = new file written; False = already existed (idempotent)


# ── Private helpers ────────────────────────────────────────────────── #

def _safe_name_component(name: str, max_len: int = _MAX_NAME_LEN) -> str:
    """Replace unsafe characters with underscores, trim extremes, enforce length."""
    safe = _UNSAFE_CHARS.sub("_", name).strip("._")
    return (safe or "attachment")[:max_len]


def _safe_filename(message_id: str, original_name: str) -> str:
    """Return the deterministic Drive filename for an attachment.

    Pattern: ``{messageId}_{safe_original_name}``

    Examples
    ────────
    "msg001", "My Report.pdf"  →  "msg001_My_Report.pdf"
    "msg001", ""               →  "msg001_attachment"
    """
    return f"{message_id}_{_safe_name_component(original_name)}"


def _to_drive_file(raw: dict, created: bool) -> DriveFile:
    return DriveFile(
        file_id=raw["id"],
        name=raw["name"],
        web_view_link=raw.get("webViewLink", ""),
        created=created,
    )


def _escape_query(value: str) -> str:
    """Escape a value for use inside a Drive Files.list q= string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# ── Public API ─────────────────────────────────────────────────────── #

def find_file_by_name(service, filename: str, parent_id: str) -> DriveFile | None:
    """Return the first Drive file matching *filename* in *parent_id*, or None.

    Searches only non-trashed files in the specified parent folder.
    If multiple files share the same name, the first result is returned.
    """
    query = (
        f"name='{_escape_query(filename)}' "
        f"and '{_escape_query(parent_id)}' in parents "
        f"and trashed=false"
    )
    try:
        resp = service.files().list(
            q=query,
            fields=f"files({_DRIVE_FIELDS})",
            spaces="drive",
        ).execute()
    except HttpError:
        raise

    files = resp.get("files", [])
    if not files:
        return None
    return _to_drive_file(files[0], created=False)


def get_or_create_folder(service, name: str, parent_id: str) -> str:
    """Return the Drive folder id, creating the folder if it doesn't exist."""
    query = (
        f"mimeType='{_FOLDER_MIME}' "
        f"and name='{_escape_query(name)}' "
        f"and '{_escape_query(parent_id)}' in parents "
        f"and trashed=false"
    )
    resp = service.files().list(q=query, fields="files(id)", spaces="drive").execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]

    folder = service.files().create(
        body={"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]},
        fields="id",
    ).execute()
    log.info("Drive folder created", extra={"folder_name": name, "folder_id": folder["id"]})
    return folder["id"]


def upload_attachment(
    service,
    parent_id: str,
    message_id: str,
    original_filename: str,
    content: bytes,
    mime_type: str,
) -> DriveFile:
    """Upload attachment bytes; skip silently if the file already exists.

    Filename: ``{message_id}_{safe_original_filename}``

    Returns DriveFile.created=True on a new upload, False if the file was
    already present (idempotent run).
    """
    name = _safe_filename(message_id, original_filename)

    existing = find_file_by_name(service, name, parent_id)
    if existing:
        log.info(
            "attachment already in Drive — skipped",
            extra={"message_id": message_id, "att_name": name},
        )
        return existing

    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
    raw = service.files().create(
        body={"name": name, "parents": [parent_id]},
        media_body=media,
        fields=_DRIVE_FIELDS,
    ).execute()

    result = _to_drive_file(raw, created=True)
    log.info(
        "attachment uploaded",
        extra={"message_id": message_id, "att_name": name, "file_id": result.file_id},
    )
    return result


def upsert_markdown(
    service,
    parent_id: str,
    filename: str,
    content: str,
) -> DriveFile:
    """Create or overwrite a Markdown file in *parent_id*.

    Idempotency
    ───────────
    • File absent  → create new (created=True).
    • File present → overwrite content via files.update (created=False).
      The file stays in the same folder; no duplicate is ever created.
    """
    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")),
        mimetype=_MD_MIME,
        resumable=False,
    )

    existing = find_file_by_name(service, filename, parent_id)

    if existing:
        raw = service.files().update(
            fileId=existing.file_id,
            body={"name": filename},
            media_body=media,
            fields=_DRIVE_FIELDS,
        ).execute()
        result = _to_drive_file(raw, created=False)
        log.info("markdown updated", extra={"md_filename": filename, "file_id": result.file_id})
    else:
        raw = service.files().create(
            body={"name": filename, "mimeType": _MD_MIME, "parents": [parent_id]},
            media_body=media,
            fields=_DRIVE_FIELDS,
        ).execute()
        result = _to_drive_file(raw, created=True)
        log.info("markdown created", extra={"md_filename": filename, "file_id": result.file_id})

    return result
