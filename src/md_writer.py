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

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.drive_client import DriveFile
    from src.gmail_client import ParsedMessage
    from src.summarizer import AnalysisResult


# ── Media / Subsidiary 키워드 (이메일 본문에서 자동 태깅) ─────────── #

def _load_subsidiary_keywords() -> list[str]:
    """Load subsidiary codes from 'Reference/Subsidiary real.md' in the vault."""
    vault = os.environ.get(
        "OBSIDIAN_VAULT_PATH",
        os.environ.get("LOCAL_OUTPUT_DIR", "").rsplit("/TeamWorkHub", 1)[0]
        if os.environ.get("LOCAL_OUTPUT_DIR", "") else "",
    )
    ref = Path(vault) / "Reference" / "Subsidiary real.md" if vault else None
    if ref and ref.is_file():
        codes: set[str] = set()
        for line in ref.read_text(encoding="utf-8").splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4 and parts[1] and parts[1] not in ("subsidiary", "---", "------------"):
                codes.add(parts[1])
        if codes:
            return sorted(codes)
    # Fallback: hardcoded list (in case the file is unavailable)
    return [
        "BANGLADESH", "GLOBAL", "SAMCOL", "SAVINA", "SCIC", "SEA", "SEAD",
        "SEAS", "SEASA", "SEAU", "SEB", "SEBN", "SEC", "SECA", "SECE",
        "SECH", "SECZ", "SEDA", "SEEA", "SEEG", "SEF", "SEG", "SEGR",
        "SEH", "SEHK", "SEI", "SEIB", "SEIL", "SEIN", "SEJ", "SELA",
        "SELV", "SEM", "SEMAG", "SENA", "SENZ", "SEPAK", "SEPCO", "SEPOL",
        "SEPR", "SEROM", "SESAR", "SESP", "SET", "SETK", "SEUC", "SEUK",
        "SEUZ", "SEWA", "SGE", "SIEL", "SME", "SRI_LANKA", "SSA", "TSE",
    ]

_SUBSIDIARY_KEYWORDS: list[str] = _load_subsidiary_keywords()

_MEDIA_KEYWORDS: list[str] = [
    "TRUEVIEW", "WE_CHAT", "LINKEDIN", "SQ_NEWS", "微信搜一搜",
    "LINEADS", "INDEPENDENT", "DIRECT", "CM360", "HANGZHOUMAISHOU",
    "DV360", "TTD", "TENCENT", "BING", "PAID_MEDIA", "JINRICHENGZHANG",
    "NOSP", "URLTARGET", "DISCOVERY+", "小红书", "DUODUO_VIDEO", "YDN",
    "LOCAL_OFFLINE_PUBLISHER", "AFFILIATE", "PAID_SOCIAL", "X", "RED",
    "BLUETV", "QQ", "SHENGQIANKUAIBAO", "ZEST_BUY", "LOCAL_PUBLISHER",
    "IQIYI", "DISPLAY", "MANGO_TV", "TENGXUN", "MEITU", "BYTEDANCE",
    "CRITEO", "TIKTOK", "JULIANG", "XHS", "REDDIT", "PINTEREST",
    "NAVER", "SINA", "META", "SHIHUO", "ZHIHU", "UC", "360",
    "SNAPCHAT", "BAIDU", "YAHOO", "GOOGLE_ADS", "XANDR", "CTRIP",
    "PAID_SEARCH", "TEADS", "BILIBILI", "WEIXIN", "KAKAO", "SA360",
]

# 짧은 키워드 (3글자 이하)는 단어 경계 매칭, 긴 키워드는 단순 포함 매칭
_SHORT_KEYWORD_LEN = 3


def _extract_media_subsidiary_tags(text: str) -> list[str]:
    """Extract matching media/subsidiary keywords from text (case-insensitive)."""
    upper = text.upper()
    found: list[str] = []
    for kw in _MEDIA_KEYWORDS:
        if len(kw) <= _SHORT_KEYWORD_LEN:
            if re.search(r"(?<![A-Z0-9])" + re.escape(kw) + r"(?![A-Z0-9])", upper):
                found.append(kw)
        elif kw.upper() in upper:
            found.append(kw)
    for kw in _SUBSIDIARY_KEYWORDS:
        if len(kw) <= _SHORT_KEYWORD_LEN:
            if re.search(r"(?<![A-Z0-9])" + re.escape(kw) + r"(?![A-Z0-9])", upper):
                found.append(kw)
        elif kw.upper() in upper:
            found.append(kw)
    return found


