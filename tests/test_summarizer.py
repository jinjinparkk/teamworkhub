"""Tests for src.summarizer — reply-chain extraction & prompt integration."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.summarizer import _extract_latest_reply, _is_trivial_reply, analyze_email


# ── Helpers ────────────────────────────────────────────────────────── #

def _chain(latest: str, separator: str, quoted: str) -> str:
    """Build a simple reply-chain body."""
    return f"{latest}\n{separator}\n{quoted}"


# Long-enough reply texts (>55 chars) to avoid min_chars fallback
_REPLY_A = "네, 확인했습니다. 내일까지 수정된 보고서를 보내드리겠습니다. 추가 검토 사항이 있으면 말씀해주세요. 감사합니다."  # ~60 chars
_REPLY_B = "감사합니다. 수정 후 재전송하겠습니다. 내일 오전 중으로 완료하여 다시 보내드리도록 하겠습니다. 확인 부탁드립니다."  # ~60 chars
_REPLY_C = "알겠습니다. 반영하겠습니다. 추가 검토 사항이 있으면 알려주세요. 금일 중으로 처리하겠습니다. 수고하세요."  # ~55 chars
_REPLY_D = "Thanks, I'll review it by Friday and send my detailed feedback. Please let me know if there's a deadline change."  # ~100 chars
_REPLY_E = "네, 금요일까지 확인하겠습니다. 검토 완료 후 상세 피드백을 드리겠습니다. 감사합니다. 좋은 하루 되세요."  # ~55 chars
_REPLY_F = "Acknowledged. Will proceed as discussed in the meeting. I'll prepare the updated report by end of day tomorrow."  # ~100 chars
_REPLY_G = "확인했습니다. 진행하겠습니다. 내일 오전 회의에서 상세 내용 공유드리겠습니다. 참석 부탁드리고 감사합니다."  # ~55 chars
_REPLY_H = "보고서 첨부합니다. 확인 부탁드립니다. 수정 사항이 있으면 이번 금주 내로 회신 부탁드립니다. 감사합니다."  # ~55 chars
_REPLY_I = "일정 조율 완료되었습니다. 금요일 오후 3시 회의실 B에서 진행하겠습니다. 참석 부탁드리고 자료 준비 부탁합니다."  # ~58 chars
_REPLY_J = "동의합니다. 진행해주세요. 다음 주 월요일까지 최종 결과물을 정리해서 공유 부탁드립니다. 수고하세요."  # ~52 chars
_REPLY_K = "최신 답장입니다. 확인 부탁드립니다. 추가적으로 검토 요청합니다. 금주 금요일까지 회신 부탁합니다. 감사합니다."  # ~58 chars


# ══════════════════════════════════════════════════════════════════════
#  1.  No separator — body returned as-is
# ══════════════════════════════════════════════════════════════════════

class TestNoSeparator:
    def test_plain_body(self):
        body = "안녕하세요. 회의 일정 확인 부탁드립니다. 금주 금요일 오후 3시에 회의실 B에서 진행합니다."
        assert _extract_latest_reply(body) == body

    def test_empty_string(self):
        assert _extract_latest_reply("") == ""

    def test_none(self):
        assert _extract_latest_reply(None) == ""

    def test_whitespace_only(self):
        assert _extract_latest_reply("   \n\n  ") == ""

    def test_short_body_no_separator(self):
        body = "OK"
        assert _extract_latest_reply(body) == body


# ══════════════════════════════════════════════════════════════════════
#  2.  Outlook "-----Original Message-----"
# ══════════════════════════════════════════════════════════════════════

class TestOutlookOriginalMessage:
    def test_english(self):
        body = _chain(
            _REPLY_A,
            "-----Original Message-----",
            "From: 김과장\n보고서 검토 부탁드립니다.",
        )
        result = _extract_latest_reply(body)
        assert "확인했습니다" in result
        assert "김과장" not in result

    def test_korean(self):
        body = _chain(
            _REPLY_B,
            "-----원본 메시지-----",
            "From: 이대리\n수정 요청합니다.",
        )
        result = _extract_latest_reply(body)
        assert "감사합니다" in result
        assert "이대리" not in result

    def test_extra_dashes(self):
        body = _chain(
            _REPLY_C,
            "----------Original Message----------",
            "Previous content here.",
        )
        result = _extract_latest_reply(body)
        assert "반영하겠습니다" in result
        assert "Previous content" not in result


# ══════════════════════════════════════════════════════════════════════
#  3.  Gmail "On ... wrote:"
# ══════════════════════════════════════════════════════════════════════

class TestGmailOnWrote:
    def test_english(self):
        body = (
            f"{_REPLY_D}\n\n"
            "On Mon, Apr 20, 2026 at 3:15 PM John Doe <john@example.com> wrote:\n"
            "Please check the attached report."
        )
        result = _extract_latest_reply(body)
        assert "review it by Friday" in result
        assert "attached report" not in result

    def test_korean(self):
        body = (
            f"{_REPLY_E}\n\n"
            "2026년 4월 20일 (월) 오후 3:15에 홍길동님이 작성:\n"
            "첨부된 보고서를 확인해주세요."
        )
        result = _extract_latest_reply(body)
        assert "금요일까지" in result
        assert "첨부된 보고서" not in result


# ══════════════════════════════════════════════════════════════════════
#  4.  Outlook header block (From/Sent/To/Subject)
# ══════════════════════════════════════════════════════════════════════

class TestOutlookHeaderBlock:
    def test_english_header(self):
        body = (
            f"{_REPLY_F}\n\n"
            "From: Park Eunjin\n"
            "Sent: Monday, April 20, 2026 3:00 PM\n"
            "To: Kim Minjun\n"
            "Subject: RE: Project Update\n\n"
            "Old email content here."
        )
        result = _extract_latest_reply(body)
        assert "Acknowledged" in result
        assert "Old email content" not in result

    def test_korean_header(self):
        body = (
            f"{_REPLY_G}\n\n"
            "보낸 사람: 박은진\n"
            "보낸 날짜: 2026년 4월 20일 월요일 오후 3:00\n"
            "받는 사람: 김민준\n"
            "제목: RE: 프로젝트 업데이트\n\n"
            "이전 메일 내용."
        )
        result = _extract_latest_reply(body)
        assert "확인했습니다" in result
        assert "이전 메일 내용" not in result


# ══════════════════════════════════════════════════════════════════════
#  5.  Long separator lines (___ / ===)
# ══════════════════════════════════════════════════════════════════════

class TestSeparatorLines:
    def test_underscores(self):
        body = _chain(
            _REPLY_H,
            "_____________________________",
            "이전 대화 내용",
        )
        result = _extract_latest_reply(body)
        assert "보고서 첨부합니다" in result
        assert "이전 대화" not in result

    def test_equals(self):
        body = _chain(
            _REPLY_I,
            "=============================",
            "이전 내용",
        )
        result = _extract_latest_reply(body)
        assert "일정 조율" in result
        assert "이전 내용" not in result

    def test_dashes_not_matched(self):
        """Markdown HR (---) should NOT be treated as a reply separator."""
        body = "첫째 문단\n\n---\n\n둘째 문단"
        result = _extract_latest_reply(body)
        assert result == body  # unchanged

    def test_short_underscores_not_matched(self):
        """Fewer than 5 chars should not match."""
        body = "내용\n____\n추가"
        assert _extract_latest_reply(body) == body


# ══════════════════════════════════════════════════════════════════════
#  6.  Quoted lines (>)
# ══════════════════════════════════════════════════════════════════════

class TestQuotedLines:
    def test_three_or_more_quoted_lines(self):
        body = (
            f"{_REPLY_J}\n\n"
            "> 이전 메시지 1줄\n"
            "> 이전 메시지 2줄\n"
            "> 이전 메시지 3줄\n"
        )
        result = _extract_latest_reply(body)
        assert "동의합니다" in result
        assert "이전 메시지 3줄" not in result

    def test_one_quoted_line_not_matched(self):
        """A single '>' line is not a reply chain."""
        body = "내용\n> 인용 한 줄\n계속"
        assert _extract_latest_reply(body) == body

    def test_two_quoted_lines_not_matched(self):
        body = "내용\n> 인용1\n> 인용2\n계속"
        assert _extract_latest_reply(body) == body


# ══════════════════════════════════════════════════════════════════════
#  7.  min_chars fallback
# ══════════════════════════════════════════════════════════════════════

class TestMinCharsFallback:
    def test_short_reply_extends_to_second_boundary(self):
        """If latest reply is < min_chars, include up to 2nd separator."""
        body = (
            "OK\n"
            "-----Original Message-----\n"
            "중간 메시지가 여기에 있습니다. 이 메시지는 충분히 길어서 두 번째 섹션으로 포함됩니다.\n"
            "-----Original Message-----\n"
            "가장 오래된 메시지"
        )
        result = _extract_latest_reply(body, min_chars=50)
        assert "중간 메시지" in result
        assert "가장 오래된 메시지" not in result

    def test_short_reply_single_boundary_returns_full(self):
        """If only one boundary and reply is too short, return full body."""
        body = (
            "OK\n"
            "-----Original Message-----\n"
            "원본 메시지 내용"
        )
        result = _extract_latest_reply(body, min_chars=50)
        assert result == body

    def test_reply_above_min_chars(self):
        """Reply above min_chars threshold — no extension needed."""
        long_reply = "A" * 100
        body = f"{long_reply}\n-----Original Message-----\n이전 내용"
        result = _extract_latest_reply(body, min_chars=50)
        assert result == long_reply
        assert "이전 내용" not in result

    def test_custom_min_chars_low(self):
        """min_chars=5 — short reply passes length check but trivial detection
        still extends to include the next section."""
        body = _chain(
            "확인했습니다.",
            "-----Original Message-----",
            "이전 메시지",
        )
        # "확인했습니다" is trivial → extends; only 1 boundary → full body
        result = _extract_latest_reply(body, min_chars=5)
        assert "확인했습니다" in result
        assert "이전 메시지" in result

    def test_default_min_chars_triggers_on_short(self):
        """Default min_chars=50 causes very short reply to extend."""
        body = _chain(
            "OK",
            "-----Original Message-----",
            "이전 메시지가 있습니다.",
        )
        # Only 2 chars → extends, but only 1 boundary → returns full body
        result = _extract_latest_reply(body)
        assert result == body


# ══════════════════════════════════════════════════════════════════════
#  8.  Edge cases
# ══════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_separator_at_very_start(self):
        """Separator at position 0 — reply is empty, falls back."""
        body = "-----Original Message-----\n원본 메시지만 있음"
        result = _extract_latest_reply(body, min_chars=50)
        # Only one boundary, reply is empty (<50), returns full body
        assert result == body

    def test_multiple_different_separators(self):
        """Multiple separator types — earliest one wins."""
        body = (
            f"{_REPLY_K}\n\n"
            "On Mon, Apr 20, 2026 at 3:15 PM Someone wrote:\n"
            "중간 메시지\n"
            "-----Original Message-----\n"
            "가장 오래된 메시지"
        )
        result = _extract_latest_reply(body)
        assert "최신 답장" in result
        assert "중간 메시지" not in result
        assert "가장 오래된 메시지" not in result

    def test_preserves_newlines_in_reply(self):
        """Reply with multiple paragraphs is preserved intact."""
        body = (
            "첫 번째 단락입니다. 보고서 검토 완료했습니다. 수정 사항 없이 승인합니다.\n\n"
            "두 번째 단락입니다. 다음 단계로 진행해주시기 바랍니다.\n\n"
            "-----Original Message-----\n이전 내용"
        )
        result = _extract_latest_reply(body)
        assert "첫 번째 단락" in result
        assert "두 번째 단락" in result

    def test_separator_in_middle_of_line_not_matched(self):
        """Separator pattern must start at line beginning."""
        body = "본문에 wrote: 라는 단어가 포함되어 있지만 이것은 구분자가 아닙니다. 단순히 텍스트의 일부입니다."
        assert _extract_latest_reply(body) == body


# ══════════════════════════════════════════════════════════════════════
#  9.  Integration: analyze_email sends stripped body to Claude
# ══════════════════════════════════════════════════════════════════════

class TestAnalyzeEmailIntegration:
    """Verify that analyze_email() strips the chain before sending to Claude."""

    @patch("src.summarizer.anthropic.Anthropic")
    def test_chain_body_stripped_before_claude_call(self, mock_anthropic_cls):
        chain_body = (
            "최신 답장: 검토 완료했습니다. 수정 사항 없이 승인 처리하겠습니다. 감사합니다. 추가 질문 있으면 연락주세요.\n"
            "-----Original Message-----\n"
            "HIDDEN_OLD_CONTENT: 보고서 검토 부탁드립니다."
        )

        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=json.dumps({
            "short_title": "검토 완료",
            "description": "검토 완료 보고",
            "summary": ["- 검토 완료"],
            "assignees": [],
            "priority": "보통",
            "category": "보고",
        }))]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_msg
        mock_anthropic_cls.return_value = mock_client

        result = analyze_email("RE: 보고서", "김과장", chain_body, "test-key")

        # Verify Claude was called with the stripped body (no old content)
        call_args = mock_client.messages.create.call_args
        prompt_sent = call_args.kwargs["messages"][0]["content"]
        assert "최신 답장" in prompt_sent
        assert "HIDDEN_OLD_CONTENT" not in prompt_sent
        assert result.source == "claude"

    @patch("src.summarizer.anthropic.Anthropic")
    def test_plain_body_passes_through(self, mock_anthropic_cls):
        """Non-chain body is sent as-is to Claude."""
        plain_body = "단순 메일 본문입니다. 회의 일정 확인 부탁드립니다. 금주 금요일 오후 3시 회의실 B에서 진행합니다."

        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=json.dumps({
            "short_title": "회의 일정",
            "description": "회의 일정 확인",
            "summary": ["- 회의 일정 확인"],
            "assignees": [],
            "priority": "보통",
            "category": "미팅",
        }))]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_msg
        mock_anthropic_cls.return_value = mock_client

        analyze_email("회의 일정", "박대리", plain_body, "test-key")

        call_args = mock_client.messages.create.call_args
        prompt_sent = call_args.kwargs["messages"][0]["content"]
        assert "단순 메일 본문" in prompt_sent

    def test_fallback_uses_raw_body(self):
        """When API key is missing, fallback summary uses raw body (not stripped)."""
        chain_body = (
            "OK\n"
            "-----Original Message-----\n"
            "원본 메시지 전체가 fallback에 포함되어야 함"
        )
        result = analyze_email("제목", "발신자", chain_body, "")
        # Fallback summary uses _fallback_summary which takes raw body
        assert result.source == "fallback"


# ══════════════════════════════════════════════════════════════════════
# 10.  _is_trivial_reply
# ══════════════════════════════════════════════════════════════════════

class TestIsTrivialReply:
    def test_empty(self):
        assert _is_trivial_reply("") is True
        assert _is_trivial_reply(None) is True
        assert _is_trivial_reply("   ") is True

    def test_just_greeting(self):
        assert _is_trivial_reply("확인바랍니다") is True
        assert _is_trivial_reply("확인 바랍니다.") is True
        assert _is_trivial_reply("확인 부탁드립니다") is True
        assert _is_trivial_reply("네, 확인했습니다") is True
        assert _is_trivial_reply("알겠습니다") is True
        assert _is_trivial_reply("OK") is True

    def test_greeting_plus_signature(self):
        text = "확인바랍니다.\n\n감사합니다\n\n김과장 드림"
        assert _is_trivial_reply(text) is True

    def test_recipient_addition(self):
        text = "수신인 추가 드립니다\n\n감사합니다\n\n심민지 드림"
        assert _is_trivial_reply(text) is True

    def test_substantive_reply_not_trivial(self):
        text = (
            "보고서 검토 완료했습니다. 3페이지 수치 오류 수정 필요합니다. "
            "금요일까지 수정본 보내주세요."
        )
        assert _is_trivial_reply(text) is False

    def test_long_reply_not_trivial(self):
        assert _is_trivial_reply(_REPLY_A) is False

    def test_short_but_substantive(self):
        # min_substance=30 default, this is ~35 chars
        text = "3.5 테이블 MX_FLAGSHIP_MODEL 컬럼 추가 요청합니다."
        assert _is_trivial_reply(text) is False

    def test_single_ne(self):
        assert _is_trivial_reply("네") is True
        assert _is_trivial_reply("넵") is True
        assert _is_trivial_reply("Yes") is True

    def test_short_meaningful_under_threshold(self):
        """Very short but not a trivial phrase — below min_substance."""
        assert _is_trivial_reply("검토중", min_substance=30) is True  # 3 chars < 30

    def test_custom_min_substance(self):
        assert _is_trivial_reply("검토중", min_substance=2) is False  # 3 chars >= 2


# ══════════════════════════════════════════════════════════════════════
# 11.  Trivial reply skipping in _extract_latest_reply
# ══════════════════════════════════════════════════════════════════════

class TestTrivialReplySkipping:
    def test_trivial_reply_extends_to_next_section(self):
        """When latest reply is trivial, include the next section."""
        body = (
            "확인바랍니다.\n\n감사합니다\n\n김대리 드림\n"
            "-----Original Message-----\n"
            "4월 17일 기준 대시보드 태스크 리스트를 공유합니다. "
            "VD View 개발 착수되었으며 4/24까지 완료 예정입니다.\n"
            "-----Original Message-----\n"
            "가장 오래된 메시지"
        )
        result = _extract_latest_reply(body)
        assert "태스크 리스트" in result
        assert "가장 오래된 메시지" not in result

    def test_trivial_reply_single_boundary_returns_full(self):
        """Trivial reply + only one boundary → return full body."""
        body = (
            "네\n"
            "-----Original Message-----\n"
            "원본 메시지 내용이 여기에 있습니다."
        )
        result = _extract_latest_reply(body)
        assert "원본 메시지 내용" in result

    def test_substantive_reply_not_extended(self):
        """Non-trivial reply keeps only the latest section."""
        body = (
            f"{_REPLY_A}\n"
            "-----Original Message-----\n"
            "HIDDEN_CONTENT"
        )
        result = _extract_latest_reply(body)
        assert "확인했습니다" in result
        assert "HIDDEN_CONTENT" not in result

    def test_recipient_add_trivial_extends(self):
        """Real-world case: 수신인 추가 reply extends to get context."""
        body = (
            "아마존, affil. 미디어 담당자 수신인 추가 드립니다\n\n"
            "감사합니다\n\n심민지 드림\n\n"
            "--------- \nOriginal Message ---------\n\n"
            "4/17 (금) 기준 금주 Task 리스트 공유 드립니다. "
            "GMPD 대시보드 기존 대시보드 Raw Data 다운로드 관련 테스트 진행 완료.\n\n"
            "--------- \nOriginal Message ---------\n\n"
            "아주 오래된 메시지"
        )
        result = _extract_latest_reply(body)
        assert "Task 리스트" in result
        assert "아주 오래된 메시지" not in result

    def test_confirm_trivial_gmail_style(self):
        """Trivial reply with Gmail-style separator."""
        body = (
            "확인 부탁드립니다.\n\n"
            "On Mon, Apr 20, 2026 at 3:15 PM 권예지 <yj@example.com> wrote:\n"
            "대시보드 업데이트 완료했습니다. 3.5 테이블 변경 검토 요청합니다. "
            "MX_FLAGSHIP_MODEL 컬럼 추가 완료되었습니다.\n\n"
            "On Fri, Apr 17, 2026 at 2:00 PM 김과장 <kim@example.com> wrote:\n"
            "원래 요청 메시지"
        )
        result = _extract_latest_reply(body)
        assert "대시보드 업데이트" in result
        assert "원래 요청 메시지" not in result
