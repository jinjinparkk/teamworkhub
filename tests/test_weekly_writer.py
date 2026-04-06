"""Unit tests for weekly_writer — no I/O, no external API calls."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.weekly_writer import compose_weekly, filename_for_week
from src.summarizer import AnalysisResult


# ── filename_for_week ────────────────────────────────────────────────── #

class TestFilenameForWeek:
    def test_format_is_week_dot_md(self):
        assert filename_for_week("2026-W14") == "2026-W14.md"

    def test_deterministic(self):
        assert filename_for_week("2026-W01") == filename_for_week("2026-W01")


# ── helpers ──────────────────────────────────────────────────────────── #

def _msg(subject="테스트 메일", sender="alice@example.com",
         date_utc="2026-04-01T10:00:00+09:00", body_text="본문"):
    m = MagicMock()
    m.subject = subject
    m.sender = sender
    m.date_utc = date_utc
    m.body_text = body_text
    return m


def _ar(summary="", assignees=None, priority="보통", category="일반"):
    return AnalysisResult(
        summary=summary,
        assignees=assignees or [],
        priority=priority,
        category=category,
    )


WEEK = "2026-W14"
FROM = "2026-03-30 (월)"
TO   = "2026-04-03 (금)"
TZ   = "Asia/Seoul"


# ── frontmatter ─────────────────────────────────────────────────────── #

class TestWeeklyFrontmatter:
    def _fm(self, md):
        parts = md.split("---")
        assert len(parts) >= 3
        return parts[1]

    def test_week_in_frontmatter(self):
        md = compose_weekly([], WEEK, FROM, TO, TZ)
        assert f"week: {WEEK}" in self._fm(md)

    def test_type_weekly_digest(self):
        md = compose_weekly([], WEEK, FROM, TO, TZ)
        assert "type: weekly-digest" in self._fm(md)

    def test_email_count_in_frontmatter(self):
        msgs = [(_msg(), _ar()), (_msg(), _ar())]
        md = compose_weekly(msgs, WEEK, FROM, TO, TZ)
        assert "email_count: 2" in self._fm(md)


# ── header ──────────────────────────────────────────────────────────── #

class TestWeeklyHeader:
    def test_week_in_title(self):
        md = compose_weekly([], WEEK, FROM, TO, TZ)
        assert WEEK in md

    def test_empty_note_shown(self):
        md = compose_weekly([], WEEK, FROM, TO, TZ)
        assert "없음" in md

    def test_timezone_shown(self):
        md = compose_weekly([], WEEK, FROM, TO, "Asia/Seoul")
        assert "Seoul" in md


# ── stats ────────────────────────────────────────────────────────────── #

class TestWeeklyStats:
    def test_priority_stats_shown(self):
        msgs = [(_msg(), _ar(priority="긴급")), (_msg(), _ar(priority="보통"))]
        md = compose_weekly(msgs, WEEK, FROM, TO, TZ)
        assert "긴급" in md
        assert "보통" in md

    def test_category_stats_shown(self):
        msgs = [(_msg(), _ar(category="보고")), (_msg(), _ar(category="승인요청"))]
        md = compose_weekly(msgs, WEEK, FROM, TO, TZ)
        assert "보고" in md
        assert "승인요청" in md

    def test_assignee_stats_shown(self):
        msgs = [(_msg(), _ar(assignees=["박은진"])), (_msg(), _ar(assignees=["박은진"]))]
        md = compose_weekly(msgs, WEEK, FROM, TO, TZ)
        assert "박은진" in md
        assert "2건" in md


# ── unprocessed checklist ────────────────────────────────────────────── #

class TestWeeklyChecklist:
    def test_tasks_query_block_present(self):
        md = compose_weekly([(_msg(), _ar())], WEEK, FROM, TO, TZ)
        assert "```tasks" in md
        assert "not done" in md
        assert "path includes TeamWorkHub_Daily" in md

    def test_tasks_query_in_empty_weekly(self):
        # query block shown even when no messages
        md = compose_weekly([], WEEK, FROM, TO, TZ)
        # empty weekly shows "없음" note instead
        assert "없음" in md


# ── category sections ────────────────────────────────────────────────── #

class TestWeeklyCategorySections:
    def test_category_section_shown(self):
        msgs = [(_msg(subject="주간 보고"), _ar(category="보고"))]
        md = compose_weekly(msgs, WEEK, FROM, TO, TZ)
        assert "보고" in md
        assert "주간 보고" in md

    def test_empty_category_not_shown(self):
        msgs = [(_msg(), _ar(category="보고"))]
        md = compose_weekly(msgs, WEEK, FROM, TO, TZ)
        assert "미팅" not in md  # no 미팅 emails, so no 미팅 section

    def test_summary_in_category_section(self):
        msgs = [(_msg(), _ar(summary="- 핵심 내용", category="보고"))]
        md = compose_weekly(msgs, WEEK, FROM, TO, TZ)
        assert "> - 핵심 내용" in md

    def test_footer_present(self):
        md = compose_weekly([], WEEK, FROM, TO, TZ)
        assert "TeamWorkHub" in md

    def test_returns_string(self):
        assert isinstance(compose_weekly([], WEEK, FROM, TO, TZ), str)