# Chars not allowed in Drive / Obsidian filenames.
_MSG_ID_UNSAFE = re.compile(r'[<>@\s/\\:*?"\'|#%&=+,;]')
# Chars not allowed in Obsidian filenames (for subject-based names).
_SUBJECT_UNSAFE = re.compile(r'[\\/:*?"<>|]')
# YAML special characters that require quoting in scalar values.
_YAML_SPECIAL = re.compile(r'[:#\[\]{}|>&*!,?\'"]')
# 3+ consecutive newlines → 1 blank line (keeps paragraph separation)
_MULTI_NEWLINE = re.compile(r"\n{3,}")

# ── Body cleanup patterns ─────────────────────────────────────────── #
# CID inline images: ![alt](cid:...) — never render outside email clients
_CID_IMAGE_RE = re.compile(r'!\[[^\]]*\]\(cid:[^)]+\)\s*')
# Tracking pixels: ![](http://...) — empty-alt images used for read receipts
_TRACKING_PIXEL_RE = re.compile(r'!\[\s*\]\(https?://[^)]+\)\s*')
# Korean external-email warning banner
_EXTERNAL_WARN_RE = re.compile(
    r'^이 메일은 조직 외부에서 발송되었습니다[^\n]*\n?',
    re.MULTILINE,
)
# English email disclaimer (matches paragraph only, not rest of text)
_DISCLAIMER_RE = re.compile(
    r'The information in this email and any\s*attachments[^\n]*(?:\n(?!\s*\n)[^\n]*)*\n?',
    re.IGNORECASE,
)
# Signature separator: ━ (U+2501) or ─ (U+2500) repeated 5+ times
_SIG_SEPARATOR_RE = re.compile(r'^[━─]{5,}\s*$', re.MULTILINE)
# Max chars after last separator to consider it a signature block
_SIG_MAX_TAIL = 800


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


def _replace_cid_images(text: str, cid_map: dict[str, str]) -> str:
    """Replace ``![alt](cid:xxx)`` refs with Drive links from *cid_map*.

    - If the CID is found in *cid_map*, replace with the Drive web view link.
    - If not found, remove the image reference entirely.
    """
    def _replacer(match: re.Match) -> str:
        alt = match.group(1)
        cid = match.group(2)
        link = cid_map.get(cid)
        if link:
            return f"![{alt}]({link})"
        return ""

    return _CID_IMAGE_RE.sub(_replacer, text)


def _clean_body(text: str, cid_map: dict[str, str] | None = None) -> str:
    """Remove non-essential clutter from email body for Obsidian readability.

    Strips CID inline images (or replaces with Drive links if *cid_map*
    provided), tracking pixels, external-email warnings, and English disclaimers.
    """
    if not text:
        return text

    # 1. CID images: replace with Drive links or remove
    if cid_map:
        text = _replace_cid_images(text, cid_map)
    else:
        text = _CID_IMAGE_RE.sub('', text)
    # 2. Remove tracking pixels (empty-alt images)
    text = _TRACKING_PIXEL_RE.sub('', text)
    # 3. Remove external-email warning banner
    text = _EXTERNAL_WARN_RE.sub('', text)
    # 4. Remove English disclaimer block (before separator check — reduces tail length)
    text = _DISCLAIMER_RE.sub('', text)

    # 5. Signature separator truncation removed — preserves full email
    #    thread content.  Thread messages are separated by ━━━/───lines
    #    which are indistinguishable from signature separators.

    return text


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


_RESULT_RE = re.compile(r'^result:[ \t]+(.+)$', re.MULTILINE)
_LINK_RE = re.compile(r'^link:[ \t]+(.+)$', re.MULTILINE)

