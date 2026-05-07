"""Drive mail archive scanner — reads shared Drive folders and creates Obsidian notes.

Expected Drive structure (under archive_folder_id):
    260420_김치성_결재요청/
        본문.md
        attachments/
            계약서.pdf
            견적서.xlsx

Folder name convention: ``YYMMDD_발신자_제목`` (also supports ``YYYY-MM-DD_발신자_제목``)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.assignee import extract_assignees
from src.drive_client import (
    download_file_content,
    list_files_in_folder,
    list_subfolders,
    upsert_markdown,
    DriveFile,
)
from src.md_writer import compose, filename_for_subject, parse_preserved_fields
from src.summarizer import AnalysisResult, analyze_email, _fallback_summary

log = logging.getLogger(__name__)

_FOLDER_NAME_ISO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})_(.+?)_(.+)$"
)
_FOLDER_NAME_SHORT_RE = re.compile(
    r"^(\d{6})_(.+?)_(.+)$"
)

_YAML_FRONT_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)


def _strip_yaml_frontmatter(text: str) -> str:
    """Remove YAML frontmatter (``---...---``) from 본문.md content."""
    return _YAML_FRONT_RE.sub("", text, count=1)


_RECIPIENT_TO_RE = re.compile(
    r"\*\*받는\s*사람:\*\*\s*(.+)", re.IGNORECASE
)
_RECIPIENT_CC_RE = re.compile(
    r"\*\*참조:\*\*\s*(.+)", re.IGNORECASE
)


def _parse_recipients_from_header(text: str) -> tuple[str, str]:
    """Extract 받는 사람 and 참조 from forwarding header before stripping.

    Returns (to_str, cc_str) — raw recipient strings as they appear in the header.
    """
    to_str = ""
    cc_str = ""
    for line in text.split("\n"):
        stripped = line.strip()
        m = _RECIPIENT_TO_RE.match(stripped)
        if m:
            to_str = m.group(1).strip()
            continue
        m = _RECIPIENT_CC_RE.match(stripped)
        if m:
            cc_str = m.group(1).strip()
            continue
    return to_str, cc_str


def _strip_forward_header(text: str) -> str:
    """Remove Outlook-style forwarding header from the body text.

    The forwarding header looks like::

        # 전달: 제목...
        **
        보낸 사람:** ...
        **보낸 날짜:** ...
        **받는 사람:** ...
        **참조:** ...
        **제목:** ...

    Everything up to and including the ``**제목:**`` line is stripped.
    """
    lines = text.split("\n")
    subject_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("**제목:**") or stripped.startswith("제목:**"):
            subject_idx = i
            break

    if subject_idx == -1:
        return text

    # Skip blank lines after the subject line
    start = subject_idx + 1
    while start < len(lines) and not lines[start].strip():
        start += 1

    return "\n".join(lines[start:])


def _yymmdd_to_iso(short: str) -> str:
    """Convert ``YYMMDD`` → ``YYYY-MM-DD``.  e.g. '260420' → '2026-04-20'."""
    dt = datetime.strptime(short, "%y%m%d")
    return dt.strftime("%Y-%m-%d")


@dataclass
class ScanResult:
    """Summary returned by scan_archive_folders()."""
    processed: int = 0
    skipped: int = 0
    errors: int = 0
    details: list[str] = field(default_factory=list)


@dataclass
class ArchiveMessage:
    """Typed replacement for SimpleNamespace used by compose() and daily_writer."""
    subject: str
    sender: str
    body_text: str
    to: str = ""
    cc: str = ""
    date_utc: str = ""
    attachments: list = field(default_factory=list)


def parse_folder_name(name: str) -> tuple[str, str, str] | None:
    """Parse folder name → (date_iso, sender, subject) or None.

    Supports both formats:
      ``YYYY-MM-DD_발신자_제목``  → ("2026-04-20", "김치성", "결재요청")
      ``YYMMDD_발신자_제목``      → ("2026-04-20", "김치성", "결재요청")
    """
    m = _FOLDER_NAME_ISO_RE.match(name)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = _FOLDER_NAME_SHORT_RE.match(name)
    if m:
        try:
            date_iso = _yymmdd_to_iso(m.group(1))
        except ValueError:
            return None
        return date_iso, m.group(2), m.group(3)
    return None


@dataclass
class _FolderResult:
    """Internal result from _process_single_folder()."""
    message: ArchiveMessage
    analysis: AnalysisResult
    md_content: str
    local_filename: str


def _process_single_folder(
    drive_svc,
    folder_id: str,
    folder_name: str,
    date_str: str,
    sender: str,
    subject: str,
    api_key: str,
    out_dir: Path | None,
    processed_at: str,
    run_id: str,
    *,
    write_local: bool = True,
    write_drive: bool = False,
    drive_output_folder_id: str = "",
) -> _FolderResult:
    """Process a single archive folder: download body, analyze, compose, optionally write.

    Raises on 본문.md not found or download failure (caller handles).
    Analysis/compose failures are handled internally with fallbacks.
    """
    full_subject = f"{date_str} {subject}"
    local_filename = filename_for_subject(full_subject)

    # 1. Preserved fields from existing local note
    preserved_fields: dict[str, str] | None = None
    local_already_existed = bool(out_dir and (out_dir / local_filename).exists())
    if local_already_existed:
        try:
            preserved_fields = parse_preserved_fields(
                (out_dir / local_filename).read_text(encoding="utf-8")
            )
        except Exception:
            pass

    # 2. Download 본문.md — raises on failure
    files = list_files_in_folder(drive_svc, folder_id)
    body_file = next((f for f in files if f.name == "본문.md"), None)
    if not body_file:
        raise FileNotFoundError(f"본문.md not found in {folder_name}")

    raw_body = download_file_content(drive_svc, body_file.file_id)
    raw_body = _strip_yaml_frontmatter(raw_body)
    to_str, cc_str = _parse_recipients_from_header(raw_body)

    # GMPD_DATA forwarded mails: keep original forwarding content intact
    # because the real content is below the forwarding header.
    is_gmpd_forward = (
        sender.upper().startswith("GMPD") or
        "GMPD" in subject.upper()
    ) and raw_body.lstrip().startswith("# 전달:")
    if is_gmpd_forward:
        body_text = raw_body
    else:
        body_text = _strip_forward_header(raw_body)

    # 3. Collect attachment links
    attachment_links: list[DriveFile] = []
    try:
        sfs = list_subfolders(drive_svc, folder_id)
        for sf in sfs:
            if sf["name"].lower() == "attachments":
                attachment_links = list_files_in_folder(drive_svc, sf["id"])
                break
    except Exception:
        pass

    # 4. Claude analysis (fallback on failure)
    try:
        analysis = analyze_email(subject, sender, body_text, api_key, to=to_str, cc=cc_str)
        if not analysis.assignees:
            analysis.assignees = extract_assignees(subject, sender, body_text, api_key, to=to_str, cc=cc_str)
    except Exception:
        analysis = AnalysisResult(summary=_fallback_summary(body_text))

    # 5. Build ArchiveMessage
    message = ArchiveMessage(
        subject=full_subject,
        sender=sender,
        body_text=body_text,
        to=to_str,
        cc=cc_str,
        date_utc=f"{date_str}T00:00:00Z",
    )

    # 6. Compose markdown (empty string on failure)
    try:
        md_content = compose(
            message, attachment_links, processed_at,
            analysis.summary, "", analysis,
            preserved_fields=preserved_fields,
        )
    except Exception as exc:
        log.warning("archive -- compose failed",
                    extra={"run_id": run_id, "error": str(exc)})
        md_content = ""

    # 7. Write local (new files only)
    if write_local and md_content and out_dir and not local_already_existed:
        try:
            (out_dir / local_filename).write_text(md_content, encoding="utf-8")
            log.info("archive -- note written locally",
                     extra={"run_id": run_id, "file": local_filename})
        except Exception as exc:
            log.warning("archive -- local write failed",
                        extra={"run_id": run_id, "error": str(exc)})

    # 8. Write Drive (new files only)
    if write_drive and md_content and drive_output_folder_id and not local_already_existed:
        try:
            upsert_markdown(drive_svc, drive_output_folder_id, local_filename, md_content)
            log.info("archive -- note uploaded to Drive",
                     extra={"run_id": run_id, "file": local_filename})
        except Exception as exc:
            log.warning("archive -- Drive upload failed",
                        extra={"run_id": run_id, "file": local_filename, "error": str(exc)})

    return _FolderResult(
        message=message,
        analysis=analysis,
        md_content=md_content,
        local_filename=local_filename,
    )


def collect_archive_for_daily(
    drive_svc,
    archive_folder_id: str,
    date_start: str,
    date_end: str,
    api_key: str,
    local_dir: str,
    run_id: str,
    drive_output_folder_id: str = "",
) -> list[tuple]:
    """Collect archive folders within a date range for Daily Note generation.

    Args:
        drive_svc:              Authenticated Drive API service.
        archive_folder_id:      Drive folder ID containing archive subfolders.
        date_start:             Start date inclusive (YYYY-MM-DD).
        date_end:               End date inclusive (YYYY-MM-DD).
        api_key:                Anthropic API key.
        local_dir:              Local Obsidian vault folder for individual notes.
        run_id:                 Correlation ID.
        drive_output_folder_id: Drive folder ID to upload individual notes to.

    Returns list of (ArchiveMessage, AnalysisResult) tuples compatible with
    compose_daily().
    """
    results: list[tuple] = []

    try:
        subfolders = list_subfolders(drive_svc, archive_folder_id)
    except Exception as exc:
        log.error("collect-archive -- failed to list subfolders",
                  extra={"run_id": run_id, "error": str(exc)})
        return results

    out_dir = Path(local_dir) if local_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    processed_at = datetime.now(tz=timezone.utc).isoformat()

    for folder in subfolders:
        folder_name = folder["name"]
        folder_id = folder["id"]

        parsed = parse_folder_name(folder_name)
        if not parsed:
            continue

        date_str, sender, subject = parsed

        if date_str < date_start or date_str > date_end:
            continue

        try:
            fr = _process_single_folder(
                drive_svc, folder_id, folder_name,
                date_str, sender, subject,
                api_key, out_dir, processed_at, run_id,
                write_local=True,
                write_drive=bool(drive_output_folder_id),
                drive_output_folder_id=drive_output_folder_id,
            )
            results.append((fr.message, fr.analysis))
            log.info("collect-archive -- collected",
                     extra={"run_id": run_id, "folder_name": folder_name})
        except Exception as exc:
            log.warning("collect-archive -- folder failed",
                        extra={"run_id": run_id, "folder_name": folder_name, "error": str(exc)})
            continue

    return results


def scan_archive_folders(
    drive_svc,
    archive_folder_id: str,
    api_key: str,
    local_dir: str,
    run_id: str,
) -> ScanResult:
    """Scan all sub-folders in *archive_folder_id* and create Obsidian notes.

    Args:
        drive_svc:          Authenticated Drive API service.
        archive_folder_id:  Drive folder ID containing date_sender_subject folders.
        api_key:            Anthropic API key (empty = fallback summary).
        local_dir:          Local Obsidian vault folder to write notes into.
        run_id:             Correlation ID for structured logs.

    Returns ScanResult with counts.
    """
    result = ScanResult()

    try:
        subfolders = list_subfolders(drive_svc, archive_folder_id)
    except Exception as exc:
        log.error("scan-archive -- failed to list subfolders",
                  extra={"run_id": run_id, "error": str(exc)})
        result.errors += 1
        return result

    log.info("scan-archive -- found subfolders",
             extra={"run_id": run_id, "count": len(subfolders)})

    processed_at = datetime.now(tz=timezone.utc).isoformat()
    out_dir = Path(local_dir) if local_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    for folder in subfolders:
        folder_name = folder["name"]
        folder_id = folder["id"]

        parsed = parse_folder_name(folder_name)
        if not parsed:
            log.warning("scan-archive -- unrecognised folder name, skipping",
                        extra={"run_id": run_id, "folder_name": folder_name})
            result.skipped += 1
            continue

        date_str, sender, subject = parsed

        # Idempotency: check if local note already exists
        local_filename = filename_for_subject(f"{date_str} {subject}")
        if out_dir and (out_dir / local_filename).exists():
            log.info("scan-archive -- note already exists, skipping",
                     extra={"run_id": run_id, "folder_name": folder_name})
            result.skipped += 1
            continue

        try:
            _process_single_folder(
                drive_svc, folder_id, folder_name,
                date_str, sender, subject,
                api_key, out_dir, processed_at, run_id,
                write_local=True, write_drive=False,
            )
            result.processed += 1
            result.details.append(folder_name)
        except Exception as exc:
            log.error("scan-archive -- folder processing failed",
                      extra={"run_id": run_id, "folder_name": folder_name, "error": str(exc)})
            result.errors += 1

    return result
