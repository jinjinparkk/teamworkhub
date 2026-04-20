"""Unit tests for archive_scanner module.

No real Drive/Gemini API calls — everything is mocked.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.archive_scanner import (
    parse_folder_name, scan_archive_folders, collect_archive_for_daily,
    ScanResult, _strip_yaml_frontmatter, _strip_forward_header, _yymmdd_to_iso,
)
from src.drive_client import DriveFile


# ── parse_folder_name ──────────────────────────────────────────────── #

class TestParseFolderName:
    def test_iso_format(self):
        result = parse_folder_name("2026-04-20_김치성_결재요청")
        assert result == ("2026-04-20", "김치성", "결재요청")

    def test_short_format_yymmdd(self):
        result = parse_folder_name("260420_김치성_결재요청")
        assert result == ("2026-04-20", "김치성", "결재요청")

    def test_short_format_different_date(self):
        result = parse_folder_name("260117_JohnDoe_Meeting")
        assert result == ("2026-01-17", "JohnDoe", "Meeting")

    def test_subject_with_underscores_iso(self):
        result = parse_folder_name("2026-04-21_이해랑_미팅_일정_확인")
        assert result == ("2026-04-21", "이해랑", "미팅_일정_확인")

    def test_subject_with_underscores_short(self):
        result = parse_folder_name("260421_이해랑_미팅_일정_확인")
        assert result == ("2026-04-21", "이해랑", "미팅_일정_확인")

    def test_invalid_format_no_date(self):
        assert parse_folder_name("김치성_결재요청") is None

    def test_invalid_format_no_underscore(self):
        assert parse_folder_name("2026-04-20김치성결재요청") is None

    def test_invalid_format_only_date_and_sender(self):
        assert parse_folder_name("2026-04-20_김치성") is None

    def test_invalid_short_only_date_and_sender(self):
        assert parse_folder_name("260420_김치성") is None

    def test_empty_string(self):
        assert parse_folder_name("") is None

    def test_date_sender_subject_english(self):
        result = parse_folder_name("2026-01-15_JohnDoe_MeetingNotes")
        assert result == ("2026-01-15", "JohnDoe", "MeetingNotes")


class TestYymmddToIso:
    def test_basic_conversion(self):
        assert _yymmdd_to_iso("260420") == "2026-04-20"

    def test_january(self):
        assert _yymmdd_to_iso("260101") == "2026-01-01"

    def test_year_25(self):
        assert _yymmdd_to_iso("250315") == "2025-03-15"


# ── scan_archive_folders — idempotency ─────────────────────────────── #

class TestScanIdempotency:
    def test_skips_existing_local_file(self, tmp_path):
        """If the local note already exists, the folder should be skipped."""
        # Pre-create the expected output file
        from src.md_writer import filename_for_subject
        local_name = filename_for_subject("2026-04-20 결재요청")
        (tmp_path / local_name).write_text("existing", encoding="utf-8")

        mock_drive = MagicMock()
        mock_drive.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "f1", "name": "2026-04-20_김치성_결재요청", "webViewLink": ""}],
        }

        with patch("src.archive_scanner.list_subfolders") as mock_ls:
            mock_ls.return_value = [
                {"id": "f1", "name": "2026-04-20_김치성_결재요청", "webViewLink": ""},
            ]
            result = scan_archive_folders(
                mock_drive, "archive-id", "", str(tmp_path), "test01"
            )

        assert result.skipped == 1
        assert result.processed == 0


# ── scan_archive_folders — missing 본문.md ─────────────────────────── #

class TestScanMissingBody:
    def test_error_when_no_body_md(self, tmp_path):
        """Folder without 본문.md should be counted as an error."""
        with patch("src.archive_scanner.list_subfolders") as mock_ls, \
             patch("src.archive_scanner.list_files_in_folder") as mock_lf:
            mock_ls.return_value = [
                {"id": "f1", "name": "2026-04-20_김치성_결재요청", "webViewLink": ""},
            ]
            # No files in the folder
            mock_lf.return_value = []

            result = scan_archive_folders(
                MagicMock(), "archive-id", "", str(tmp_path), "test02"
            )

        assert result.errors == 1
        assert result.processed == 0


# ── scan_archive_folders — successful processing ──────────────────── #

class TestScanSuccess:
    def test_processes_folder_and_creates_note(self, tmp_path):
        """A folder with 본문.md should produce a local Obsidian note."""
        body_file = DriveFile(
            file_id="body-id", name="본문.md", web_view_link="", created=False,
        )
        att_file = DriveFile(
            file_id="att-id", name="계약서.pdf",
            web_view_link="https://drive.google.com/계약서", created=False,
        )

        with patch("src.archive_scanner.list_subfolders") as mock_ls, \
             patch("src.archive_scanner.list_files_in_folder") as mock_lf, \
             patch("src.archive_scanner.download_file_content") as mock_dl, \
             patch("src.archive_scanner.analyze_email") as mock_analyze:

            # Top-level: one subfolder
            mock_ls.side_effect = [
                [{"id": "f1", "name": "2026-04-20_김치성_결재요청", "webViewLink": ""}],
                # Subfolders of f1: attachments folder
                [{"id": "att-folder", "name": "attachments", "webViewLink": ""}],
            ]
            # First call: files in f1 (본문.md), second: files in att-folder
            mock_lf.side_effect = [
                [body_file],
                [att_file],
            ]
            mock_dl.return_value = "이것은 테스트 본문입니다."
            from src.summarizer import AnalysisResult
            mock_analyze.return_value = AnalysisResult(
                summary="- 테스트 요약", assignees=["김치성"],
                priority="보통", category="승인요청", source="gemini",
            )

            result = scan_archive_folders(
                MagicMock(), "archive-id", "fake-key", str(tmp_path), "test03"
            )

        assert result.processed == 1
        assert result.errors == 0

        # Verify the note file was created
        from src.md_writer import filename_for_subject
        expected_name = filename_for_subject("2026-04-20 결재요청")
        note_path = tmp_path / expected_name
        assert note_path.exists()

        content = note_path.read_text(encoding="utf-8")
        assert "테스트 요약" in content
        assert "김치성" in content
        assert "계약서.pdf" in content


# ── scan_archive_folders — unrecognised folder skipped ────────────── #

class TestScanUnrecognisedFolder:
    def test_skips_bad_folder_name(self, tmp_path):
        with patch("src.archive_scanner.list_subfolders") as mock_ls:
            mock_ls.return_value = [
                {"id": "f1", "name": "random_folder", "webViewLink": ""},
            ]
            result = scan_archive_folders(
                MagicMock(), "archive-id", "", str(tmp_path), "test04"
            )

        assert result.skipped == 1
        assert result.processed == 0
        assert result.errors == 0


# ── scan_archive_folders — list_subfolders failure ────────────────── #

class TestScanListFailure:
    def test_error_when_list_subfolders_fails(self, tmp_path):
        with patch("src.archive_scanner.list_subfolders") as mock_ls:
            mock_ls.side_effect = Exception("Drive API error")
            result = scan_archive_folders(
                MagicMock(), "archive-id", "", str(tmp_path), "test05"
            )

        assert result.errors == 1
        assert result.processed == 0


# ── ScanResult defaults ──────────────────────────────────────────── #

class TestStripForwardHeader:
    def test_strips_forward_header(self):
        text = (
            '# 전달: RE:(3) TTD issue\n'
            '\n'
            '**\n'
            '\n'
            '\n'
            '보낸 사람:** 김치성 <chisung.kim@samsung.com>\n'
            '\n'
            '**보낸 날짜:** 2026년 4월 14일\n'
            '\n'
            '**받는 사람:** someone@example.com\n'
            '\n'
            '**참조:** cc@example.com\n'
            '\n'
            '**제목:** RE:(3) TTD issue\n'
            '\n'
            '\n'
            '송한비님 안녕하세요.\n'
            '업데이트 된 사항 있으실까요?\n'
        )
        result = _strip_forward_header(text)
        assert result.startswith("송한비님 안녕하세요.")
        assert "보낸 사람" not in result
        assert "# 전달" not in result

    def test_no_forward_header_unchanged(self):
        text = "안녕하세요.\n본문입니다."
        assert _strip_forward_header(text) == text

    def test_empty_string(self):
        assert _strip_forward_header("") == ""


class TestStripYamlFrontmatter:
    def test_removes_frontmatter(self):
        text = '---\nsubject: "hello"\nfrom: "a@b.com"\n---\n# Body content\nHello world'
        result = _strip_yaml_frontmatter(text)
        assert result == "# Body content\nHello world"

    def test_no_frontmatter_unchanged(self):
        text = "# Just a heading\nSome text"
        result = _strip_yaml_frontmatter(text)
        assert result == text

    def test_empty_string(self):
        assert _strip_yaml_frontmatter("") == ""

    def test_frontmatter_only(self):
        text = "---\nkey: value\n---\n"
        result = _strip_yaml_frontmatter(text)
        assert result == ""


class TestScanResult:
    def test_defaults(self):
        sr = ScanResult()
        assert sr.processed == 0
        assert sr.skipped == 0
        assert sr.errors == 0
        assert sr.details == []