# Regex to capture checked todo items: "- [x] task text #assignee #발신자:name"
_TODO_CHECK_RE = re.compile(r'^-\s*\[[xX]\]\s+(.+)$', re.MULTILINE)


def _extract_sender_name(sender: str) -> str:
    """Extract the display name from a sender string, stripping email address.

    Examples:
        "김치성 <chisung@example.com>" → "김치성"
        "chisung@example.com"          → "chisung"
        "GMPD 데이터 <gmpd@...>"      → "GMPD"
    """
    if not sender:
        return ""
    # "Name <email>" pattern
    m = re.match(r'^"?([^"<]+)"?\s*<', sender)
    if m:
        name = m.group(1).strip()
    elif "@" in sender and " " not in sender.strip():
        # Plain email — return local part
        name = sender.split("@")[0]
    else:
        name = sender.strip()
    # Normalize GMPD variants (GMPD 데이터, GMPD DATA, GMPD_DATA, etc.) → "GMPD"
    if name.upper().replace(" ", "").replace("_", "").startswith("GMPD"):
        return "GMPD"
    return name


def parse_todo_checks(content: str) -> set[str]:
    """Extract checked todo task texts from an existing note's To-do List section.

    Returns a set of task description strings (the part after ``[x]``,
    before any ``#tag``).  Used to preserve user check states when
    regenerating a note.
    """
    checked: set[str] = set()
    in_todo = False
    for line in content.splitlines():
        if line.strip() == "### To-do List":
            in_todo = True
            continue
        if in_todo and line.startswith("### "):
            break
        if in_todo:
            m = _TODO_CHECK_RE.match(line)
            if m:
                # Extract task text before first # tag
                raw = m.group(1)
                task_part = re.split(r'\s+#', raw)[0].strip()
                # Un-escape markdown underscores so task text matches Claude output
                task_part = task_part.replace(r"\_", "_")
                if task_part:
                    checked.add(task_part)
    return checked


_TODO_ITEM_RE = re.compile(r'^-\s*\[[ xX]\]\s+(.+)$')


def parse_todo_items(content: str) -> list[dict]:
    """Extract all To-do List items from an existing note as action_item dicts.

    Returns list of ``{"task": "...", "assignee": "..."}`` matching the
    ``AnalysisResult.action_items`` format.  Used to sync daily note tags
    with the individual note's To-do List.

    Tag order in compose(): ``#assignee #sender_name`` — first tag is
    always the assignee.
    """
    items: list[dict] = []
    in_todo = False
    for line in content.splitlines():
        if line.strip() == "### To-do List":
            in_todo = True
            continue
        if in_todo and line.startswith("### "):
            break
        if in_todo:
            m = _TODO_ITEM_RE.match(line)
            if m:
                raw = m.group(1)
                parts = re.split(r'\s+#', raw)
                task = parts[0].strip().replace(r"\_", "_")
                # First #tag is assignee (compose always writes assignee first)
                assignee = parts[1].strip() if len(parts) > 1 else ""
                if task:
                    items.append({"task": task, "assignee": assignee})
    return items


def parse_preserved_fields(content: str) -> dict[str, str]:
    """Extract user-editable frontmatter fields from an existing note.

    Returns a dict with ``result`` and ``link`` keys.  Values are empty
    strings when the field is blank or absent.
    """
    fields: dict[str, str] = {"result": "", "link": ""}
    # Only look inside the YAML frontmatter block.
    if not content.startswith("---"):
        return fields
    end = content.find("\n---", 3)
    if end == -1:
        return fields
    fm = content[:end]
    m_result = _RESULT_RE.search(fm)
    if m_result:
        fields["result"] = m_result.group(1).strip()
    m_link = _LINK_RE.search(fm)
    if m_link:
        fields["link"] = m_link.group(1).strip()
    return fields


