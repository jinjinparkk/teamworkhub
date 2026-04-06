"""Obsidian-compatible Markdown composer — email note format.

Template contract (frontmatter):
  ---
  email_title: <subject>
  date: <YYYY-MM-DD>
  sender: <sender>
  cc:
  attachment: true|false
  tags:
    - "#assignee"
    - "#category"
  result:
  link:
  ---

Body sections:
  - ### 요약   (AI-generated summary bullets)
  - ### 본문   (original email body)
  - ### 첨부파일 링크  (Drive links)
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.drive_client import DriveFile
    from src.gmail_client import ParsedMessage
    from src.summarizer import AnalysisResult


# Chars not allowed in Drive / Obsidian filenames.
_MSG_ID_UNSAFE = re.compile(r'[<>@\s/\\:*?"\'|#%&=+,;]')
# Chars not allowed in Obsidian filenames (for subject-based names).
_SUBJECT_UNSAFE = re.compile(r'[\\/:*?"<>|]')
# YAML special characters that require quoting in scalar values.
_YAML_SPECIAL = re.compile(r'[:#\[\]{}|>&*!,?\'"]')


# ── Helpers ─────────────────────────────────────────────────────────── #

def _yaml_scalar(value: str) -> str:
    """Quote a YAML scalar with double quotes if it contains special chars.

    Escape embedded double-quotes and backslashes.
    """
    if not value:
        return '""'
    if _YAML_SPECIAL.search(value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _sanitise_message_id(message_id: str) -> str:
    """Strip unsafe characters from a Gmail message-id for use in filenames."""
    safe = _MSG_ID_UNSAFE.sub("_", message_id).strip("_")
    return safe or "unknown"


def _sanitise_subject(subject: str) -> str:
    """Strip filesystem-unsafe characters from an email subject for Obsidian filenames."""
    safe = _SUBJECT_UNSAFE.sub("", subject).strip()
    return safe or "untitled"


# ── Public API ──────────────────────────────────────────────────────── #

def filename_for(message_id: str, account_email: str = "") -> str:
    """Return the deterministic Drive filename for a message's .md file.

    Pattern (single-account):  twh_{sanitised_message_id}.md
    Pattern (multi-account):   twh_{email_prefix}_{sanitised_message_id}.md

    The same (message_id, account_email) pair always maps to the same filename,
    enabling the .md file to act as the idempotency commit-marker.

    Examples
    ────────
    "msg_plain_123", ""                   →  "twh_msg_plain_123.md"
    "msg_plain_123", "alice@example.com"  →  "twh_alice_msg_plain_123.md"
    "<CAMsg001@mail.gmail.com>", ""       →  "twh_CAMsg001_mail.gmail.com_.md"
    ""                                    →  "twh_unknown.md"
    """
    safe_id = _sanitise_message_id(message_id)
    if account_email:
        prefix = re.sub(r"[^a-zA-Z0-9]", "_", account_email.split("@")[0]).strip("_")
        return f"twh_{prefix}_{safe_id}.md"
    return f"twh_{safe_id}.md"


def filename_for_subject(subject: str) -> str:
    """Return the local Obsidian filename for an email by its subject.

    Pattern: {sanitised_subject}.md

    Used for local Obsidian vault files (NOT Drive — Drive still uses
    filename_for() with message_id for idempotency).

    Examples
    ────────
    "CM360 중복제거 문제 확인"  →  "CM360 중복제거 문제 확인.md"
    "Re: Hello: World"         →  "Re Hello World.md"
    ""                          →  "untitled.md"
    """
    return f"{_sanitise_subject(subject)}.md"


def compose(
    message: "ParsedMessage",
    drive_files: list["DriveFile"],
    processed_at: str,
    summary: str = "",
    account_email: str = "",
    analysis: "AnalysisResult | None" = None,
) -> str:
    """Return the full Markdown string for one message.

    Args:
        message:       Parsed Gmail message.
        drive_files:   DriveFile objects for all uploaded attachments.
        processed_at:  ISO-8601 UTC string for when this sync cycle ran.
                       The date portion (YYYY-MM-DD) is used in frontmatter.
        summary:       Optional AI-generated summary string.
        account_email: Kept for API compatibility; not included in output.
        analysis:      Optional AnalysisResult for assignee/category tags.

    Returns a string ready to be written as a .md file.
    """
    # Extract date part from processed_at (YYYY-MM-DD)
    date_str = processed_at[:10] if len(processed_at) >= 10 else processed_at

    # ── YAML frontmatter ──────────────────────────────────────────────── #
    lines: list[str] = ["---"]
    lines.append(f"email_title: {_yaml_scalar(message.subject)}")
    lines.append(f"date: {date_str}")
    lines.append(f"sender: {_yaml_scalar(message.sender)}")
    lines.append("cc:")
    lines.append(f"attachment: {'true' if drive_files else 'false'}")

    # tags: assignees + category from analysis
    tag_list: list[str] = []
    if analysis:
        for name in analysis.assignees:
            tag_list.append(f'"#{name}"')
        if analysis.category:
            tag_list.append(f'"#{analysis.category}"')

    if tag_list:
        lines.append("tags:")
        for tag in tag_list:
            lines.append(f"  - {tag}")
    else:
        lines.append("tags:")

    lines.append("result:")
    lines.append("link:")
    lines.append("---")
    lines.append("")

    # ── 요약 section ──────────────────────────────────────────────────── #
    lines.append("### 요약")
    lines.append("")
    if summary:
        for line in summary.strip().splitlines():
            lines.append(line)
    else:
        lines.append("_(요약 없음)_")
    lines.append("")

    # ── 본문 section ──────────────────────────────────────────────────── #
    lines.append("### 본문")
    lines.append("")
    if message.body_text:
        lines.append(message.body_text.rstrip())
    else:
        lines.append("_(본문 없음)_")
    lines.append("")

    # ── 첨부파일 링크 section ─────────────────────────────────────────── #
    lines.append("### 첨부파일 링크")
    lines.append("")
    if drive_files:
        for df in drive_files:
            lines.append(f"- [{df.name}]({df.web_view_link})")
    else:
        lines.append("_(없음)_")
    lines.append("")

    return "\n".join(lines)
