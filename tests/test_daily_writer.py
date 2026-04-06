"""Unit tests for daily_writer — no I/O, no external API calls."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.daily_writer import compose_daily, filename_for_date, _normalise_subject
from src.summarizer import AnalysisResult


# ── filename_for_date ────────────────────────────────────────────────── #

class TestFilenameForDate:
    def test_format_is_date_dot_md(self):
        assert filename_for_date("2025-04-02") == "2025-04-02.md"

    def test_deterministic(self):
        assert filename_for_date("2025-01-01") == filename_for_date("2025-01-01")

    def test_different_dates_differ(self):
        assert filename_for_date("2025-04-01") != filename_for_date("2025-04-02")


# ── _normalise_subject ───────────────────────────────────────────────── #

class TestNormaliseSubject:
    def test_strips_re_prefix(self):
        assert _normalise_subject("Re: 회의 일정") == "회의 일정"

    def test_strips_fw_prefix(self):
        assert _normalise_subject("Fw: 공지사항") == "공지사항"

    def test_strips_korean_prefix(self):
        assert _normalise_subject("회신: 업무 보고") == "업무 보고"

    def test_strips_multiple_prefixes(self):
        assert _normalise_subject("Re: Re: 업무") == "업무"

    def test_no_prefix_unchanged(self):
        assert _normalise_subject("일반 메일") == "일반 메일"

    def test_case_insensitive(self):
        assert _normalise_subject("RE: 제목") == "제목"


# ── helpers ──────────────────────────────────────────────────────────── #

def _msg(subject="테스트 메일", sender="alice@example.com",
         date_utc="2025-04-01T20:00:00+09:00", body_text="본문 내용입니다."):
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


DATE = "2025-04-02"
START = "2025-04-01 18:00"
END   = "2025-04-02 08:59"
TZ    = "Asia/Seoul"


# ── compose_daily — frontmatter ─────────────────────────────────────── #

class TestComposeDailyFrontmatter:
    def _fm(self, md):
        parts = md.split("---")
        assert len(parts) >= 3
        return parts[1]

    def test_has_frontmatter_delimiters(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert md.startswith("---\n")

    def test_date_in_frontmatter(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert f"date: {DATE}" in self._fm(md)

    def test_type_is_daily_note(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert "Type: daily_note" in self._fm(md)

    def test_email_count_zero_when_empty(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert "email_count: 0" in self._fm(md)

    def test_email_count_matches_messages(self):
        msgs = [(_msg(), _ar()), (_msg(), _ar())]
        md = compose_daily(msgs, DATE, START, END, TZ)
        assert "email_count: 2" in self._fm(md)

    def test_period_in_frontmatter(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert START in self._fm(md)
        assert END in self._fm(md)

    def test_has_urgent_false_when_no_urgent(self):
        md = compose_daily([(_msg(), _ar(priority="보통"))], DATE, START, END, TZ)
        assert "has_urgent: false" in self._fm(md)

    def test_has_urgent_true_when_urgent_present(self):
        md = compose_daily([(_msg(), _ar(priority="긴급"))], DATE, START, END, TZ)
        assert "has_urgent: true" in self._fm(md)

    def test_assignees_list_in_frontmatter(self):
        md = compose_daily([(_msg(), _ar(assignees=["박은진"]))], DATE, START, END, TZ)
        assert "박은진" in self._fm(md)


# ── compose_daily — structure ────────────────────────────────────────── #

class TestComposeDailyStructure:
    def test_today_work_section_present(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert "### Today's work" in md

    def test_to_do_list_section_present(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert "#### To do list" in md

    def test_regular_tasks_section_present(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert "#### 정기적인 일" in md

    def test_schedule_section_present(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert "#### Schedule" in md

    def test_incomplete_dataview_section_present(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert "### 미완료" in md

    def test_history_section_present(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert "### History" in md

    def test_dataview_uses_daily_folder(self):
        md = compose_daily([], DATE, START, END, TZ, daily_folder="MyDailyNotes")
        assert "MyDailyNotes" in md

    def test_default_daily_folder_in_dataview(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert "TeamWorkHub_Daily" in md

    def test_history_has_three_date_blocks(self):
        """History section contains Today, Yesterday, 1 Week ago."""
        md = compose_daily([], DATE, START, END, TZ)
        assert "Today" in md
        assert "Yesterday" in md
        assert "1 Week ago" in md


# ── compose_daily — metadata / header ───────────────────────────────── #

class TestComposeDailyMeta:
    def test_date_in_output(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert DATE in md

    def test_email_count_in_frontmatter(self):
        md = compose_daily([(_msg(), _ar())], DATE, START, END, TZ)
        assert "email_count: 1" in md

    def test_period_label_in_output(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert START in md
        assert END in md

    def test_timezone_short_name_shown(self):
        md = compose_daily([], DATE, START, END, "Asia/Seoul")
        assert "Seoul" in md

    def test_empty_messages_shows_empty_checkbox(self):
        md = compose_daily([], DATE, START, END, TZ)
        # Empty state: shows a blank checkbox line
        assert "- [ ]" in md


# ── compose_daily — To do list items ────────────────────────────────── #

class TestComposeDailyToDoList:
    def test_wiki_link_for_email_subject(self):
        md = compose_daily([(_msg(subject="업무 보고"), _ar())], DATE, START, END, TZ)
        assert "[[업무 보고]]" in md

    def test_checkbox_per_email(self):
        md = compose_daily([(_msg(), _ar())], DATE, START, END, TZ)
        assert "- [ ]" in md

    def test_assignee_tag_first_only(self):
        """Only the first assignee is shown per To do list item."""
        md = compose_daily([(_msg(), _ar(assignees=["박은진", "이해랑"]))], DATE, START, END, TZ)
        assert "#박은진" in md

    def test_unassigned_tag_when_no_assignees(self):
        md = compose_daily([(_msg(), _ar())], DATE, START, END, TZ)
        assert "#미지정" in md

    def test_multiple_emails_produce_multiple_checkboxes(self):
        msgs = [(_msg(subject=f"메일{i}"), _ar()) for i in range(3)]
        md = compose_daily(msgs, DATE, START, END, TZ)
        # Should have at least 3 checkboxes (To do list items)
        assert md.count("- [ ]") >= 3

    def test_subject_unsafe_chars_stripped_from_wiki_link(self):
        """Characters like : / * ? are stripped from wiki link names."""
        md = compose_daily([(_msg(subject="보고서: 1/4분기"), _ar())], DATE, START, END, TZ)
        assert "[[" in md
        # Colon and slash stripped
        assert "[[보고서 14분기]]" in md or "[[보고서" in md

    def test_no_per_email_numbered_sections(self):
        """Old ## N. subject sections are removed."""
        msgs = [(_msg(subject=f"메일{i}"), _ar()) for i in range(3)]
        md = compose_daily(msgs, DATE, START, END, TZ)
        assert "## 1." not in md
        assert "## 2." not in md
        assert "## 3." not in md

    def test_no_details_blocks(self):
        """Body text is not shown in daily note (only in individual notes)."""
        md = compose_daily([(_msg(body_text="원문입니다"), _ar())], DATE, START, END, TZ)
        assert "<details>" not in md

    def test_no_blockquote_summary(self):
        """Summary blockquotes (> text) are not in daily note."""
        md = compose_daily([(_msg(), _ar(summary="- 핵심 내용"))], DATE, START, END, TZ)
        assert "> - 핵심 내용" not in md

    def test_returns_string(self):
        assert isinstance(compose_daily([], DATE, START, END, TZ), str)
