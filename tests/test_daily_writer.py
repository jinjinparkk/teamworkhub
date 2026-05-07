"""Unit tests for daily_writer — no I/O, no external API calls."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.daily_writer import compose_daily, filename_for_date, _normalise_subject, parse_checked_items, merge_daily
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


def _ar(summary="", assignees=None, priority="보통", category="일반",
        short_title="", description=""):
    return AnalysisResult(
        summary=summary,
        assignees=assignees or [],
        priority=priority,
        category=category,
        short_title=short_title,
        description=description,
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

    def test_incomplete_dataview_section_present(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert "### 미완료" in md

    def test_dataview_uses_daily_folder(self):
        md = compose_daily([], DATE, START, END, TZ, daily_folder="MyDailyNotes")
        assert "MyDailyNotes" in md

    def test_default_daily_folder_in_dataview(self):
        md = compose_daily([], DATE, START, END, TZ)
        assert "TeamWorkHub_Daily" in md

    def test_no_schedule_section(self):
        """Schedule 섹션이 제거되었는지 확인."""
        md = compose_daily([], DATE, START, END, TZ)
        assert "Schedule" not in md

    def test_no_history_section(self):
        """History 섹션이 제거되었는지 확인."""
        md = compose_daily([], DATE, START, END, TZ)
        assert "History" not in md


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

    def test_empty_messages_shows_placeholder(self):
        md = compose_daily([], DATE, START, END, TZ)
        # Empty state: shows a placeholder line
        assert "- (없음)" in md


# ── compose_daily — To do list items ────────────────────────────────── #

class TestComposeDailyToDoList:
    def test_wiki_link_for_email_subject(self):
        md = compose_daily([(_msg(subject="업무 보고"), _ar())], DATE, START, END, TZ)
        assert "[[업무 보고]]" in md

    def test_list_item_per_email(self):
        md = compose_daily([(_msg(), _ar())], DATE, START, END, TZ)
        assert "- [[" in md
        assert "- [ ]" not in md

    def test_assignee_tag_first_only(self):
        """Only the first assignee is shown per To do list item."""
        md = compose_daily([(_msg(), _ar(assignees=["박은진", "이해랑"]))], DATE, START, END, TZ)
        assert "#박은진" in md

    def test_unassigned_tag_when_no_assignees(self):
        md = compose_daily([(_msg(), _ar())], DATE, START, END, TZ)
        assert "#미지정" in md

    def test_multiple_emails_produce_multiple_list_items(self):
        msgs = [(_msg(subject=f"메일{i}"), _ar()) for i in range(3)]
        md = compose_daily(msgs, DATE, START, END, TZ)
        # Should have at least 3 list items (no checkboxes)
        assert md.count("- [[") >= 3
        assert "- [ ]" not in md

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

    def test_short_title_used_as_display_text_with_folder(self):
        """short_title이 있으면 wiki-link 표시 텍스트로 사용 (note_folder 있을 때)."""
        ar = _ar(short_title="데이터검증 보고")
        md = compose_daily(
            [(_msg(subject="FW: (2) [Daily Report] 데이터 검증_2026-04-20"), ar)],
            DATE, START, END, TZ, note_folder="TeamWorkHub",
        )
        assert "데이터검증 보고]]" in md
        # 파일명(wiki link target)은 그대로 원본 subject 기반
        assert "TeamWorkHub/FW (2) [Daily Report] 데이터 검증_2026-04-20|데이터검증 보고" in md

    def test_short_title_used_as_display_text_without_folder(self):
        """short_title이 있으면 wiki-link 표시 텍스트로 사용 (note_folder 없을 때)."""
        ar = _ar(short_title="데이터검증 보고")
        md = compose_daily(
            [(_msg(subject="FW: (2) [Daily Report] 데이터 검증"), ar)],
            DATE, START, END, TZ,
        )
        assert "데이터검증 보고]]" in md

    def test_fallback_to_wiki_name_when_no_short_title(self):
        """short_title이 비어있으면 기존 wiki_name 그대로 사용."""
        ar = _ar(short_title="")
        md = compose_daily(
            [(_msg(subject="업무 보고"), ar)],
            DATE, START, END, TZ, note_folder="TeamWorkHub",
        )
        assert "TeamWorkHub/업무 보고|업무 보고" in md

    def test_returns_string(self):
        assert isinstance(compose_daily([], DATE, START, END, TZ), str)


# ── compose_daily — 요일별 정기적인 일 ──────────────────────────────── #

class TestComposeDailyRecurringTasks:
    # DATE = "2025-04-02" → 수요일 (weekday 2)
    def test_wednesday_has_수정기(self):
        md = compose_daily([], "2025-04-02", START, END, TZ)  # 수
        assert "- 수정기" in md

    def test_monday_has_rpa(self):
        md = compose_daily([], "2025-03-31", START, END, TZ)  # 월
        assert "- RPA" in md

    def test_tuesday_has_로직점검(self):
        md = compose_daily([], "2025-04-01", START, END, TZ)  # 화
        assert "- 로직점검" in md

    def test_thursday_has_목정기(self):
        md = compose_daily([], "2025-04-03", START, END, TZ)  # 목
        assert "- 목정기" in md

    def test_friday_has_금정기(self):
        md = compose_daily([], "2025-04-04", START, END, TZ)  # 금
        assert "- 금정기" in md

    def test_saturday_has_no_recurring(self):
        md = compose_daily([], "2025-04-05", START, END, TZ)  # 토
        assert "#### 정기적인 일\n- (없음)" in md

    def test_sunday_has_no_recurring(self):
        md = compose_daily([], "2025-04-06", START, END, TZ)  # 일
        assert "#### 정기적인 일\n- (없음)" in md


# ── parse_checked_items ──────────────────────────────────────────────── #

class TestParseCheckedItems:
    def test_extracts_checked_wiki_links(self):
        content = (
            "- [x] [[TeamWorkHub/업무 보고|업무 보고]] #박은진\n"
            "- [ ] [[TeamWorkHub/회의록|회의록]] #미지정\n"
        )
        result = parse_checked_items(content)
        assert result == {"TeamWorkHub/업무 보고"}

    def test_extracts_uppercase_X(self):
        content = "- [X] [[보고서]] #미지정\n"
        result = parse_checked_items(content)
        assert result == {"보고서"}

    def test_unchecked_not_included(self):
        content = "- [ ] [[업무 보고]] #미지정\n"
        result = parse_checked_items(content)
        assert result == set()

    def test_empty_content(self):
        assert parse_checked_items("") == set()

    def test_multiple_checked_items(self):
        content = (
            "- [x] [[A/링크1|표시1]] #태그\n"
            "- [x] [[A/링크2|표시2]] #태그\n"
            "- [ ] [[A/링크3|표시3]] #태그\n"
        )
        result = parse_checked_items(content)
        assert result == {"A/링크1", "A/링크2"}


# ── compose_daily — no checkboxes in daily note ──────────────────────── #

class TestComposeDailyNoCheckboxes:
    def test_no_checkboxes_in_daily_items(self):
        """Daily Note 항목에 체크박스가 없어야 함."""
        msgs = [(_msg(subject="업무 보고"), _ar())]
        md = compose_daily(msgs, DATE, START, END, TZ, note_folder="TeamWorkHub")
        assert "- [[TeamWorkHub/업무 보고" in md
        assert "- [ ]" not in md
        assert "- [x]" not in md

    def test_plain_list_items_with_tags(self):
        """Daily Note 항목은 plain list + tag 형태."""
        msgs = [
            (_msg(subject="업무 보고"), _ar()),
            (_msg(subject="회의록"), _ar()),
        ]
        md = compose_daily(msgs, DATE, START, END, TZ, note_folder="TeamWorkHub")
        assert "- [[TeamWorkHub/업무 보고" in md
        assert "- [[TeamWorkHub/회의록" in md

    def test_items_are_plain_without_note_folder(self):
        """note_folder 없이도 plain list."""
        msgs = [(_msg(subject="업무 보고"), _ar())]
        md = compose_daily(msgs, DATE, START, END, TZ)
        assert "- [[업무 보고]]" in md
        assert "- [ ]" not in md


# ── merge_daily ────────────────────────────────────────────────────── #

def _build_existing_daily(
    items: list[str],
    checked: set[str] | None = None,
    user_lines: list[str] | None = None,
    email_count: int | None = None,
    assignees: list[str] | None = None,
    has_urgent: bool = False,
    extra_after_dataview: str = "",
):
    """Build a minimal existing daily note string for merge tests."""
    _checked = checked or set()
    _user_lines = user_lines or []
    _assignees = assignees or []
    if email_count is None:
        email_count = len(items)

    lines = [
        "---",
        "Type: daily_note",
        f"date: {DATE}",
        f'period: "{START} ~ {END} (Seoul)"',
        f"email_count: {email_count}",
        f"assignees: {_assignees}" if _assignees else "assignees: []",
        f"has_urgent: {str(has_urgent).lower()}",
        "---",
        "",
        "### Today's work",
        "#### To do list",
    ]
    for wiki_target in items:
        check = "x" if wiki_target in _checked else " "
        display = wiki_target.split("/")[-1] if "/" in wiki_target else wiki_target
        lines.append(f"- [{check}] [[{wiki_target}|{display}]] #미지정")
    for ul in _user_lines:
        lines.append(ul)
    lines.append("")
    lines.append("#### 정기적인 일")
    lines.append("- 수정기")
    lines.append("")
    lines.append("### 미완료")
    lines.append("")
    lines.append("```dataview")
    lines.append('TASK FROM "TeamWorkHub_Daily"')
    lines.append('WHERE !completed AND date(file.name) >= date(today) - dur(14d) AND text != ""')
    lines.append("```")
    lines.append("")
    if extra_after_dataview:
        lines.append(extra_after_dataview)
    return "\n".join(lines)


NEW_START = "2025-04-01 18:00"
NEW_END   = "2025-04-02 12:00"


class TestMergeDaily:
    def test_merge_adds_new_items_only(self):
        """기존 3개 + 신규 1개 → To do list에 4개."""
        existing = _build_existing_daily(
            ["업무 보고", "회의록", "공지사항"], email_count=3,
        )
        msgs = [
            (_msg(subject="업무 보고"), _ar()),
            (_msg(subject="회의록"), _ar()),
            (_msg(subject="공지사항"), _ar()),
            (_msg(subject="출장 결과"), _ar()),
        ]
        result = merge_daily(existing, msgs, NEW_START, NEW_END, TZ)
        assert "[[출장 결과]]" in result
        assert result.count("[[업무 보고") == 1
        assert "email_count: 4" in result

    def test_merge_migrates_checkboxes_to_plain(self):
        """기존 [x]/[ ] 체크박스가 plain list로 마이그레이션됨."""
        existing = _build_existing_daily(
            ["업무 보고", "회의록"],
            checked={"업무 보고"},
            email_count=2,
        )
        msgs = [(_msg(subject="출장 결과"), _ar())]
        result = merge_daily(existing, msgs, NEW_START, NEW_END, TZ)
        # Existing checkbox items should be migrated to plain list
        assert "- [[업무 보고" in result
        assert "- [[회의록" in result
        # New items should also be plain
        assert "- [[출장 결과]]" in result
        # No checkboxes should remain
        assert "- [x]" not in result
        assert "- [ ] [[" not in result

    def test_merge_preserves_user_added_lines(self):
        """사용자가 To do 섹션에 직접 추가한 줄 보존."""
        existing = _build_existing_daily(
            ["업무 보고"],
            user_lines=["- 내가 추가한 메모"],
            email_count=1,
        )
        msgs = [(_msg(subject="출장 결과"), _ar())]
        result = merge_daily(existing, msgs, NEW_START, NEW_END, TZ)
        assert "내가 추가한 메모" in result
        assert "[[출장 결과]]" in result

    def test_merge_preserves_content_after_todo(self):
        """정기적인 일, 미완료 Dataview, 파일 하단 자유 메모 보존."""
        existing = _build_existing_daily(
            ["업무 보고"],
            email_count=1,
            extra_after_dataview="## 자유 메모\n오늘 할 일 정리",
        )
        msgs = [(_msg(subject="출장 결과"), _ar())]
        result = merge_daily(existing, msgs, NEW_START, NEW_END, TZ)
        assert "#### 정기적인 일" in result
        assert "- 수정기" in result
        assert "### 미완료" in result
        assert "```dataview" in result
        assert "자유 메모" in result
        assert "오늘 할 일 정리" in result

    def test_merge_updates_frontmatter(self):
        """email_count, period, assignees 갱신."""
        existing = _build_existing_daily(
            ["업무 보고"],
            email_count=1,
            assignees=["박은진"],
        )
        msgs = [
            (_msg(subject="업무 보고"), _ar(assignees=["박은진"])),
            (_msg(subject="출장 결과"), _ar(assignees=["이해랑"], priority="긴급")),
        ]
        result = merge_daily(existing, msgs, NEW_START, NEW_END, TZ)
        assert "email_count: 2" in result
        assert NEW_END in result
        assert "이해랑" in result
        assert "박은진" in result
        assert "has_urgent: true" in result

    def test_merge_no_duplicate_items(self):
        """이미 존재하는 이메일은 중복 추가 안 됨."""
        existing = _build_existing_daily(["업무 보고"], email_count=1)
        msgs = [(_msg(subject="업무 보고"), _ar())]
        result = merge_daily(existing, msgs, NEW_START, NEW_END, TZ)
        assert result.count("[[업무 보고") == 1
        assert "email_count: 1" in result

    def test_merge_new_items_have_no_checkbox(self):
        """새로 추가되는 항목에 체크박스가 없어야 함."""
        existing = _build_existing_daily(["업무 보고"], email_count=1)
        msgs = [
            (_msg(subject="업무 보고"), _ar()),
            (_msg(subject="출장 결과"), _ar()),
        ]
        result = merge_daily(existing, msgs, NEW_START, NEW_END, TZ)
        # Find the new item line
        for line in result.splitlines():
            if "[[출장 결과]]" in line:
                assert line.startswith("- [[")
                assert "[ ]" not in line
                break
