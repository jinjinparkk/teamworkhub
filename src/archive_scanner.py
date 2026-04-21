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
from types import SimpleNamespace

from src.assignee import extract_assignees
from src.drive_client import (
    download_file_content,
    list_files_in_folder,
    list_subfolders,
    DriveFile,
)
from src.md_writer import compose, filename_for_subject
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


def collect_archive_for_daily(
    drive_svc,
    archive_folder_id: str,
    date_start: str,
    date_end: str,
    api_key: str,
    local_dir: str,
    run_id: str,
) -> list[tuple]:
    """Collect archive folders within a date range for Daily Note generation.

    Args:
        drive_svc:          Authenticated Drive API service.
        archive_folder_id:  Drive folder ID containing archive subfolders.
        date_start:         Start date inclusive (YYYY-MM-DD).
        date_end:           End date inclusive (YYYY-MM-DD).
        api_key:            Anthropic API key.
        local_dir:          Local Obsidian vault folder for individual notes.
        run_id:             Correlation ID.

    Returns list of (SimpleNamespace, AnalysisResult) tuples compatible with
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

        # Filter by date range
        if date_str < date_start or date_str > date_end:
            continue

        # Download 본문.md
        try:
            files = list_files_in_folder(drive_svc, folder_id)
            body_file = next((f for f in files if f.name == "본문.md"), None)
            if not body_file:
                log.warning("collect-archive -- 본문.md not found",
                            extra={"run_id": run_id, "folder_name": folder_name})
                continue
            body_text = download_file_content(drive_svc, body_file.file_id)
            body_text = _strip_forward_header(_strip_yaml_frontmatter(body_text))
        except Exception as exc:
            log.error("collect-archive -- download failed",
                      extra={"run_id": run_id, "folder_name": folder_name, "error": str(exc)})
            continue

        # Collect attachment links
        attachment_links: list[DriveFile] = []
        try:
            sfs = list_subfolders(drive_svc, folder_id)
            for sf in sfs:
                if sf["name"].lower() == "attachments":
                    attachment_links = list_files_in_folder(drive_svc, sf["id"])
                    break
        except Exception:
            pass

        # Analyze
        try:
            analysis = analyze_email(subject, sender, body_text, api_key)
            if not analysis.assignees:
                analysis.assignees = extract_assignees(subject, sender, body_text, "")
        except Exception:
            analysis = AnalysisResult(summary=_fallback_summary(body_text))

        # Build pseudo message
        full_subject = f"{date_str} {subject}"
        pseudo_msg = SimpleNamespace(
            subject=full_subject,
            sender=sender,
            body_text=body_text,
            to="",
            cc="",
            attachments=[],
            _attachment_links=attachment_links,
        )

        # Write individual note
        if out_dir:
            local_filename = filename_for_subject(full_subject)
            local_path = out_dir / local_filename
            if not local_path.exists():
                try:
                    md_content = compose(pseudo_msg, attachment_links, processed_at, analysis.summary, "", analysis)
                    local_path.write_text(md_content, encoding="utf-8")
                    log.info("collect-archive -- note written",
                             extra={"run_id": run_id, "file": local_filename})
                except Exception as exc:
                    log.warning("collect-archive -- note write failed",
                                extra={"run_id": run_id, "error": str(exc)})

        results.append((pseudo_msg, analysis))
        log.info("collect-archive -- collected",
                 extra={"run_id": run_id, "folder_name": folder_name})

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
            _process_archive_folder(
                drive_svc, folder_id, folder_name,
                date_str, sender, subject,
                api_key, out_dir, local_filename,
                processed_at, run_id,
            )
            result.processed += 1
            result.details.append(folder_name)
        except Exception as exc:
            log.error("scan-archive -- folder processing failed",
                      extra={"run_id": run_id, "folder_name": folder_name, "error": str(exc)})
            result.errors += 1

    return result


def _process_archive_folder(
    drive_svc,
    folder_id: str,
    folder_name: str,
    date_str: str,
    sender: str,
    subject: str,
    api_key: str,
    out_dir: Path | None,
    local_filename: str,
    processed_at: str,
    run_id: str,
) -> None:
    """Process a single archive folder: download body, analyze, write note."""
    # 1. Find 본문.md in the folder
    files = list_files_in_folder(drive_svc, folder_id)
    body_file = None
    for f in files:
        if f.name == "본문.md":
            body_file = f
            break

    if not body_file:
        log.warning("scan-archive -- 본문.md not found",
                     extra={"run_id": run_id, "folder_name": folder_name})
        raise FileNotFoundError(f"본문.md not found in {folder_name}")

    # 2. Download body text and strip YAML frontmatter if present
    body_text = download_file_content(drive_svc, body_file.file_id)
    body_text = _strip_forward_header(_strip_yaml_frontmatter(body_text))
    log.info("scan-archive -- body downloaded",
             extra={"run_id": run_id, "folder_name": folder_name, "chars": len(body_text)})

    # 3. Collect attachment links from attachments/ subfolder
    attachment_links: list[DriveFile] = []
    try:
        subfolders = list_subfolders(drive_svc, folder_id)
        for sf in subfolders:
            if sf["name"].lower() == "attachments":
                attachment_links = list_files_in_folder(drive_svc, sf["id"])
                break
    except Exception as exc:
        log.warning("scan-archive -- attachment scan failed",
                    extra={"run_id": run_id, "folder_name": folder_name, "error": str(exc)})

    # 4. Analyze with Claude
    try:
        analysis = analyze_email(subject, sender, body_text, api_key)
        if not analysis.assignees:
            analysis.assignees = extract_assignees(subject, sender, body_text, "")
    except Exception as exc:
        log.warning("scan-archive -- analysis failed, using fallback",
                    extra={"run_id": run_id, "folder_name": folder_name, "error": str(exc)})
        analysis = AnalysisResult(summary=_fallback_summary(body_text))

    # 5. Build pseudo ParsedMessage for compose()
    pseudo_msg = SimpleNamespace(
        subject=f"{date_str} {subject}",
        sender=sender,
        body_text=body_text,
        to="",
        cc="",
        attachments=[],
    )

    md_content = compose(pseudo_msg, attachment_links, processed_at, analysis.summary, "", analysis)

    # 6. Write to local Obsidian vault
    if out_dir:
        (out_dir / local_filename).write_text(md_content, encoding="utf-8")
        log.info("scan-archive -- note written",
                 extra={"run_id": run_id, "file": local_filename})