def compose(
    message: "ParsedMessage",
    drive_files: list["DriveFile"],
    processed_at: str,
    summary: str = "",
    account_email: str = "",
    analysis: "AnalysisResult | None" = None,
    preserved_fields: dict[str, str] | None = None,
    cid_map: dict[str, str] | None = None,
    checked_todos: set[str] | None = None,
) -> str:
    """Return the full Markdown string for one message.

    Args:
        message:          Parsed Gmail message.
        drive_files:      DriveFile objects for all uploaded attachments.
        processed_at:     ISO-8601 UTC string for when this sync cycle ran.
                          The date portion (YYYY-MM-DD) is used in frontmatter.
        summary:          Optional AI-generated summary string.
        account_email:    Kept for API compatibility; not included in output.
        analysis:         Optional AnalysisResult for assignee/category tags.
        preserved_fields: Dict with ``result``/``link`` values extracted from
                          a previous version of this note via
                          :func:`parse_preserved_fields`.  When provided, the
                          values are carried over into the new frontmatter.
        checked_todos:    Set of task description strings that were previously
                          checked off by the user.  When provided, matching
                          items use ``[x]`` instead of ``[ ]``.

    Returns a string ready to be written as a .md file.
    """
    # Extract date: prefer email subject date prefix (YYYY-MM-DD ...),
    # fall back to processed_at.
    date_str = processed_at[:10] if len(processed_at) >= 10 else processed_at
    _date_prefix = re.match(r"^(\d{4}-\d{2}-\d{2})\s", message.subject or "")
    if _date_prefix:
        date_str = _date_prefix.group(1)

    # ── YAML frontmatter ──────────────────────────────────────────────── #
    lines: list[str] = ["---"]
    lines.append(f"email_title: {_yaml_scalar(message.subject)}")
    lines.append(f"date: {date_str}")
    lines.append(f"sender: {_yaml_scalar(message.sender)}")
    lines.append("cc:")
    lines.append(f"attachment: {'true' if drive_files else 'false'}")

    # tags: AI-selected subsidiary/media keywords (from Claude analysis)
    # Falls back to keyword matching when AI tags are not available.
    if analysis and (analysis.media_tags or analysis.subsidiary_tags):
        ai_tags = analysis.subsidiary_tags + analysis.media_tags
    else:
        full_text = (message.subject or "") + " " + (message.body_text or "")
        ai_tags = _extract_media_subsidiary_tags(full_text)

    if ai_tags:
        lines.append("tags:")
        for t in ai_tags:
            lines.append(f"  - {t}")
    else:
        lines.append("tags:")

    # description: Claude 생성 한 줄 요약 (100자 이내), 없으면 summary 첫 줄 fallback
    if analysis and analysis.description:
        lines.append(f"description: {_yaml_scalar(analysis.description)}")
    elif analysis and analysis.summary:
        first_line = analysis.summary.strip().splitlines()[0]
        desc = first_line.lstrip("- ").strip()
        lines.append(f"description: {_yaml_scalar(desc)}")
    else:
        lines.append("description:")

    _pf = preserved_fields or {}
    _result_val = _pf.get("result", "")
    _link_val = _pf.get("link", "")
    lines.append(f"result: {_result_val}" if _result_val else "result:")
    lines.append(f"link: {_link_val}" if _link_val else "link:")
    lines.append("---")
    lines.append("")

    # ── To-do List section ────────────────────────────────────────────── #
    action_items = analysis.action_items if analysis else []
    if action_items:
        _checked_todos = checked_todos or set()
        sender_name = _extract_sender_name(message.sender).replace(' ', '_')
        lines.append("### To-do List")
        lines.append("")
        for item in action_items:
            task = item.get("task", "")
            assignee = item.get("assignee", "")
            if not task:
                continue
            check = "x" if task in _checked_todos else " "
            # Escape underscores in task text to prevent Obsidian italic rendering
            escaped_task = task.replace("_", r"\_")
            tag_parts = []
            if assignee:
                tag_parts.append(f"#{assignee.replace(' ', '_')}")
            if sender_name:
                tag_parts.append(f"#{sender_name}")
            tag_str = " ".join(tag_parts)
            lines.append(f"- [{check}] {escaped_task} {tag_str}".rstrip())
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
        cleaned = message.body_text.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = _clean_body(cleaned, cid_map=cid_map)
        cleaned = _MULTI_NEWLINE.sub("\n\n", cleaned).rstrip()
        lines.append(cleaned)
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
