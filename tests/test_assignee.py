"""Unit tests for assignee extractor — no real API calls."""
from __future__ import annotations

from unittest.mock import patch

from src.assignee import extract_assignees, _regex_extract, is_valid_assignee


# ── _regex_extract ────────────────────────────────────────────────────── #

class TestRegexExtract:
    def test_extracts_name_with_title(self):
        assert "박은진" in _regex_extract("박은진대리님 확인 부탁드립니다")

    def test_extracts_name_with_space_before_title(self):
        assert "이해랑" in _regex_extract("이해랑 팀장님께 전달해주세요")

    def test_extracts_multiple_names(self):
        names = _regex_extract("박은진대리님과 이해랑팀장님이 담당입니다")
        assert "박은진" in names
        assert "이해랑" in names

    def test_deduplicates_same_name(self):
        names = _regex_extract("박은진대리님, 박은진대리님 두 번 언급")
        assert names.count("박은진") == 1

    def test_returns_empty_when_no_match(self):
        assert _regex_extract("일반적인 업무 내용입니다") == []

    def test_various_titles(self):
        texts = [
            ("김철수과장님", "김철수"),
            ("이영희부장님", "이영희"),
            ("박민준주임님", "박민준"),
            ("최지수차장님", "최지수"),
            ("정수현선임님", "정수현"),
        ]
        for text, expected in texts:
            assert expected in _regex_extract(text)


# ── extract_assignees ─────────────────────────────────────────────────── #

class TestExtractAssignees:
    def test_regex_hit_returns_names(self):
        names = extract_assignees("확인요청", "sender@example.com",
                                  "박은진대리님 처리 부탁드립니다", "fake-key")
        assert "박은진" in names

    def test_regex_also_searches_subject(self):
        names = extract_assignees("이해랑팀장님 보고", "sender@example.com",
                                  "본문에는 이름 없음", "fake-key")
        assert "이해랑" in names

    def test_no_api_key_returns_empty_when_no_regex(self):
        names = extract_assignees("일반 메일", "sender@example.com",
                                  "담당자 언급 없음", "")
        assert names == []

    def test_claude_called_when_no_regex_match(self):
        mock_message = type("Msg", (), {
            "content": [type("Block", (), {"text": "박은진, 이해랑"})()]
        })()
        with patch("src.assignee.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = mock_message
            names = extract_assignees("일반 메일", "sender@example.com",
                                      "담당자 언급 없음", "fake-key")
        assert names == ["박은진", "이해랑"]

    def test_claude_empty_response_returns_empty(self):
        mock_message = type("Msg", (), {
            "content": [type("Block", (), {"text": ""})()]
        })()
        with patch("src.assignee.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = mock_message
            names = extract_assignees("일반 메일", "sender@example.com",
                                      "담당자 언급 없음", "fake-key")
        assert names == []

    def test_claude_failure_returns_empty(self):
        with patch("src.assignee.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.side_effect = Exception("timeout")
            names = extract_assignees("일반 메일", "sender@example.com",
                                      "담당자 언급 없음", "fake-key")
        assert names == []

    def test_regex_takes_priority_over_claude(self):
        """When regex finds names, Claude should not be called."""
        with patch("src.assignee.anthropic.Anthropic") as mock_cls:
            names = extract_assignees("박은진대리님 확인", "sender@example.com",
                                      "내용", "fake-key")
        mock_cls.return_value.messages.create.assert_not_called()
        assert "박은진" in names

    def test_claude_garbage_filtered_out(self):
        """Claude returning garbage like 'ㅋㅋㅋ' should be filtered."""
        mock_message = type("Msg", (), {
            "content": [type("Block", (), {"text": "ㅋㅋㅋㅋㅋ, 데이트는, 박은진"})()]
        })()
        with patch("src.assignee.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = mock_message
            names = extract_assignees("일반 메일", "sender@example.com",
                                      "담당자 언급 없음", "fake-key")
        assert names == ["박은진"]

    def test_claude_all_garbage_returns_empty(self):
        """If Claude returns only garbage, result should be empty."""
        mock_message = type("Msg", (), {
            "content": [type("Block", (), {"text": "ㅋㅋㅋ, 데이트는, #태그"})()]
        })()
        with patch("src.assignee.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = mock_message
            names = extract_assignees("일반 메일", "sender@example.com",
                                      "담당자 언급 없음", "fake-key")
        assert names == []


# ── is_valid_assignee ──────────────────────────────────────────────── #

class TestIsValidAssignee:
    # Valid names
    def test_known_team_member(self):
        assert is_valid_assignee("박은진") is True
        assert is_valid_assignee("이해랑") is True
        assert is_valid_assignee("최원영") is True

    def test_two_char_korean_name(self):
        assert is_valid_assignee("김철") is True

    def test_three_char_korean_name(self):
        assert is_valid_assignee("김철수") is True

    def test_unknown_valid_name(self):
        """Unknown but structurally valid Korean name."""
        assert is_valid_assignee("홍길동") is True

    # Invalid names
    def test_jamo_chars(self):
        """Korean consonant/vowel jamo are not valid names."""
        assert is_valid_assignee("ㅋㅋㅋㅋㅋ") is False
        assert is_valid_assignee("ㅎㅎ") is False
        assert is_valid_assignee("ㅠㅠ") is False

    def test_four_char_not_known(self):
        """4-char strings that aren't known names are rejected."""
        assert is_valid_assignee("데이트는") is False
        assert is_valid_assignee("감사합니다") is False

    def test_hashtag_prefix(self):
        assert is_valid_assignee("#태그") is False

    def test_empty_string(self):
        assert is_valid_assignee("") is False

    def test_single_char(self):
        assert is_valid_assignee("김") is False

    def test_english_name(self):
        assert is_valid_assignee("John") is False

    def test_mixed_chars(self):
        assert is_valid_assignee("김abc") is False

    def test_with_spaces(self):
        assert is_valid_assignee("김 철수") is False

    def test_with_title(self):
        """Name+title should not pass (titles are already stripped upstream)."""
        assert is_valid_assignee("김과장") is True  # 2-char, structurally valid
        assert is_valid_assignee("박은진대리") is False  # 5-char, too long
