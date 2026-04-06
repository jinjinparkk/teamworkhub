"""Unit tests for assignee extractor — no real API calls."""
from __future__ import annotations

from unittest.mock import patch

from src.assignee import extract_assignees, _regex_extract


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

    def test_gemini_called_when_no_regex_match(self):
        mock_resp = {
            "candidates": [{"content": {"parts": [{"text": "박은진, 이해랑"}]}}]
        }
        with patch("src.assignee.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = mock_resp
            mock_post.return_value.raise_for_status = lambda: None
            names = extract_assignees("일반 메일", "sender@example.com",
                                      "담당자 언급 없음", "fake-key")
        assert names == ["박은진", "이해랑"]

    def test_gemini_empty_response_returns_empty(self):
        mock_resp = {
            "candidates": [{"content": {"parts": [{"text": ""}]}}]
        }
        with patch("src.assignee.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = mock_resp
            mock_post.return_value.raise_for_status = lambda: None
            names = extract_assignees("일반 메일", "sender@example.com",
                                      "담당자 언급 없음", "fake-key")
        assert names == []

    def test_gemini_failure_returns_empty(self):
        with patch("src.assignee.requests.post", side_effect=Exception("timeout")):
            names = extract_assignees("일반 메일", "sender@example.com",
                                      "담당자 언급 없음", "fake-key")
        assert names == []

    def test_regex_takes_priority_over_gemini(self):
        """When regex finds names, Gemini should not be called."""
        with patch("src.assignee.requests.post") as mock_post:
            names = extract_assignees("박은진대리님 확인", "sender@example.com",
                                      "내용", "fake-key")
        mock_post.assert_not_called()
        assert "박은진" in names
