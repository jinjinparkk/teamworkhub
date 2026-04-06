"""Unit tests for drive_client — no real Drive API calls.

Covers:
  _safe_name_component  : unsafe chars, empty, length cap
  _safe_filename        : pattern, spaces, empty original
  find_file_by_name     : found / not found / multiple results
  get_or_create_folder  : existing folder / creates new
  upload_attachment     : new upload / idempotent skip / filename pattern / HttpError
  upsert_markdown       : create / update / correct fileId on update / HttpError
"""
from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest
from googleapiclient.errors import HttpError

from src.drive_client import (
    DriveFile,
    _safe_filename,
    _safe_name_component,
    find_file_by_name,
    get_or_create_folder,
    upload_attachment,
    upsert_markdown,
)


# ── Shared helpers ─────────────────────────────────────────────────── #

def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    return HttpError(resp=resp, content=b"error")


def _entry(file_id: str = "f1", name: str = "file.md",
           link: str = "https://drive.google.com/f1") -> dict:
    return {"id": file_id, "name": name, "webViewLink": link}


def _svc(
    list_files: list | None = None,
    create_file: dict | None = None,
    update_file: dict | None = None,
) -> MagicMock:
    """Return a mock Drive service with preset return values."""
    svc = MagicMock()
    svc.files.return_value.list.return_value.execute.return_value = {
        "files": list_files if list_files is not None else []
    }
    svc.files.return_value.create.return_value.execute.return_value = (
        create_file or _entry()
    )
    svc.files.return_value.update.return_value.execute.return_value = (
        update_file or _entry()
    )
    return svc


# ── _safe_name_component ───────────────────────────────────────────── #

class TestSafeNameComponent:
    def test_plain_name_unchanged(self):
        assert _safe_name_component("report.pdf") == "report.pdf"

    def test_spaces_replaced(self):
        result = _safe_name_component("my file.pdf")
        assert " " not in result
        assert result.endswith(".pdf")

    def test_forward_slash_replaced(self):
        assert "/" not in _safe_name_component("path/to/file.pdf")

    def test_backslash_replaced(self):
        assert "\\" not in _safe_name_component("path\\file.pdf")

    def test_colon_replaced(self):
        assert ":" not in _safe_name_component("note:2024.txt")

    def test_empty_string_becomes_attachment(self):
        assert _safe_name_component("") == "attachment"

    def test_only_unsafe_chars_becomes_attachment(self):
        assert _safe_name_component("???///") == "attachment"

    def test_max_length_enforced(self):
        result = _safe_name_component("a" * 300)
        assert len(result) <= 100

    def test_leading_dots_stripped(self):
        result = _safe_name_component("...hidden.pdf")
        assert not result.startswith(".")


# ── _safe_filename ─────────────────────────────────────────────────── #

class TestSafeFilename:
    def test_basic_pattern(self):
        name = _safe_filename("msg001", "report.pdf")
        assert name.startswith("msg001_")
        assert name.endswith(".pdf")

    def test_spaces_removed(self):
        assert " " not in _safe_filename("msg001", "my doc.pdf")

    def test_empty_original_uses_fallback(self):
        assert _safe_filename("msg001", "") == "msg001_attachment"

    def test_message_id_preserved_exactly(self):
        name = _safe_filename("AbCdEf123", "file.txt")
        assert name.startswith("AbCdEf123_")

    def test_two_calls_same_input_are_identical(self):
        """Determinism: identical input → identical output."""
        a = _safe_filename("msgX", "重要な文書.pdf")
        b = _safe_filename("msgX", "重要な文書.pdf")
        assert a == b


# ── find_file_by_name ─────────────────────────────────────────────── #

class TestFindFileByName:
    def test_found_returns_drive_file(self):
        e = _entry("f99", "note.md", "https://drive.google.com/f99")
        result = find_file_by_name(_svc(list_files=[e]), "note.md", "parent1")
        assert result is not None
        assert result.file_id == "f99"
        assert result.web_view_link == "https://drive.google.com/f99"

    def test_not_found_returns_none(self):
        result = find_file_by_name(_svc(list_files=[]), "missing.md", "parent1")
        assert result is None

    def test_created_is_false_for_existing(self):
        result = find_file_by_name(_svc(list_files=[_entry()]), "file.md", "p1")
        assert result.created is False

    def test_returns_first_when_multiple_match(self):
        entries = [_entry("first", "dup.md"), _entry("second", "dup.md")]
        result = find_file_by_name(_svc(list_files=entries), "dup.md", "p1")
        assert result.file_id == "first"

    def test_returns_drive_file_instance(self):
        result = find_file_by_name(_svc(list_files=[_entry()]), "f.md", "p1")
        assert isinstance(result, DriveFile)

    def test_http_error_propagates(self):
        svc = MagicMock()
        svc.files.return_value.list.return_value.execute.side_effect = _http_error(403)
        with pytest.raises(HttpError):
            find_file_by_name(svc, "file.md", "parent1")


# ── get_or_create_folder ──────────────────────────────────────────── #

