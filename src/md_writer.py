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


# ── Media / Subsidiary 키워드 (이메일 본문에서 자동 태깅) ─────────── #
_SUBSIDIARY_KEYWORDS: list[str] = [
    "SEGR", "SECE", "SEAD", "SEPR", "SECH", "SESAR", "SELV", "SSA",
    "SEEG", "SEB", "SEJ", "SEAS", "SEHK", "SGE", "SIEL", "SME",
    "SEDA", "BANGLADESH", "SEI", "SEF", "SEUC", "SEG", "SEH", "SET",
    "SEMAG", "SETK", "SEWA", "SAMCOL", "SRI_LANKA", "SEM", "SENA",
    "SELA", "TSE", "SEA", "SEIB", "LA", "SENZ", "SEPAK", "SESP",
    "SEEA", "SEUK", "GLOBAL", "SECA", "SEBN", "SEASA", "SEPOL",
    "SEROM", "SECZ", "SEPCO", "SCIC", "SAVINA", "SEAU", "SEIN",
    "SEIL", "SEUZ", "SEC",
]

_MEDIA_KEYWORDS: list[str] = [
    "TRUEVIEW", "WE CHAT", "LINKEDIN", "SQ NEWS", "微信搜一搜",
    "LINEADS", "INDEPENDENT", "DIRECT", "CM360", "HANGZHOUMAISHOU",
    "DV360", "TTD", "TENCENT", "BING", "PAID MEDIA", "JINRICHENGZHANG",
    "NOSP", "URLTARGET", "DISCOVERY +", "小红书", "DUODUO VIDEO", "YDN",
    "LOCAL OFFLINE PUBLISHER", "AFFILIATE", "PAID SOCIAL", "X", "RED",
    "BLUETV", "QQ", "SHENGQIANKUAIBAO", "ZEST BUY", "LOCAL PUBLISHER",
    "IQIYI", "DISPLAY", "MANGO TV", "TENGXUN", "MEITU", "BYTEDANCE",
    "CRITEO", "TIKTOK", "JULIANG", "XHS", "REDDIT", "PINTEREST",
    "NAVER", "SINA", "META", "SHIHUO", "ZHIHU", "UC", "360",
    "SNAPCHAT", "BAIDU", "YAHOO", "GOOGLE ADS", "XANDR", "CTRIP",
    "PAID SEARCH", "TEADS", "BILIBILI", "WEIXIN", "KAKAO", "SA360",
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
# English email disclaimer (matches to end of text)
_DISCLAIMER_RE = re.compile(
    r'The information in this email and any\s*attachments.*$',
    re.IGNORECASE | re.DOTALL,
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


def _clean_body(text: str) -> str:
    """Remove non-essential clutter from email body for Obsidian readability.

    Strips CID inline images, tracking pixels, external-email warnings,
    English disclaimers, and signature blocks (after ━━━ separators).
    """
    if not text:
        return text

    # 1. Remove CID images (never render in Obsidian)
    text = _CID_IMAGE_RE.sub('', text)
    # 2. Remove tracking pixels (empty-alt images)
    text = _TRACKING_PIXEL_RE.sub('', text)
    # 3. Remove external-email warning banner
    text = _EXTERNAL_WARN_RE.sub('', text)
    # 4. Remove English disclaimer block (before separator check — reduces tail length)
    text = _DISCLAIMER_RE.sub('', text)

    # 5. Truncate at signature separator if tail is short enough
    seps = list(_SIG_SEPARATOR_RE.finditer(text))
    if seps:
        last = seps[-1]
        tail = text[last.end():].strip()
        if len(tail) <= _SIG_MAX_TAIL:
            text = text[:last.start()]

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

    # original_title: 순수 원본 메일 제목
    lines.append(f"original_title: {_yaml_scalar(message.subject)}")

    # description: Claude 생성 한 줄 요약 (100자 이내), 없으면 summary 첫 줄 fallback
    if analysis and analysis.description:
        lines.append(f"description: {_yaml_scalar(analysis.description)}")
    elif analysis and analysis.summary:
        first_line = analysis.summary.strip().splitlines()[0]
        desc = first_line.lstrip("- ").strip()
        lines.append(f"description: {_yaml_scalar(desc)}")
    else:
        lines.append("description:")

    # tag: media / subsidiary 키워드 자동 매칭
    full_text = (message.subject or "") + " " + (message.body_text or "")
    ms_tags = _extract_media_subsidiary_tags(full_text)
    if ms_tags:
        lines.append("tag:")
        for t in ms_tags:
            lines.append(f"  - {t}")
    else:
        lines.append("tag:")

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
        cleaned = message.body_text.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = _clean_body(cleaned)
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
