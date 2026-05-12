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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.assignee import extract_assignees
from src.drive_client import (
    download_file_bytes,
    download_file_content,
    find_file_by_name,
    list_files_in_folder,
    list_subfolders,
    upsert_markdown,
    DriveFile,
)
from src.md_writer import compose, filename_for_subject, parse_preserved_fields, parse_todo_checks, parse_todo_items
from src.summarizer import AnalysisResult, analyze_email, _fallback_summary

log = logging.getLogger(__name__)

_FOLDER_NAME_ISO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})_(.+?)_(.+)$"
)
_FOLDER_NAME_SHORT_RE = re.compile(
    r"^(\d{6})_(.+?)_(.+)$"
)
# YYMMDDHH format (8 digits) — hour included
_FOLDER_NAME_HOUR_RE = re.compile(
    r"^(\d{8})_(.+?)_(.+)$"
)

_YAML_FRONT_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)

# ── CID inline image helpers ─────────────────────────────────────────── #

_CID_REF_RE = re.compile(r'!\[[^\]]*\]\(cid:([^)]+)\)')
_IMAGE_EXTS = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg'})


def _build_cid_map(
    drive_svc,
    body_text: str,
    attachments: list[DriveFile],
    out_dir: Path | None,
) -> tuple[dict[str, str], set[str]]:
    """Build a mapping from CID references in *body_text* to local image paths.

    Downloads matched inline images to ``{out_dir}/assets/`` and returns
    relative paths (``assets/filename.png``) usable in Obsidian markdown.

    Returns ``(cid_map, used_file_ids)`` where *cid_map* maps full CID
    strings to local relative paths, and *used_file_ids* contains the
    ``file_id`` values of matched attachments (for removal from the
    attachment link list to avoid duplicates).
    """
    cid_map: dict[str, str] = {}
    used_ids: set[str] = set()

    cids = _CID_REF_RE.findall(body_text)
    if not cids:
        return cid_map, used_ids

    image_attachments = [
        a for a in attachments
        if any(a.name.lower().endswith(ext) for ext in _IMAGE_EXTS)
    ]
    if not image_attachments:
        return cid_map, used_ids

    assets_dir = out_dir / "assets" if out_dir else None

    for cid in cids:
        # CID format: "image001.png@01DCDE42.BB892F50" → name part "image001.png"
        cid_name = cid.split("@")[0] if "@" in cid else cid
        cid_lower = cid_name.lower()

        for att in image_attachments:
            att_lower = att.name.lower()
            # Match: attachment name contains CID filename part
            # e.g. "inline_image001.png" contains "image001.png"
            if cid_lower in att_lower:
                if assets_dir:
                    try:
                        assets_dir.mkdir(parents=True, exist_ok=True)
                        local_path = assets_dir / att.name
                        if not local_path.exists():
                            img_bytes = download_file_bytes(drive_svc, att.file_id)
                            local_path.write_bytes(img_bytes)
                        cid_map[cid] = f"assets/{att.name}"
                    except Exception:
                        log.warning("cid-map -- failed to download %s", att.name)
                        break
                else:
                    # Fallback: Drive URL (won't render in Obsidian but keeps info)
                    cid_map[cid] = f"https://drive.google.com/uc?export=view&id={att.file_id}"
                used_ids.add(att.file_id)
                break

    return cid_map, used_ids


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


