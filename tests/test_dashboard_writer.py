"""Unit tests for dashboard_writer — no I/O, no external API calls."""
from __future__ import annotations

from src.dashboard_writer import (
    compose_assignee_page,
    compose_dashboard,
    filename_for_assignee,
    filename_for_dashboard,
)


# ── filename_for_dashboard ───────────────────────────────────────────── #

class TestFilenameForDashboard:
    def test_returns_dashboard_md(self):
        assert filename_for_dashboard() == "Dashboard.md"

    def test_deterministic(self):
        assert filename_for_dashboard() == filename_for_dashboard()


# ── filename_for_assignee ────────────────────────────────────────────── #

class TestFilenameForAssignee:
    def test_korean_name(self):
        assert filename_for_assignee("박은진") == "박은진.md"

    def test_colon_sanitized(self):
        result = filename_for_assignee("name:with:colons")
        assert ":" not in result
        assert result.endswith(".md")

    def test_slash_sanitized(self):
        result = filename_for_assignee("path/slash")
        assert "/" not in result

    def test_star_sanitized(self):
        result = filename_for_assignee("star*name")
        assert "*" not in result

    def test_leading_trailing_whitespace_stripped(self):
        assert filename_for_assignee("  박은진  ") == "박은진.md"

    def test_empty_name(self):
        result = filename_for_assignee("")
        assert result == ".md"


# ── compose_dashboard ────────────────────────────────────────────────── #

DATE = "2026-04-02"
DAILY = "TeamWorkHub_Daily"
WEEKLY = "TeamWorkHub_Weekly"


class TestComposeDashboardFrontmatter:
    def _fm(self, md: str) -> str:
        parts = md.split("---")
        assert len(parts) >= 3
        return parts[1]

    def test_type_dashboard(self):
        md = compose_dashboard(DATE, DAILY, WEEKLY)
        assert "type: dashboard" in self._fm(md)

    def test_updated_date(self):
        md = compose_dashboard(DATE, DAILY, WEEKLY)
        assert f"updated: {DATE}" in self._fm(md)


class TestComposeDashboardContent:
    def test_returns_string(self):
        assert isinstance(compose_dashboard(DATE, DAILY, WEEKLY), str)

    def test_contains_dataview_blocks(self):
        md = compose_dashboard(DATE, DAILY, WEEKLY)
        assert "```dataview" in md

    def test_contains_tasks_block(self):
        md = compose_dashboard(DATE, DAILY, WEEKLY)
        assert "```tasks" in md

    def test_daily_folder_in_queries(self):
        md = compose_dashboard(DATE, DAILY, WEEKLY)
        assert f'FROM "{DAILY}"' in md

    def test_weekly_folder_in_queries(self):
        md = compose_dashboard(DATE, DAILY, WEEKLY)
        assert f'FROM "{WEEKLY}"' in md

    def test_urgent_section_present(self):
        md = compose_dashboard(DATE, DAILY, WEEKLY)
        assert "긴급" in md

    def test_assignee_section_present(self):
        md = compose_dashboard(DATE, DAILY, WEEKLY)
        assert "담당자" in md

    def test_category_section_present(self):
        md = compose_dashboard(DATE, DAILY, WEEKLY)
        assert "카테고리" in md

    def test_weekly_section_present(self):
        md = compose_dashboard(DATE, DAILY, WEEKLY)
        assert "주간" in md

    def test_footer_present(self):
        md = compose_dashboard(DATE, DAILY, WEEKLY)
        assert "TeamWorkHub" in md

    def test_custom_folders(self):
        md = compose_dashboard(DATE, "MyDaily", "MyWeekly")
        assert 'FROM "MyDaily"' in md
        assert 'FROM "MyWeekly"' in md

    def test_default_folder_names(self):
        md = compose_dashboard(DATE)
        assert f'FROM "{DAILY}"' in md
        assert f'FROM "{WEEKLY}"' in md


# ── compose_assignee_page ────────────────────────────────────────────── #

class TestComposeAssigneePageFrontmatter:
    def _fm(self, md: str) -> str:
        parts = md.split("---")
        assert len(parts) >= 3
        return parts[1]

    def test_type_assignee_page(self):
        md = compose_assignee_page("박은진", DAILY)
        assert "type: assignee-page" in self._fm(md)

    def test_assignee_in_frontmatter(self):
        md = compose_assignee_page("박은진", DAILY)
        assert "assignee: 박은진" in self._fm(md)


class TestComposeAssigneePageContent:
    def test_returns_string(self):
        assert isinstance(compose_assignee_page("박은진", DAILY), str)

    def test_name_in_title(self):
        md = compose_assignee_page("박은진", DAILY)
        assert "박은진" in md

    def test_contains_tasks_block(self):
        md = compose_assignee_page("박은진", DAILY)
        assert "```tasks" in md

    def test_contains_dataview_block(self):
        md = compose_assignee_page("박은진", DAILY)
        assert "```dataview" in md

    def test_daily_folder_in_tasks_query(self):
        md = compose_assignee_page("박은진", DAILY)
        assert f"path includes {DAILY}" in md

    def test_name_in_dataview_filter(self):
        md = compose_assignee_page("박은진", DAILY)
        assert 'contains(assignees, "박은진")' in md

    def test_footer_present(self):
        md = compose_assignee_page("박은진", DAILY)
        assert "TeamWorkHub" in md

    def test_default_folder(self):
        md = compose_assignee_page("홍길동")
        assert f'FROM "{DAILY}"' in md
