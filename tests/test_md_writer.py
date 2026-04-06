"""Unit tests for md_writer — no I/O, no external API calls.

Covers:
  filename_for        : determinism, angle-bracket stripping, unsafe chars,
                        empty input, @ and spaces, account prefix
  filename_for_subject: subject sanitisation, extension, edge cases
  compose             : YAML frontmatter keys (email_title, date, sender,
                        attachment, tags), sections (### 요약, ### 본문,
                        ### 첨부파일 링크), analysis integration
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.md_writer import compose, filename_for, filename_for_subject
from src.summarizer import AnalysisResult


# ── Shared fixtures ─────────────────────────────────────────────────── #

def _msg(
    message_id: str = "msg001",
    thread_id: str = "thread001",
    subject: str = "Weekly Report",
    sender: str = "alice@example.com",
    date_utc: str = "2024-01-15T10:30:00+00:00",
    body_text: str = "Hello, here is the weekly report.",
    attachments: list | None = None,
) -> MagicMock:
    """Return a minimal ParsedMessage-like mock."""
    m = MagicMock()
    m.message_id = message_id
    m.thread_id = thread_id
    m.subject = subject
    m.sender = sender
    m.date_utc = date_utc
    m.body_text = body_text
    m.attachments = attachments if attachments is not None else []
    return m


def _df(
    file_id: str = "drive_id_1",
    name: str = "msg001_report.pdf",
    web_view_link: str = "https://drive.google.com/file/d/drive_id_1/view",
    created: bool = True,
) -> MagicMock:
    """Return a minimal DriveFile-like mock."""
    df = MagicMock()
    df.file_id = file_id
    df.name = name
    df.web_view_link = web_view_link
    df.created = created
    return df


def _ar(summary="", assignees=None, priority="보통", category="일반"):
    return AnalysisResult(
        summary=summary,
        assignees=assignees or [],
        priority=priority,
        category=category,
    )


PROCESSED_AT = "2024-01-15T11:00:00+00:00"


# ── filename_for ─────────────────────────────────────────────────────── #

class TestFilenameFor:
    def test_prefix_is_twh(self):
        assert filename_for("msg001").startswith("twh_")

    def test_suffix_is_md(self):
        assert filename_for("msg001").endswith(".md")

    def test_deterministic(self):
        assert filename_for("msg001") == filename_for("msg001")

    def test_different_ids_differ(self):
        assert filename_for("msg001") != filename_for("msg002")

    def test_angle_brackets_stripped(self):
        name = filename_for("<CAMsg001@mail.gmail.com>")
        assert "<" not in name
        assert ">" not in name

    def test_at_sign_replaced(self):
        assert "@" not in filename_for("<CAMsg001@mail.gmail.com>")

    def test_spaces_replaced(self):
        assert " " not in filename_for("msg with spaces")

    def test_empty_string_fallback(self):
        name = filename_for("")
        assert name == "twh_unknown.md"

    def test_plain_id_unchanged_content(self):
        name = filename_for("plainID123")
        assert "plainID123" in name

    def test_two_calls_same_unicode_input(self):
        a = filename_for("重要なmsg-001")
        b = filename_for("重要なmsg-001")
        assert a == b

    def test_account_email_adds_prefix(self):
        name = filename_for("msg001", "alice@example.com")
        assert name.startswith("twh_alice_")

    def test_account_email_strips_domain(self):
        """Only the part before @ is used as prefix."""
        name = filename_for("msg001", "bob@example.com")
        assert "example" not in name.split("_msg001")[0]

    def test_account_email_empty_no_prefix_change(self):
        assert filename_for("msg001", "") == filename_for("msg001")

    def test_account_email_special_chars_sanitised(self):
        """Special chars in the email local-part are replaced with underscores."""
        name = filename_for("msg001", "user.name+tag@example.com")
        assert "<" not in name
        assert ">" not in name
        assert "+" not in name

    def test_different_accounts_different_filenames(self):
        a = filename_for("msg001", "alice@example.com")
        b = filename_for("msg001", "bob@example.com")
        assert a != b

    def test_same_account_same_message_deterministic(self):
        a = filename_for("msg001", "alice@example.com")
        b = filename_for("msg001", "alice@example.com")
        assert a == b


# ── filename_for_subject ─────────────────────────────────────────────── #

class TestFilenameForSubject:
    def test_suffix_is_md(self):
        assert filename_for_subject("CM360 확인").endswith(".md")

    def test_subject_preserved_in_name(self):
        name = filename_for_subject("CM360 확인")
        assert "CM360 확인" in name

    def test_colon_stripped(self):
        name = filename_for_subject("Re: Hello World")
        assert ":" not in name

    def test_slash_stripped(self):
        name = filename_for_subject("보고/공지 자료")
        assert "/" not in name

    def test_question_mark_stripped(self):
        name = filename_for_subject("확인?")
        assert "?" not in name

    def test_empty_subject_fallback(self):
        name = filename_for_subject("")
        assert name == "untitled.md"

    def test_deterministic(self):
        assert filename_for_subject("업무 보고") == filename_for_subject("업무 보고")

    def test_different_subjects_differ(self):
        assert filename_for_subject("제목A") != filename_for_subject("제목B")


# ── compose ──────────────────────────────────────────────────────────── #

class TestComposeFrontmatter:
    def _fm(self, text: str) -> str:
        """Extract the YAML frontmatter block (between the two --- lines)."""
        parts = text.split("---")
        assert len(parts) >= 3, "frontmatter delimiters not found"
        return parts[1]

    def test_has_frontmatter_delimiters(self):
        md = compose(_msg(), [], PROCESSED_AT)
        assert md.startswith("---\n")
        second = md.index("---\n", 4)
        assert second > 0

    def test_email_title_in_frontmatter(self):
        md = compose(_msg(subject="Weekly Report"), [], PROCESSED_AT)
        assert "email_title:" in self._fm(md)
        assert "Weekly Report" in self._fm(md)

    def test_subject_colon_quoted_in_email_title(self):
        """YAML colon in subject must be quoted so it's valid YAML."""
        md = compose(_msg(subject="Re: Hello World"), [], PROCESSED_AT)
        fm = self._fm(md)
        assert '"Re: Hello World"' in fm or "'Re: Hello World'" in fm

    def test_sender_in_frontmatter(self):
        md = compose(_msg(sender="bob@example.com"), [], PROCESSED_AT)
        assert "sender:" in self._fm(md)
        assert "bob@example.com" in self._fm(md)

    def test_date_in_frontmatter(self):
        """Date field contains the YYYY-MM-DD portion of processed_at."""
        md = compose(_msg(), [], PROCESSED_AT)
        assert "date:" in self._fm(md)
        assert "2024-01-15" in self._fm(md)

    def test_attachment_false_when_no_files(self):
        md = compose(_msg(), [], PROCESSED_AT)
        assert "attachment: false" in self._fm(md)

    def test_attachment_true_when_files_present(self):
        df = _df(web_view_link="https://drive.google.com/file/d/x/view")
        md = compose(_msg(), [df], PROCESSED_AT)
        assert "attachment: true" in self._fm(md)

    def test_tags_key_present(self):
        md = compose(_msg(), [], PROCESSED_AT)
        assert "tags:" in self._fm(md)

    def test_tags_contain_assignee_from_analysis(self):
        ar = _ar(assignees=["이기정"])
        md = compose(_msg(), [], PROCESSED_AT, analysis=ar)
        fm = self._fm(md)
        assert "#이기정" in fm

    def test_tags_contain_category_from_analysis(self):
        ar = _ar(category="보고")
        md = compose(_msg(), [], PROCESSED_AT, analysis=ar)
        fm = self._fm(md)
        assert "#보고" in fm

    def test_result_and_link_keys_present(self):
        md = compose(_msg(), [], PROCESSED_AT)
        fm = self._fm(md)
        assert "result:" in fm
        assert "link:" in fm

    def test_drive_url_not_in_frontmatter(self):
        """Drive URLs go in 첨부파일 링크 section, not frontmatter."""
        df = _df(web_view_link="https://drive.google.com/file/d/x/view")
        md = compose(_msg(), [df], PROCESSED_AT)
        fm = self._fm(md)
        assert "https://drive.google.com/file/d/x/view" not in fm


