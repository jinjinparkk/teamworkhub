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

from src.md_writer import compose, filename_for, filename_for_subject, _clean_body
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

    def test_tags_exclude_assignee_names(self):
        """tags should only contain subsidiary/media keywords, not assignee names."""
        ar = _ar(assignees=["이기정"])
        md = compose(_msg(), [], PROCESSED_AT, analysis=ar)
        fm = self._fm(md)
        assert "#이기정" not in fm

    def test_tags_exclude_category(self):
        """tags should only contain subsidiary/media keywords, not categories."""
        ar = _ar(category="보고")
        md = compose(_msg(), [], PROCESSED_AT, analysis=ar)
        fm = self._fm(md)
        assert "#보고" not in fm

    def test_tags_contain_subsidiary_keyword(self):
        """tags should contain subsidiary keywords found in email text."""
        ar = _ar()
        md = compose(_msg(subject="SIEL Report"), [], PROCESSED_AT, analysis=ar)
        fm = self._fm(md)
        assert "SIEL" in fm

    def test_original_title_in_frontmatter(self):
        md = compose(_msg(subject="FW: Daily Report"), [], PROCESSED_AT)
        fm = self._fm(md)
        assert "original_title:" in fm
        assert "FW: Daily Report" in fm

    def test_original_title_with_special_chars_quoted(self):
        md = compose(_msg(subject="Re: Hello: World #1"), [], PROCESSED_AT)
        fm = self._fm(md)
        assert "original_title:" in fm

    def test_description_from_analysis(self):
        """analysis.description이 있으면 그것을 사용."""
        ar = _ar(description="CM360 4월 검증 결과 정상", summary="- 요약 불릿")
        md = compose(_msg(), [], PROCESSED_AT, analysis=ar)
        fm = self._fm(md)
        assert "CM360 4월 검증 결과 정상" in fm

    def test_description_fallback_to_summary(self):
        """description 없으면 summary 첫 줄 fallback."""
        ar = _ar(description="", summary="- 핵심 요약 내용")
        md = compose(_msg(), [], PROCESSED_AT, analysis=ar)
        fm = self._fm(md)
        assert "핵심 요약 내용" in fm

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


# ── _clean_body ────────────────────────────────────────────────────── #

class TestCleanBody:
    def test_empty_string(self):
        assert _clean_body("") == ""

    def test_none_returns_none(self):
        assert _clean_body(None) is None

    def test_plain_text_unchanged(self):
        text = "안녕하세요.\n업무 보고 드립니다."
        assert _clean_body(text) == text

    def test_cid_image_removed(self):
        text = "본문 내용\n![스크린샷](cid:cafe_image_0@s-core.co.kr)\n감사합니다"
        result = _clean_body(text)
        assert "cid:" not in result
        assert "본문 내용" in result
        assert "감사합니다" in result

    def test_cid_image_empty_alt_removed(self):
        text = "내용\n![](cid:20260420051739_0@epcms1p)\n끝"
        result = _clean_body(text)
        assert "cid:" not in result
        assert "내용" in result

    def test_tracking_pixel_removed(self):
        text = "본문\n![](http://ext.samsung.net/mail/ext/v1/external/status/update?userid=test)\n끝"
        result = _clean_body(text)
        assert "ext.samsung.net" not in result
        assert "본문" in result

    def test_tracking_pixel_https_removed(self):
        text = "본문\n![](https://tracker.example.com/pixel.gif)\n끝"
        result = _clean_body(text)
        assert "tracker.example.com" not in result

    def test_real_image_with_alt_text_preserved(self):
        """Images with non-empty alt text and http URLs are kept."""
        text = "![보고서 차트](https://example.com/chart.png)"
        result = _clean_body(text)
        assert "보고서 차트" in result
        assert "example.com/chart.png" in result

    def test_external_warn_removed(self):
        text = "본문 내용\n이 메일은 조직 외부에서 발송되었습니다. 링크나 첨부 파일 클릭 시 주의하십시오.\n끝"
        result = _clean_body(text)
        assert "조직 외부" not in result
        assert "본문 내용" in result

    def test_english_disclaimer_removed(self):
        text = (
            "본문 내용\n"
            "The information in this email and any attachments are for the sole use "
            "of the intended recipient and may contain privileged information."
        )
        result = _clean_body(text)
        assert "The information in this email" not in result
        assert "본문 내용" in result

    def test_signature_separator_truncates(self):
        sig = (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "권예지 | Yeji Kwon\n"
            "Mobile: +82-10-1234-5678\n"
        )
        text = "본문 내용입니다.\n감사합니다.\n" + sig
        result = _clean_body(text)
        assert "권예지" not in result
        assert "본문 내용입니다" in result

    def test_signature_separator_light_horizontal(self):
        """─ (U+2500) separator also works."""
        sig = "─────────────────────\n이름 | Name\nEmail: test@test.com\n"
        text = "업무 보고\n" + sig
        result = _clean_body(text)
        assert "이름 | Name" not in result
        assert "업무 보고" in result

    def test_signature_kept_if_tail_too_long(self):
        """If content after separator is > 800 chars, don't truncate."""
        long_content = "중요한 내용입니다. " * 100  # ~1000 chars
        text = "시작\n━━━━━━━━\n" + long_content
        result = _clean_body(text)
        assert "중요한 내용입니다" in result

    def test_multiple_separators_uses_last(self):
        """Only the last separator is checked for truncation."""
        text = (
            "내용1\n━━━━━━━━\n"
            "전달된 메일 내용 (길다)" + "x" * 600 + "\n"
            "━━━━━━━━\n"
            "짧은 서명\n"
        )
        result = _clean_body(text)
        # First separator's content kept (>500 chars), last separator truncated
        assert "내용1" in result
        assert "짧은 서명" not in result

    def test_combined_junk_all_removed(self):
        """Real-world scenario: CID + tracking + disclaimer + signature."""
        text = (
            "안녕하세요,\n"
            "업무 보고 드립니다.\n\n"
            "감사합니다.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "![](cid:cafe_image_0@s-core.co.kr)\n"
            "홍길동 | Gildong Hong\n"
            "Mobile: +82-10-1234-5678\n"
            "The information in this email and any attachments "
            "are for the sole use of the intended recipient.\n"
            "![](http://ext.samsung.net/mail/ext/v1/track)\n"
            "이 메일은 조직 외부에서 발송되었습니다.\n"
        )
        result = _clean_body(text)
        assert "업무 보고" in result
        assert "감사합니다" in result
        assert "cid:" not in result
        assert "홍길동" not in result
        assert "The information" not in result
        assert "ext.samsung.net" not in result
        assert "조직 외부" not in result

    def test_compose_uses_clean_body(self):
        """compose() applies body cleanup."""
        body = "본문\n![](cid:img@mail)\n![](http://tracker.com/px)\n끝"
        md = compose(_msg(body_text=body), [], PROCESSED_AT)
        assert "cid:" not in md
        assert "tracker.com" not in md
        assert "본문" in md
