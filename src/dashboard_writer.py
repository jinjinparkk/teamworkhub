"""Dashboard and assignee-page Markdown composers.

Generates Obsidian notes that use the Dataview plugin to show
live-queried summaries across all TeamWorkHub daily/weekly notes.

Usage
─────
  from src.dashboard_writer import (
      compose_dashboard, compose_assignee_page,
      filename_for_dashboard, filename_for_assignee,
  )

  dash_md = compose_dashboard("2026-04-02", "TeamWorkHub_Daily", "TeamWorkHub_Weekly")
  page_md = compose_assignee_page("박은진", "TeamWorkHub_Daily")
"""
from __future__ import annotations

import re


def filename_for_dashboard() -> str:
    """Return the Dashboard filename."""
    return "Dashboard.md"


def filename_for_assignee(name: str) -> str:
    """Return a safe filename for an assignee page.

    Example: "박은진" → "박은진.md"
    Special characters that are invalid in filenames are replaced with '_'.
    """
    safe = re.sub(r'[\\/*?"<>|:]', "_", name.strip())
    return f"{safe}.md"


def compose_dashboard(
    updated_date: str,
    daily_folder: str = "TeamWorkHub_Daily",
    weekly_folder: str = "TeamWorkHub_Weekly",
) -> str:
    """Return Dashboard.md content with Dataview plugin queries.

    Args:
        updated_date:   ISO date string (YYYY-MM-DD) shown in the note.
        daily_folder:   Obsidian vault folder name for daily notes.
        weekly_folder:  Obsidian vault folder name for weekly reports.

    Returns a UTF-8 string ready to be written as Dashboard.md.
    Requires Obsidian Dataview and Tasks plugins.
    """
    lines: list[str] = []

    # ── Frontmatter ───────────────────────────────────────────────────── #
    lines.append("---")
    lines.append("type: dashboard")
    lines.append(f"updated: {updated_date}")
    lines.append("---")
    lines.append("")

    # ── Title ─────────────────────────────────────────────────────────── #
    lines.append("# 📊 TeamWorkHub Dashboard")
    lines.append(f"_마지막 업데이트: {updated_date}_")
    lines.append("")
    lines.append(
        "> **플러그인 필요**: Dataview · Tasks  "
        "(Settings → Community plugins)"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Urgent emails ─────────────────────────────────────────────────── #
    lines.append("## 🔴 긴급 메일 (최근 30일)")
    lines.append("")
    lines.append("```dataview")
    lines.append("TABLE date, email_count, period")
    lines.append(f'FROM "{daily_folder}"')
    lines.append("WHERE has_urgent = true")
    lines.append("AND date >= date(today) - dur(30 days)")
    lines.append("SORT date DESC")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Unprocessed checklist ─────────────────────────────────────────── #
    lines.append("## ⚠️ 미처리 항목 (전체)")
    lines.append("")
    lines.append(
        "> Daily Note에서 체크하면 자동으로 사라져요. (Tasks 플러그인 필요)"
    )
    lines.append("")
    lines.append("```tasks")
    lines.append("not done")
    lines.append(f"path includes {daily_folder}")
    lines.append("sort by path")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Recent 7 days ─────────────────────────────────────────────────── #
    lines.append("## 📋 최근 7일 일간 다이제스트")
    lines.append("")
    lines.append("```dataview")
    lines.append("TABLE date, email_count, assignees, categories")
    lines.append(f'FROM "{daily_folder}"')
    lines.append("WHERE date >= date(today) - dur(7 days)")
    lines.append("SORT date DESC")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Assignee breakdown ────────────────────────────────────────────── #
    lines.append("## 👥 담당자별 메일 수 (최근 30일)")
    lines.append("")
    lines.append("```dataview")
    lines.append("TABLE length(rows) AS 메일수")
    lines.append(f'FROM "{daily_folder}"')
    lines.append("WHERE date >= date(today) - dur(30 days)")
    lines.append("FLATTEN assignees AS 담당자")
    lines.append("GROUP BY 담당자")
    lines.append("SORT length(rows) DESC")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Category stats ────────────────────────────────────────────────── #
    lines.append("## 📂 카테고리별 통계 (최근 30일)")
    lines.append("")
    lines.append("```dataview")
    lines.append("TABLE length(rows) AS 메일수")
    lines.append(f'FROM "{daily_folder}"')
    lines.append("WHERE date >= date(today) - dur(30 days)")
    lines.append("FLATTEN categories AS 카테고리")
    lines.append("GROUP BY 카테고리")
    lines.append("SORT length(rows) DESC")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Weekly reports ────────────────────────────────────────────────── #
    lines.append("## 📅 주간 리포트 목록")
    lines.append("")
    lines.append("```dataview")
    lines.append("TABLE week, email_count, period")
    lines.append(f'FROM "{weekly_folder}"')
    lines.append("SORT week DESC")
    lines.append("LIMIT 8")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_TeamWorkHub 자동 생성_")

    return "\n".join(lines)


def compose_assignee_page(
    name: str,
    daily_folder: str = "TeamWorkHub_Daily",
) -> str:
    """Return a per-assignee Obsidian page with Dataview queries.

    Shows this person's assigned emails and unprocessed checklist items.

    Args:
        name:          Assignee name (e.g. "박은진").
        daily_folder:  Obsidian vault folder name for daily notes.

    Returns a UTF-8 string ready to be written as {name}.md.
    """
    lines: list[str] = []

    # ── Frontmatter ───────────────────────────────────────────────────── #
    lines.append("---")
    lines.append("type: assignee-page")
    lines.append(f"assignee: {name}")
    lines.append("---")
    lines.append("")

    lines.append(f"# 👤 {name}")
    lines.append("")

    # ── Unprocessed items ─────────────────────────────────────────────── #
    lines.append("## ⚠️ 미처리 항목")
    lines.append("")
    lines.append("```tasks")
    lines.append("not done")
    lines.append(f"path includes {daily_folder}")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── All assigned emails ───────────────────────────────────────────── #
    lines.append("## 📋 담당 메일 전체 목록")
    lines.append("")
    lines.append("```dataview")
    lines.append("TABLE date, email_count, categories, has_urgent AS 긴급")
    lines.append(f'FROM "{daily_folder}"')
    safe_name = name.replace('"', '\\"')
    lines.append(f'WHERE contains(assignees, "{safe_name}")')
    lines.append("SORT date DESC")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_TeamWorkHub 자동 생성_")

    return "\n".join(lines)