class TestComposeBody:
    def test_body_text_included(self):
        md = compose(_msg(body_text="Important content here."), [], PROCESSED_AT)
        assert "Important content here." in md

    def test_empty_body_text_allowed(self):
        md = compose(_msg(body_text=""), [], PROCESSED_AT)
        assert md  # should not crash or be empty

    def test_summary_section_present(self):
        md = compose(_msg(), [], PROCESSED_AT)
        assert "### 요약" in md

    def test_summary_content_when_provided(self):
        md = compose(_msg(), [], PROCESSED_AT, summary="- 핵심 내용")
        assert "### 요약" in md
        assert "- 핵심 내용" in md

    def test_summary_placeholder_when_empty(self):
        md = compose(_msg(), [], PROCESSED_AT, summary="")
        assert "_(요약 없음)_" in md

    def test_body_section_present(self):
        md = compose(_msg(), [], PROCESSED_AT)
        assert "### 본문" in md

    def test_body_text_in_body_section(self):
        md = compose(_msg(body_text="원문 내용입니다."), [], PROCESSED_AT)
        assert "원문 내용입니다." in md
        assert "<details>" not in md

    def test_attachment_section_present(self):
        md = compose(_msg(), [], PROCESSED_AT)
        assert "### 첨부파일 링크" in md

    def test_attachment_section_shows_none_when_empty(self):
        md = compose(_msg(), [], PROCESSED_AT)
        assert "_(없음)_" in md

    def test_attachment_section_lists_drive_files(self):
        df = _df(name="report.pdf", web_view_link="https://drive.google.com/r")
        md = compose(_msg(), [df], PROCESSED_AT)
        assert "report.pdf" in md
        assert "https://drive.google.com/r" in md

    def test_sections_in_correct_order(self):
        md = compose(_msg(), [], PROCESSED_AT)
        summary_pos = md.index("### 요약")
        body_pos = md.index("### 본문")
        att_pos = md.index("### 첨부파일 링크")
        assert summary_pos < body_pos < att_pos

    def test_no_details_tag_in_output(self):
        """New format never wraps content in <details> blocks."""
        md = compose(_msg(body_text="원문 내용입니다."), [], PROCESSED_AT, summary="- 요약")
        assert "<details>" not in md

    def test_no_legacy_summary_heading(self):
        """Old ## Summary heading is replaced by ### 요약."""
        md = compose(_msg(), [], PROCESSED_AT, summary="- 요약 내용")
        assert "## Summary" not in md

    def test_summary_before_body_before_attachments(self):
        md = compose(_msg(), [], PROCESSED_AT, summary="- 요약 내용")
        summary_pos = md.index("### 요약")
        body_pos = md.index("### 본문")
        att_pos = md.index("### 첨부파일 링크")
        assert summary_pos < body_pos < att_pos

    def test_sender_in_output(self):
        md = compose(_msg(sender="carol@example.com"), [], PROCESSED_AT)
        assert "carol@example.com" in md

    def test_multiple_drive_links_in_attachment_section(self):
        dfs = [
            _df("id1", "file1.pdf", "https://drive.google.com/1"),
            _df("id2", "file2.png", "https://drive.google.com/2"),
        ]
        md = compose(_msg(), dfs, PROCESSED_AT)
        assert "https://drive.google.com/1" in md
        assert "https://drive.google.com/2" in md

    def test_returns_string(self):
        assert isinstance(compose(_msg(), [], PROCESSED_AT), str)

    def test_account_email_param_accepted(self):
        """account_email parameter is accepted without error."""
        md = compose(_msg(), [], PROCESSED_AT, account_email="alice@example.com")
        assert md  # should not crash

    def test_analysis_none_produces_empty_tags(self):
        """When no analysis is passed, tags: is present but empty."""
        md = compose(_msg(), [], PROCESSED_AT, analysis=None)
        parts = md.split("---")
        fm = parts[1]
        assert "tags:" in fm
        # No #-tag content in frontmatter
        assert "#" not in fm
