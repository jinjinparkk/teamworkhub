"""Unit tests for gmail_client — no real API calls.

Covers:
  - _b64decode         : encoding edge cases
  - _extract_body      : plain, html, multipart variants
  - _extract_attachments: zero / one / nested attachments
  - _parse_date        : valid RFC 2822 + fallback
  - list_messages      : single page, pagination, max_results cap, empty label
  - fetch_message      : field mapping, subject log truncation, attachment list
  - download_attachment: bytes returned, HttpError propagation
"""
from __future__ import annotations

import base64
from datetime import timezone
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from src.gmail_client import (
    Attachment,
    ParsedMessage,
    _b64decode,
    _extract_attachments,
    _extract_body,
    _parse_date,
    download_attachment,
    fetch_message,
    list_messages,
)


# ── Helpers ────────────────────────────────────────────────────────── #

def _b64(text: str) -> str:
    """Base64url-encode text without padding (as Gmail sends it)."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _b64_bytes(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _plain_payload(text: str) -> dict:
    return {
        "mimeType": "text/plain",
        "body": {"data": _b64(text)},
        "parts": [],
    }


def _html_payload(html: str) -> dict:
    return {
        "mimeType": "text/html",
        "body": {"data": _b64(html)},
        "parts": [],
    }


def _raw_message(
    msg_id: str = "m001",
    thread_id: str = "t001",
    subject: str = "Hello",
    sender: str = "alice@example.com",
    date: str = "Mon, 15 Jan 2024 10:30:00 +0000",
    body: str = "Hi there.",
) -> dict:
    return {
        "id": msg_id,
        "threadId": thread_id,
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": date},
                {"name": "Message-ID", "value": f"<{msg_id}@mail.example.com>"},
            ],
            "body": {"data": _b64(body)},
            "parts": [],
        },
    }


def _make_list_service(pages: list[list[dict]]) -> MagicMock:
    """Build a mock service for list_messages with one or more result pages."""
    svc = MagicMock()

    first_resp: dict = {"messages": pages[0]}
    if len(pages) > 1:
        first_resp["nextPageToken"] = "page2"

    svc.users.return_value.messages.return_value.list.return_value.execute.return_value = first_resp

    if len(pages) > 1:
        subsequent: list = []
        for i, page in enumerate(pages[1:], start=2):
            req = MagicMock()
            resp: dict = {"messages": page}
            if i < len(pages):
                resp["nextPageToken"] = f"page{i + 1}"
            req.execute.return_value = resp
            subsequent.append(req)
        subsequent.append(None)  # signals end of pages to the loop
        svc.users.return_value.messages.return_value.list_next.side_effect = subsequent
    else:
        svc.users.return_value.messages.return_value.list_next.return_value = None

    return svc


def _make_fetch_service(raw: dict) -> MagicMock:
    svc = MagicMock()
    svc.users.return_value.messages.return_value.get.return_value.execute.return_value = raw
    return svc


def _make_attachment_service(data_b64: str) -> MagicMock:
    svc = MagicMock()
    svc.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
        "data": data_b64
    }
    return svc


def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    return HttpError(resp=resp, content=b"error")


# ── _b64decode ─────────────────────────────────────────────────────── #

class TestB64Decode:
    def test_ascii_round_trip(self):
        assert _b64decode(_b64("hello")) == b"hello"

    def test_unicode_round_trip(self):
        assert _b64decode(_b64("안녕하세요")) == "안녕하세요".encode()

    def test_binary_round_trip(self):
        data = bytes(range(256))
        assert _b64decode(_b64_bytes(data)) == data

    def test_padding_variants(self):
        # Lengths 1–4 exercise all padding cases.
        for n in range(1, 5):
            text = "A" * n
            assert _b64decode(_b64(text)) == text.encode()

    def test_empty_string(self):
        assert _b64decode("") == b""


# ── _extract_body ──────────────────────────────────────────────────── #

class TestExtractBody:
    def test_plain_text(self):
        assert _extract_body(_plain_payload("Hello plain")) == "Hello plain"

    def test_html_converted_to_text(self):
        result = _extract_body(_html_payload("<p>Hello <b>world</b></p>"))
        assert "Hello" in result
        assert "world" in result
        assert "<p>" not in result

    def test_empty_body(self):
        assert _extract_body({"mimeType": "text/plain", "body": {}, "parts": []}) == ""

    def test_multipart_alternative_prefers_plain(self):
        payload = {
            "mimeType": "multipart/alternative",
            "body": {},
            "parts": [
                _plain_payload("plain content"),
                _html_payload("<b>html content</b>"),
            ],
        }
        assert _extract_body(payload) == "plain content"

    def test_multipart_alternative_falls_back_to_html(self):
        payload = {
            "mimeType": "multipart/alternative",
            "body": {},
            "parts": [_html_payload("<p>only html</p>")],
        }
        result = _extract_body(payload)
        assert "only html" in result

    def test_multipart_mixed(self):
        payload = {
            "mimeType": "multipart/mixed",
            "body": {},
            "parts": [
                _plain_payload("body text"),
                {   # attachment part — should be ignored for body
                    "mimeType": "application/pdf",
                    "filename": "doc.pdf",
                    "body": {"attachmentId": "att1", "size": 100},
                    "parts": [],
                },
            ],
        }
        assert _extract_body(payload) == "body text"

    def test_nested_multipart(self):
        inner = {
            "mimeType": "multipart/alternative",
            "body": {},
            "parts": [_plain_payload("nested plain")],
        }
        outer = {"mimeType": "multipart/mixed", "body": {}, "parts": [inner]}
        assert _extract_body(outer) == "nested plain"

    def test_unknown_mime_returns_empty(self):
        payload = {"mimeType": "application/pdf", "body": {}, "parts": []}
        assert _extract_body(payload) == ""


# ── _extract_attachments ───────────────────────────────────────────── #

class TestExtractAttachments:
    def test_no_attachments(self):
        assert _extract_attachments(_plain_payload("body")) == []

    def test_single_attachment(self):
        payload = {
            "mimeType": "multipart/mixed",
            "body": {},
            "parts": [
                _plain_payload("body"),
                {
                    "mimeType": "application/pdf",
                    "filename": "report.pdf",
                    "body": {"attachmentId": "att123", "size": 2048},
                    "parts": [],
                },
            ],
        }
        atts = _extract_attachments(payload)
        assert len(atts) == 1
        assert atts[0].filename == "report.pdf"
        assert atts[0].attachment_id == "att123"
        assert atts[0].size == 2048

    def test_multiple_attachments(self):
        payload = {
            "mimeType": "multipart/mixed",
            "body": {},
            "parts": [
                _plain_payload("body"),
                {"mimeType": "image/png", "filename": "img.png",
                 "body": {"attachmentId": "a1", "size": 100}, "parts": []},
                {"mimeType": "application/zip", "filename": "data.zip",
                 "body": {"attachmentId": "a2", "size": 200}, "parts": []},
            ],
        }
        atts = _extract_attachments(payload)
        assert len(atts) == 2
        filenames = {a.filename for a in atts}
        assert filenames == {"img.png", "data.zip"}

    def test_nested_attachment(self):
        inner = {
            "mimeType": "multipart/mixed",
            "body": {},
            "parts": [
                {"mimeType": "application/pdf", "filename": "nested.pdf",
                 "body": {"attachmentId": "n1", "size": 10}, "parts": []},
            ],
        }
        payload = {"mimeType": "multipart/mixed", "body": {}, "parts": [inner]}
        atts = _extract_attachments(payload)
        assert len(atts) == 1
        assert atts[0].filename == "nested.pdf"

    def test_part_without_filename_excluded(self):
        """Inline content references (no filename) must not appear as attachments."""
        payload = {
            "mimeType": "multipart/mixed",
            "body": {},
            "parts": [
                {"mimeType": "text/plain", "filename": "",
                 "body": {"data": _b64("body")}, "parts": []},
            ],
        }
        assert _extract_attachments(payload) == []


# ── _parse_date ────────────────────────────────────────────────────── #

class TestParseDate:
    def test_valid_rfc2822(self):
        result = _parse_date("Mon, 15 Jan 2024 10:30:00 +0000")
        assert "2024-01-15" in result
        assert "+00:00" in result or "Z" in result

    def test_result_is_utc(self):
        result = _parse_date("Mon, 15 Jan 2024 10:30:00 +0900")
        # +0900 → UTC is 01:30
        assert "01:30" in result

    def test_empty_string_fallback(self):
        result = _parse_date("")
        # Should return something ISO-8601-ish, not crash.
        assert "T" in result

    def test_garbage_fallback(self):
        result = _parse_date("not a date at all !!!")
        assert "T" in result


# ── list_messages ──────────────────────────────────────────────────── #

class TestListMessages:
    def test_single_page(self):
        stubs = [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t1"}]
        svc = _make_list_service([stubs])
        result = list_messages(svc, "INBOX", max_results=50)
        assert result == stubs

    def test_two_page_pagination(self):
        page1 = [{"id": "m1"}, {"id": "m2"}]
        page2 = [{"id": "m3"}]
        svc = _make_list_service([page1, page2])
        result = list_messages(svc, "INBOX", max_results=50)
        assert [r["id"] for r in result] == ["m1", "m2", "m3"]

    def test_max_results_caps_output(self):
        page1 = [{"id": f"m{i}"} for i in range(10)]
        svc = _make_list_service([page1])
        result = list_messages(svc, "INBOX", max_results=3)
        assert len(result) == 3

    def test_empty_label(self):
        svc = _make_list_service([[]])
        result = list_messages(svc, "Label_99", max_results=50)
        assert result == []

    def test_http_error_propagates(self):
        svc = MagicMock()
        svc.users.return_value.messages.return_value.list.return_value.execute.side_effect = (
            _http_error(403)
        )
        with pytest.raises(HttpError):
            list_messages(svc, "INBOX")


# ── fetch_message ──────────────────────────────────────────────────── #

class TestFetchMessage:
    def test_basic_fields(self):
        raw = _raw_message("m1", "t1", "Hello", "bob@example.com",
                           "Mon, 15 Jan 2024 10:30:00 +0000", "Test body.")
        msg = fetch_message(_make_fetch_service(raw), "m1")
        assert msg.message_id == "m1"
        assert msg.thread_id == "t1"
        assert msg.subject == "Hello"
        assert msg.sender == "bob@example.com"
        assert "2024-01-15" in msg.date_utc
        assert msg.body_text == "Test body."

    def test_no_subject_uses_fallback(self):
        raw = _raw_message(subject="")
        raw["payload"]["headers"] = [
            h for h in raw["payload"]["headers"] if h["name"] != "Subject"
        ]
        msg = fetch_message(_make_fetch_service(raw), "m1")
        assert msg.subject == "(no subject)"

    def test_returns_parsed_message_type(self):
        msg = fetch_message(_make_fetch_service(_raw_message()), "m1")
        assert isinstance(msg, ParsedMessage)

    def test_attachments_populated(self):
        raw = _raw_message()
        raw["payload"]["mimeType"] = "multipart/mixed"
        raw["payload"]["parts"] = [
            _plain_payload("body"),
            {"mimeType": "application/pdf", "filename": "doc.pdf",
             "body": {"attachmentId": "a1", "size": 512}, "parts": []},
        ]
        msg = fetch_message(_make_fetch_service(raw), "m1")
        assert len(msg.attachments) == 1
        assert isinstance(msg.attachments[0], Attachment)

    def test_long_subject_not_truncated_in_message(self):
        """subject in ParsedMessage is the full string; truncation is for logging only."""
        long_subject = "A" * 200
        raw = _raw_message(subject=long_subject)
        msg = fetch_message(_make_fetch_service(raw), "m1")
        assert msg.subject == long_subject

    def test_http_error_propagates(self):
        svc = MagicMock()
        svc.users.return_value.messages.return_value.get.return_value.execute.side_effect = (
            _http_error(404)
        )
        with pytest.raises(HttpError):
            fetch_message(svc, "missing_id")


# ── download_attachment ────────────────────────────────────────────── #

class TestDownloadAttachment:
    def test_returns_correct_bytes(self):
        payload = b"\x00\x01\x02\xFF binary data"
        svc = _make_attachment_service(_b64_bytes(payload))
        result = download_attachment(svc, "m1", "att1")
        assert result == payload

    def test_returns_bytes_type(self):
        svc = _make_attachment_service(_b64_bytes(b"data"))
        assert isinstance(download_attachment(svc, "m1", "att1"), bytes)

    def test_empty_attachment(self):
        svc = _make_attachment_service("")
        assert download_attachment(svc, "m1", "att1") == b""

    def test_http_error_propagates(self):
        svc = MagicMock()
        svc.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.side_effect = (
            _http_error(500)
        )
        with pytest.raises(HttpError):
            download_attachment(svc, "m1", "att1")
