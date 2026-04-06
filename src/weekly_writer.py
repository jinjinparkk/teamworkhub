"""Weekly digest Markdown composer.

Generates a single Obsidian weekly report aggregating Mon~Fri emails.
Groups by category and priority, lists unprocessed items.

Output filename pattern: YYYY-WNN.md  (e.g. 2026-W14.md)

Usage
─────
  from src.weekly_writer import compose_weekly, filename_for_week

  md = compose_weekly(messages_with_analysis, "2026-W14",
                      "2026-03-30", "2026-04-03", "Asia/Seoul")
  name = filename_for_week("2026-W14")   # "2026-W14.md"
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.gmail_client import ParsedMessage
    from src.summarizer import AnalysisResult

_PRIORITY_EMOJI = {"긴급": "🔴", "보통": "🟡", "낮음": "🟢"}
_CATEGORY_EMOJI = {
    "보고": "📊", "승인요청": "✅", "공지": "📢", "미팅": "📅", "일반": "📧"
}
_PRIORITY_ORDER = ["긴급", "보통", "낮음"]
_CATEGORY_ORDER = ["보고", "승인요청", "공지", "미팅", "일반"]


def filename_for_week(week_str: str) -> str:
    """Return the Weekly Note filename for *week_str* (``YYYY-WNN``).

    Example: "2026-W14" → "2026-W14.md"
    """
    return f"{week_str}.md"


def compose_weekly(
    messages: list[tuple["ParsedMessage", "AnalysisResult"]],
    week_str: str,
    date_from: str,
    date_to: str,
    timezone_name: str = "Asia/Seoul",
) -> str:
    """Return a Weekly Report Markdown string.

    Args:
        messages:      List of (ParsedMessage, AnalysisResult) pairs for the week.
        week_str:      ISO week string, e.g. "2026-W14".
        date_from:     First day of the week, e.g. "2026-03-30 (월)".
        date_to:       Last day of the week, e.g. "2026-04-03 (금)".
        timezone_name: Timezone label shown in the note header.

    Returns a UTF-8 string ready to be written as a .md file.
    """
    tz_short = timezone_name.split("/")[-1] if "/" in timezone_name else timezone_name
    count = len(messages)

    lines: list[str] = []

    # ── YAML frontmatter ──────────────────────────────────────────────── #
    lines.append("---")
    lines.append(f"week: {week_str}")
    lines.append(f"type: weekly-digest")
    lines.append(f'period: "{date_from} ~ {date_to} ({tz_short})"')
    lines.append(f"email_count: {count}")
    lines.append("---")
    lines.append("")

    # ── Header ────────────────────────────────────────────────────────── #
    lines.append(f"# 📋 {week_str} 주간 메일 리포트 ({count}건)")
    lines.append(f"_기간: {date_from} ~ {date_to} ({tz_short})_")
    lines.append("")

    if not messages:
        lines.append("_해당 주 수신 메일 없음_")
        lines.append("")
        lines.append("---")
        lines.append("_TeamWorkHub 자동 생성_")
        return "\n".join(lines)

    # ── Stats ─────────────────────────────────────────────────────────── #
    priority_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    assignee_counts: dict[str, int] = {}

    for _, ar in messages:
        priority_counts[ar.priority] = priority_counts.get(ar.priority, 0) + 1
        category_counts[ar.category] = category_counts.get(ar.category, 0) + 1
        for name in ar.assignees:
            assignee_counts[name] = assignee_counts.get(name, 0) + 1

    lines.append("## 📊 주간 통계")
    lines.append("")
    lines.append("**우선순위별:**")
    for p in _PRIORITY_ORDER:
        if priority_counts.get(p):
            lines.append(f"- {_PRIORITY_EMOJI[p]} {p}: {priority_counts[p]}건")
    lines.append("")
    lines.append("**카테고리별:**")
    for c in _CATEGORY_ORDER:
        if category_counts.get(c):
            lines.append(f"- {_CATEGORY_EMOJI[c]} {c}: {category_counts[c]}건")
    lines.append("")
    if assignee_counts:
        lines.append("**담당자별:**")
        for name, cnt in sorted(assignee_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- #{name}: {cnt}건")
        lines.append("")

    lines.append("---")
    lines.append("")

    # ── Unprocessed items — Obsidian Tasks plugin live query ─────────── #
    lines.append("## ⚠️ 미처리 체크리스트")
    lines.append("")
    lines.append("> Daily Note에서 체크하면 여기서도 자동으로 사라져요. (Obsidian Tasks 플러그인 필요)")
    lines.append("")
    lines.append("```tasks")
    lines.append("not done")
    lines.append("path includes TeamWorkHub_Daily")
    lines.append("sort by path")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Full list by category ─────────────────────────────────────────── #
    lines.append("## 📂 카테고리별 전체 목록")
    lines.append("")
    for cat in _CATEGORY_ORDER:
        cat_msgs = [(msg, ar) for msg, ar in messages if ar.category == cat]
        if not cat_msgs:
            continue
        lines.append(f"### {_CATEGORY_EMOJI[cat]} {cat} ({len(cat_msgs)}건)")
        for msg, ar in cat_msgs:
            p_emoji = _PRIORITY_EMOJI.get(ar.priority, "🟡")
            assignee_str = ", ".join(f"#{n}" for n in ar.assignees) if ar.assignees else "#미지정"
            lines.append(f"#### {msg.subject or '(제목 없음)'}")
            lines.append(f"{p_emoji} #{ar.priority}  {assignee_str}  ")
            lines.append(f"**From:** {msg.sender}  **Date:** {msg.date_utc[:10]}")
            if ar.summary:
                lines.append("")
                for bullet in ar.summary.strip().splitlines():
                    lines.append(f"> {bullet}")
            lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("_TeamWorkHub 자동 생성_")
    return "\n".join(lines)