def _yymmddhh_to_iso(short: str) -> str:
    """Convert ``YYMMDDHH`` → ``YYYY-MM-DD`` (actual date, no shift).

    e.g. '26050716' → '2026-05-07'
         '26050718' → '2026-05-07'
    """
    dt = datetime.strptime(short, "%y%m%d%H")
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

    Supports three formats:
      ``YYYY-MM-DD_발신자_제목``  → ("2026-04-20", "김치성", "결재요청")
      ``YYMMDDHH_발신자_제목``    → ("2026-04-20", "김치성", "결재요청")  [18시 이상 → 다음날]
      ``YYMMDD_발신자_제목``      → ("2026-04-20", "김치성", "결재요청")
    """
    m = _FOLDER_NAME_ISO_RE.match(name)
    if m:
        return m.group(1), m.group(2), m.group(3)
    # Check 8-digit (YYMMDDHH) before 6-digit (YYMMDD)
    m = _FOLDER_NAME_HOUR_RE.match(name)
    if m:
        try:
            date_iso = _yymmddhh_to_iso(m.group(1))
        except ValueError:
            return None
        return date_iso, m.group(2), m.group(3)
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

    # 1. Preserved fields + checked todos from existing local note
    preserved_fields: dict[str, str] | None = None
    checked_todos: set[str] = set()
    local_already_existed = bool(out_dir and (out_dir / local_filename).exists())
    if local_already_existed:
        try:
            existing_text = (out_dir / local_filename).read_text(encoding="utf-8")
            preserved_fields = parse_preserved_fields(existing_text)
            checked_todos = parse_todo_checks(existing_text)
        except Exception:
            pass

    # 1b. Drive fallback (Cloud Run — no local filesystem)
    if preserved_fields is None and drive_output_folder_id:
        try:
            existing_file = find_file_by_name(drive_svc, local_filename, drive_output_folder_id)
            if existing_file:
                existing_text = download_file_content(drive_svc, existing_file.file_id)
                preserved_fields = parse_preserved_fields(existing_text)
                checked_todos = parse_todo_checks(existing_text)
        except Exception:
            pass

    # 2. Download 본문.md — raises on failure
    #    Some archive folders use numbered variants: [1] 본문.md, [2] 본문.md
    #    Prefer exact "본문.md", then fall back to the highest-numbered variant.
    files = list_files_in_folder(drive_svc, folder_id)
    body_file = next((f for f in files if f.name == "본문.md"), None)
    if not body_file:
        body_variants = sorted(
            [f for f in files if f.name.endswith("본문.md")],
            key=lambda f: f.name, reverse=True,
        )
        body_file = body_variants[0] if body_variants else None
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

    # 3b. Build CID map for inline images, remove matched from attachment list
    cid_map: dict[str, str] = {}
    if attachment_links:
        cid_map, inline_ids = _build_cid_map(drive_svc, body_text, attachment_links, out_dir)
        if inline_ids:
            attachment_links = [a for a in attachment_links if a.file_id not in inline_ids]

    # 4. Claude analysis (fallback on failure)
    try:
        analysis = analyze_email(subject, sender, body_text, api_key, to=to_str, cc=cc_str)
        if not analysis.assignees:
            analysis.assignees = extract_assignees(subject, sender, body_text, api_key, to=to_str, cc=cc_str)
    except Exception:
        analysis = AnalysisResult(summary=_fallback_summary(body_text))

    # 4b. Preserve existing note's To-do List if it exists.
    # Claude returns varying task text across runs, which breaks check-state
    # matching.  Once a note has To-do items, keep them stable.
    if local_already_existed and out_dir:
        try:
            existing_items = parse_todo_items(
                (out_dir / local_filename).read_text(encoding="utf-8")
            )
            if existing_items:
                analysis.action_items = existing_items
        except Exception:
            pass
    elif not local_already_existed and drive_output_folder_id:
        # Drive fallback for To-do items (Cloud Run)
        try:
            existing_file = find_file_by_name(drive_svc, local_filename, drive_output_folder_id)
            if existing_file:
                existing_items = parse_todo_items(
                    download_file_content(drive_svc, existing_file.file_id)
                )
                if existing_items:
                    analysis.action_items = existing_items
        except Exception:
            pass

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
            cid_map=cid_map if cid_map else None,
            checked_todos=checked_todos,
        )
    except Exception as exc:
        log.warning("archive -- compose failed",
                    extra={"run_id": run_id, "error": str(exc)})
        md_content = ""

    # 7. Write local (always update to keep To-do List in sync with daily note)
    if write_local and md_content and out_dir:
        try:
            (out_dir / local_filename).write_text(md_content, encoding="utf-8")
            log.info("archive -- note written locally",
                     extra={"run_id": run_id, "file": local_filename})
        except Exception as exc:
            log.warning("archive -- local write failed",
                        extra={"run_id": run_id, "error": str(exc)})

    # 8. Write Drive (always update to keep in sync)
    if write_drive and md_content and drive_output_folder_id:
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
    start_hour: int | None = None,
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
        start_hour:             When set, folders on *date_start* are only
                                included if they use YYMMDDHH format with
                                hour >= start_hour.  YYMMDD / ISO folders
                                on *date_start* are skipped (no hour info).

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

        # Hour-based filtering for the start date (e.g. only 18:00+)
        if (start_hour is not None
                and date_str == date_start
                and date_start != date_end):
            m = _FOLDER_NAME_HOUR_RE.match(folder_name)
            if m:
                try:
                    dt = datetime.strptime(m.group(1), "%y%m%d%H")
                    if dt.hour < start_hour:
                        continue
                except ValueError:
                    continue
            else:
                # YYMMDD / ISO format — no hour info; skip for start date
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
