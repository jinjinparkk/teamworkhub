"""Daily digest Markdown composer.

Generates a single Obsidian-compatible Daily Note that aggregates all
overnight emails (18:00 ~ 08:59) into one readable page.

Output filename pattern: YYYY-MM-DD.md  (Obsidian Daily Notes format)

Usage
─────
  from src.daily_writer import compose_daily, filename_for_date

  md = compose_daily(messages_with_analysis, "2025-04-02",
                     "2025-04-01 18:00", "2025-04-02 08:59", "Asia/Seoul")
  name = filename_for_date("2025-04-02")   # "2025-04-02.md"
"""
from __future__ import annotations

import re
from datetime import date as _date, timedelta
from typing import TYPE_CHECKING

from src.md_writer import filename_for_subject

if TYPE_CHECKING:
    from src.gmail_client import ParsedMessage
    from src.summarizer import AnalysisResult

# Prefixes to strip when normalising email subjects for thread detection
_THREAD_PREFIX_RE = re.compile(
    r"^(?:re|fw|fwd|회신|전달|답장|답변)\s*:\s*", flags=re.IGNORECASE
)

# 요일별 정기 업무 (0=월 ~ 4=금, 5=토/6=일은 항목 없음)
_RECURRING_TASKS: dict[int, str] = {
    0: "RPA",
    1: "로직점검",
    2: "수정기",
    3: "목정기",
    4: "금정기",
}


def filename_for_date(date_str: str) -> str:
    """Return the Daily Note filename for *date_str* (``YYYY-MM-DD``).

    Example: "2025-04-02" → "2025-04-02.md"
    """
    return f"{date_str}.md"


def _normalise_subject(subject: str) -> str:
    """Strip RE:/FW: prefixes repeatedly until none remain."""
    s = subject.strip()
    while True:
        new = _THREAD_PREFIX_RE.sub("", s).strip()
        if new == s:
            return s.lower()
        s = new


def compose_daily(
    messages: list[tuple["ParsedMessage", "AnalysisResult"]],
    date_str: str,
    period_start: str,
    period_end: str,
    timezone_name: str = "Asia/Seoul",
    daily_folder: str = "TeamWorkHub_Daily",
    note_folder: str = "",
) -> str:
    """Return a Daily Note Markdown string aggregating overnight emails.

    Args:
        messages:      List of (ParsedMessage, AnalysisResult) pairs.
        date_str:      ISO date of the digest day, e.g. "2025-04-02".
        period_start:  Human-readable start of the collection window.
        period_end:    Human-readable end of the collection window.
        timezone_name: Timezone label shown in the note header.
        daily_folder:  Obsidian folder name for daily notes (used in Dataview
                       queries). Defaults to "TeamWorkHub_Daily".
        note_folder:   Obsidian folder name for individual email notes. When set,
                       wiki-links include the folder path so Obsidian resolves
                       cross-folder links correctly (e.g. "TeamWorkHub/제목").

    Returns a UTF-8 string ready to be written as a .md file.
    """
    tz_short = timezone_name.split("/")[-1] if "/" in timezone_name else timezone_name
    count = len(messages)
    note_date = _date.fromisoformat(date_str)

    lines: list[str] = []

    # ── YAML frontmatter ──────────────────────────────────────────────── #
    all_assignees = sorted({n for _, ar in messages for n in ar.assignees})
    has_urgent = any(ar.priority == "긴급" for _, ar in messages)

    lines.append("---")
    lines.append("Type: daily_note")
    lines.append(f"date: {date_str}")
    lines.append(f'period: "{period_start} ~ {period_end} ({tz_short})"')
    lines.append(f"email_count: {count}")
    if all_assignees:
        lines.append(f"assignees: {all_assignees}")
    else:
        lines.append("assignees: []")
    lines.append(f"has_urgent: {str(has_urgent).lower()}")
    lines.append("---")
    lines.append("")

    # ── Today's work ────────────────────────────────────────────────── #
    lines.append("### Today's work")
    lines.append("#### To do list")

    if messages:
        for msg, ar in messages:
            subject = msg.subject or "(제목 없음)"
            wiki_name = filename_for_subject(subject).removesuffix(".md")
            if note_folder:
                wiki_link = f"{note_folder}/{wiki_name}|{wiki_name}"
            else:
                wiki_link = wiki_name
            tags = " ".join(f"#{a}" for a in ar.assignees) if ar.assignees else "#미지정"
            lines.append(f"- [ ] [[{wiki_link}]] {tags}")
    else:
        lines.append("- [ ]")

    lines.append("")

    # Static sections
    lines.append("#### 정기적인 일")
    recurring = _RECURRING_TASKS.get(note_date.weekday())
    if recurring:
        lines.append(f"- [ ] {recurring}")
    else:
        lines.append("- [ ]")
    lines.append("")

    lines.append("#### Schedule (구글 캘린더 연동)")
    lines.append("-")
    lines.append("")

    # ── 미완료 (Dataview) ───────────────────────────────────────────── #
    lines.append("### 미완료")
    lines.append("")
    lines.append("```dataview")
    lines.append(f'TASK FROM "{daily_folder}"')
    lines.append('WHERE !completed AND date(file.name) >= date(today) - dur(14d) AND text != ""')
    lines.append("```")
    lines.append("")

    # ── History (3 Dataview blocks) ──────────────────────────────────── #
    # 월요일이면 "어제" = 금요일 (주말엔 daily note 없음)
    if note_date.weekday() == 0:
        prev_work_day = note_date - timedelta(days=3)
        prev_label = f"지난 금요일 ({prev_work_day.isoformat()})"
    else:
        prev_work_day = note_date - timedelta(days=1)
        prev_label = f"Yesterday ({prev_work_day.isoformat()})"
    week_ago = note_date - timedelta(days=7)

    lines.append("### History")
    lines.append("")
    lines.append(f"**Today ({date_str})**")
    lines.append("```dataview")
    lines.append(f'TASK FROM "{daily_folder}"')
    lines.append(f'WHERE file.name = "{date_str}" AND !completed')
    lines.append("```")
    lines.append("")
    lines.append(f"**{prev_label}**")
    lines.append("```dataview")
    lines.append(f'TASK FROM "{daily_folder}"')
    lines.append(f'WHERE file.name = "{prev_work_day.isoformat()}" AND !completed')
    lines.append("```")
    lines.append("")
    lines.append(f"**1 Week ago ({week_ago.isoformat()})**")
    lines.append("```dataview")
    lines.append(f'TASK FROM "{daily_folder}"')
    lines.append(f'WHERE file.name = "{week_ago.isoformat()}" AND !completed')
    lines.append("```")
    lines.append("")

    return "\n".join(lines)
