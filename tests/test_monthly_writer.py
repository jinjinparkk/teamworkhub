"""Unit tests for monthly_writer — no I/O, no external API calls."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.monthly_writer import compose_monthly, filename_for_month
from src.summarizer import AnalysisResult


# ── filename_for_month ───────────────────────────────────────────────── #

class TestFilenameForMonth:
    def test_format_is_month_dot_md(self):
        assert filename_for_month("2026-04") == "2026-04.md"

    def test_deterministic(self):
        assert filename_for_month("2026-01") == filename_for_month("2026-01")


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


MONTH = "2026-04"
FROM  = "2026-04-01"
TO    = "2026-04-30"
TZ    = "Asia/Seoul"


# ── frontmatter ──────────────────────────────────────────────────────── #

class TestMonthlyFrontmatter:
    def _fm(self, md):
        parts = md.split("---")
        assert len(parts) >= 3
        return parts[1]

    def test_month_in_frontmatter(self):
        md = compose_monthly([], MONTH, FROM, TO, TZ)
        assert f"month: {MONTH}" in self._fm(md)

    def test_type_monthly_digest(self):
        md = compose_monthly([], MONTH, FROM, TO, TZ)
        assert "type: monthly-digest" in self._fm(md)

    def test_email_count_in_frontmatter(self):
        msgs = [(_msg(), _ar()), (_msg(), _ar())]
        md = compose_monthly(msgs, MONTH, FROM, TO, TZ)
        assert "email_count: 2" in self._fm(md)


# ── header ───────────────────────────────────────────────────────────── #

class TestMonthlyHeader:
    def test_month_in_title(self):
        md = compose_monthly([], MONTH, FROM, TO, TZ)
        assert MONTH in md

    def test_empty_note_shown(self):
        md = compose_monthly([], MONTH, FROM, TO, TZ)
        assert "없음" in md

    def test_timezone_shown(self):
        md = compose_monthly([], MONTH, FROM, TO, "Asia/Seoul")
        assert "Seoul" in md


# ── stats ────────────────────────────────────────────────────────────── #

class TestMonthlyStats:
    def test_priority_stats_shown(self):
        msgs = [(_msg(), _ar(priority="긴급")), (_msg(), _ar(priority="보통"))]
        md = compose_monthly(msgs, MONTH, FROM, TO, TZ)
        assert "긴급" in md
        assert "보통" in md

    def test_category_stats_shown(self):
        msgs = [(_msg(), _ar(category="보고")), (_msg(), _ar(category="승인요청"))]
        md = compose_monthly(msgs, MONTH, FROM, TO, TZ)
        assert "보고" in md
        assert "승인요청" in md

    def test_assignee_stats_shown(self):
        msgs = [(_msg(), _ar(assignees=["박은진"])), (_msg(), _ar(assignees=["박은진"]))]
        md = compose_monthly(msgs, MONTH, FROM, TO, TZ)
        assert "박은진" in md
        assert "2건" in md


# ── top senders ──────────────────────────────────────────────────────── #

class TestMonthlyTopSenders:
    def test_sender_shown(self):
        msgs = [(_msg(sender="boss@company.com"), _ar())]
        md = compose_monthly(msgs, MONTH, FROM, TO, TZ)
        assert "boss@company.com" in md

    def test_top_5_limit(self):
        # 6 different senders — only top 5 should appear
        senders = [f"user{i}@test.com" for i in range(6)]
        msgs = [(_msg(sender=s), _ar()) for s in senders]
        # sender 0 appears twice → top
        msgs.append((_msg(sender=senders[0]), _ar()))
        md = compose_monthly(msgs, MONTH, FROM, TO, TZ)
        assert "발신자 TOP 5" in md

    def test_most_frequent_sender_first(self):
        msgs = [
            (_msg(sender="frequent@test.com"), _ar()),
            (_msg(sender="frequent@test.com"), _ar()),
            (_msg(sender="rare@test.com"), _ar()),
        ]
        md = compose_monthly(msgs, MONTH, FROM, TO, TZ)
        freq_pos = md.index("frequent@test.com")
        rare_pos = md.index("rare@test.com")
        assert freq_pos < rare_pos


# ── tasks query ──────────────────────────────────────────────────────── #

class TestMonthlyChecklist:
    def test_tasks_query_block_present(self):
        md = compose_monthly([(_msg(), _ar())], MONTH, FROM, TO, TZ)
        assert "```tasks" in md
        assert "not done" in md
        assert "path includes TeamWorkHub_Daily" in md


# ── category sections ────────────────────────────────────────────────── #

class TestMonthlyCategorySections:
    def test_category_section_shown(self):
        msgs = [(_msg(subject="월간 보고"), _ar(category="보고"))]
        md = compose_monthly(msgs, MONTH, FROM, TO, TZ)
        assert "보고" in md
        assert "월간 보고" in md

    def test_empty_category_not_shown(self):
        msgs = [(_msg(), _ar(category="보고"))]
        md = compose_monthly(msgs, MONTH, FROM, TO, TZ)
        assert "미팅" not in md

    def test_summary_in_category_section(self):
        msgs = [(_msg(), _ar(summary="- 핵심 내용", category="보고"))]
        md = compose_monthly(msgs, MONTH, FROM, TO, TZ)
        assert "> - 핵심 내용" in md

    def test_footer_present(self):
        md = compose_monthly([], MONTH, FROM, TO, TZ)
        assert "TeamWorkHub" in md

    def test_returns_string(self):
        assert isinstance(compose_monthly([], MONTH, FROM, TO, TZ), str)