class TestGetOrCreateFolder:
    def test_returns_existing_folder_id(self):
        svc = _svc(list_files=[{"id": "existing_folder"}])
        fid = get_or_create_folder(svc, "Notes", "root123")
        assert fid == "existing_folder"
        svc.files.return_value.create.assert_not_called()

    def test_creates_folder_when_absent(self):
        svc = _svc(list_files=[], create_file={"id": "new_folder"})
        fid = get_or_create_folder(svc, "Notes", "root123")
        assert fid == "new_folder"
        svc.files.return_value.create.assert_called_once()

    def test_create_body_has_folder_mime(self):
        svc = _svc(list_files=[], create_file={"id": "x"})
        get_or_create_folder(svc, "Attachments", "parent99")
        body = svc.files.return_value.create.call_args.kwargs["body"]
        assert body["mimeType"] == "application/vnd.google-apps.folder"

    def test_create_body_has_correct_name(self):
        svc = _svc(list_files=[], create_file={"id": "x"})
        get_or_create_folder(svc, "MyFolder", "parent99")
        body = svc.files.return_value.create.call_args.kwargs["body"]
        assert body["name"] == "MyFolder"


# ── upload_attachment ─────────────────────────────────────────────── #

class TestUploadAttachment:
    def test_creates_new_file_when_absent(self):
        svc = _svc(
            list_files=[],
            create_file=_entry("new_id", "msg1_report.pdf", "https://drive/new"),
        )
        result = upload_attachment(svc, "folder1", "msg1", "report.pdf",
                                   b"pdf bytes", "application/pdf")
        assert result.created is True
        assert result.file_id == "new_id"
        svc.files.return_value.create.assert_called_once()

    def test_skips_existing_file(self):
        existing = _entry("exist_id", "msg1_report.pdf", "https://drive/exist")
        svc = _svc(list_files=[existing])
        result = upload_attachment(svc, "folder1", "msg1", "report.pdf",
                                   b"pdf bytes", "application/pdf")
        assert result.created is False
        assert result.file_id == "exist_id"
        svc.files.return_value.create.assert_not_called()

    def test_filename_follows_safe_pattern(self):
        svc = _svc(list_files=[], create_file=_entry("x", "msg99_my_doc.pdf"))
        upload_attachment(svc, "f", "msg99", "my doc.pdf", b"data", "application/pdf")
        body = svc.files.return_value.create.call_args.kwargs["body"]
        assert body["name"].startswith("msg99_")
        assert " " not in body["name"]

    def test_parent_id_set_in_create_body(self):
        svc = _svc(list_files=[], create_file=_entry())
        upload_attachment(svc, "target_folder", "m1", "f.pdf", b"d", "application/pdf")
        body = svc.files.return_value.create.call_args.kwargs["body"]
        assert "target_folder" in body["parents"]

    def test_returns_drive_file_type(self):
        svc = _svc(list_files=[], create_file=_entry())
        result = upload_attachment(svc, "f", "m1", "file.pdf", b"d", "application/pdf")
        assert isinstance(result, DriveFile)

    def test_http_error_from_list_propagates(self):
        svc = MagicMock()
        svc.files.return_value.list.return_value.execute.side_effect = _http_error(403)
        with pytest.raises(HttpError):
            upload_attachment(svc, "f", "m1", "file.pdf", b"d", "application/pdf")

    def test_http_error_from_create_propagates(self):
        svc = _svc(list_files=[])   # file not found
        svc.files.return_value.create.return_value.execute.side_effect = _http_error(500)
        with pytest.raises(HttpError):
            upload_attachment(svc, "f", "m1", "file.pdf", b"d", "application/pdf")


# ── upsert_markdown ────────────────────────────────────────────────── #

class TestUpsertMarkdown:
    def test_creates_when_not_exists(self):
        svc = _svc(list_files=[], create_file=_entry("new_md", "twh_m1.md"))
        result = upsert_markdown(svc, "folder1", "twh_m1.md", "# Hello")
        assert result.created is True
        svc.files.return_value.create.assert_called_once()
        svc.files.return_value.update.assert_not_called()

    def test_updates_when_exists(self):
        existing = _entry("old_id", "twh_m1.md", "https://drive/old")
        svc = _svc(
            list_files=[existing],
            update_file=_entry("old_id", "twh_m1.md", "https://drive/old"),
        )
        result = upsert_markdown(svc, "folder1", "twh_m1.md", "# Updated")
        assert result.created is False
        svc.files.return_value.update.assert_called_once()
        svc.files.return_value.create.assert_not_called()

    def test_update_uses_correct_file_id(self):
        svc = _svc(
            list_files=[_entry("target_id", "twh_m1.md")],
            update_file=_entry("target_id", "twh_m1.md"),
        )
        upsert_markdown(svc, "folder1", "twh_m1.md", "content")
        kwargs = svc.files.return_value.update.call_args.kwargs
        assert kwargs["fileId"] == "target_id"

    def test_create_body_includes_parent_and_mime(self):
        svc = _svc(list_files=[], create_file=_entry())
        upsert_markdown(svc, "parent_folder", "note.md", "content")
        body = svc.files.return_value.create.call_args.kwargs["body"]
        assert "parent_folder" in body["parents"]
        assert body["mimeType"] == "text/markdown"

    def test_returns_drive_file_on_create(self):
        svc = _svc(list_files=[], create_file=_entry())
        assert isinstance(upsert_markdown(svc, "f", "note.md", "c"), DriveFile)

    def test_returns_drive_file_on_update(self):
        svc = _svc(
            list_files=[_entry("x", "note.md")],
            update_file=_entry("x", "note.md"),
        )
        assert isinstance(upsert_markdown(svc, "f", "note.md", "c"), DriveFile)

    def test_http_error_propagates(self):
        svc = MagicMock()
        svc.files.return_value.list.return_value.execute.side_effect = _http_error(500)
        with pytest.raises(HttpError):
            upsert_markdown(svc, "f", "note.md", "content")
