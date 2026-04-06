"""Integration tests for the /sync pipeline.

All external I/O is mocked (Google OAuth, Gmail API, Drive API).
Tests verify the full per-message loop: idempotency skip, new message
processed, attachment upload, error counting, and auth failure handling.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.drive_client import DriveFile
from src.gmail_client import Attachment, ParsedMessage
from src.summarizer import AnalysisResult

# ── Fixtures & builders ─────────────────────────────────────────────── #

_FULL_ENV = {
    "DRIVE_OUTPUT_FOLDER_ID": "folder_abc",
    "GOOGLE_OAUTH_CLIENT_ID": "client_id",
    "GOOGLE_OAUTH_CLIENT_SECRET": "client_secret",
    "GOOGLE_OAUTH_REFRESH_TOKEN": "refresh_token",
}


def _parsed_message(
    message_id: str = "msg001",
    subject: str = "Hello",
    sender: str = "alice@example.com",
    body_text: str = "Body text.",
    attachments: list | None = None,
) -> ParsedMessage:
    return ParsedMessage(
        message_id=message_id,
        thread_id=f"thread_{message_id}",
        subject=subject,
        sender=sender,
        date_utc="2024-01-15T10:30:00+00:00",
        body_text=body_text,
        attachments=attachments or [],
    )


def _drive_file(
    file_id: str = "df1",
    name: str = "f.pdf",
    web_view_link: str = "https://drive.google.com/f1",
    created: bool = True,
) -> DriveFile:
    return DriveFile(
        file_id=file_id,
        name=name,
        web_view_link=web_view_link,
        created=created,
    )


def _attachment(
    attachment_id: str = "att1",
    filename: str = "report.pdf",
    mime_type: str = "application/pdf",
    size: int = 1024,
) -> Attachment:
    return Attachment(
        attachment_id=attachment_id,
        filename=filename,
        mime_type=mime_type,
        size=size,
    )


@pytest.fixture()
def env(monkeypatch):
    """Set all required env vars."""
    for k, v in _FULL_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── Helpers ─────────────────────────────────────────────────────────── #

def _base_patches(
    list_messages_return=None,
    fetch_message_return=None,
    find_file_by_name_return=None,
    download_attachment_return=b"bytes",
    upload_attachment_return=None,
    upsert_markdown_return=None,
):
    """Return a dict of patch targets with sensible defaults."""
    creds = MagicMock()
    return {
        "src.app.build_credentials": MagicMock(return_value=creds),
        "src.app.build_gmail_service": MagicMock(return_value=MagicMock()),
        "src.app.build_drive_service": MagicMock(return_value=MagicMock()),
        "src.app.list_messages": MagicMock(return_value=list_messages_return or []),
        "src.app.fetch_message": MagicMock(
            return_value=fetch_message_return or _parsed_message()
        ),
        "src.app.find_file_by_name": MagicMock(return_value=find_file_by_name_return),
        "src.app.download_attachment": MagicMock(return_value=download_attachment_return),
        "src.app.upload_attachment": MagicMock(
            return_value=upload_attachment_return or _drive_file()
        ),
        "src.app.upsert_markdown": MagicMock(
            return_value=upsert_markdown_return or _drive_file(name="twh_msg001.md")
        ),
        "src.app.analyze_email": MagicMock(return_value=AnalysisResult()),
    }


def _run_sync(client, patches: dict) -> dict:
    """Apply all patches simultaneously and POST /sync."""
    from contextlib import ExitStack
    with ExitStack() as stack:
        for target, mock in patches.items():
            stack.enter_context(patch(target, mock))
        return client.post("/sync").json()


# ── No messages ─────────────────────────────────────────────────────── #

class TestSyncNoMessages:
    def test_ok_status_when_no_messages(self, env, client):
        data = _run_sync(client, _base_patches(list_messages_return=[]))
        assert data["status"] == "ok"

    def test_zero_counts_when_no_messages(self, env, client):
        data = _run_sync(client, _base_patches(list_messages_return=[]))
        assert data["processed"] == 0
        assert data["skipped"] == 0
        assert data["errors"] == 0


# ── New message (full happy path) ────────────────────────────────────── #

class TestSyncNewMessage:
    def test_processed_count_increments(self, env, client):
        stubs = [{"id": "msg001"}]
        data = _run_sync(
            client,
            _base_patches(
                list_messages_return=stubs,
                find_file_by_name_return=None,  # not in Drive yet
            ),
        )
        assert data["processed"] == 1

    def test_status_ok_for_single_new_message(self, env, client):
        stubs = [{"id": "msg001"}]
        data = _run_sync(
            client,
            _base_patches(list_messages_return=stubs, find_file_by_name_return=None),
        )
        assert data["status"] == "ok"

    def test_upsert_markdown_called(self, env, client):
        stubs = [{"id": "msg001"}]
        patches = _base_patches(list_messages_return=stubs, find_file_by_name_return=None)
        _run_sync(client, patches)
        patches["src.app.upsert_markdown"].assert_called_once()

    def test_fetch_message_called_with_correct_id(self, env, client):
        stubs = [{"id": "msg999"}]
        patches = _base_patches(list_messages_return=stubs, find_file_by_name_return=None)
        _run_sync(client, patches)
        patches["src.app.fetch_message"].assert_called_once()
        _, kwargs = patches["src.app.fetch_message"].call_args
        # message_id is the second positional arg
        args = patches["src.app.fetch_message"].call_args.args
        assert args[1] == "msg999"

    def test_multiple_new_messages_all_processed(self, env, client):
        stubs = [{"id": f"msg{i:03d}"} for i in range(3)]
        patches = _base_patches(list_messages_return=stubs, find_file_by_name_return=None)
        data = _run_sync(client, patches)
        assert data["processed"] == 3
        assert data["skipped"] == 0


# ── Idempotency: already-synced message skipped ──────────────────────── #

class TestSyncIdempotency:
    def test_skipped_when_md_exists(self, env, client):
        stubs = [{"id": "msg001"}]
        existing_md = _drive_file(name="twh_msg001.md", created=False)
        data = _run_sync(
            client,
            _base_patches(list_messages_return=stubs, find_file_by_name_return=existing_md),
        )
        assert data["skipped"] == 1
        assert data["processed"] == 0

    def test_fetch_not_called_when_md_exists(self, env, client):
        stubs = [{"id": "msg001"}]
        existing_md = _drive_file(name="twh_msg001.md", created=False)
        patches = _base_patches(
            list_messages_return=stubs, find_file_by_name_return=existing_md
        )
        _run_sync(client, patches)
        patches["src.app.fetch_message"].assert_not_called()

    def test_upsert_not_called_when_md_exists(self, env, client):
        stubs = [{"id": "msg001"}]
        existing_md = _drive_file(name="twh_msg001.md", created=False)
        patches = _base_patches(
            list_messages_return=stubs, find_file_by_name_return=existing_md
        )
        _run_sync(client, patches)
        patches["src.app.upsert_markdown"].assert_not_called()

    def test_mixed_new_and_skipped(self, env, client):
        """Two messages: one already in Drive, one new."""
        stubs = [{"id": "msg001"}, {"id": "msg002"}]
        existing_md = _drive_file(name="twh_msg001.md", created=False)

        call_count = [0]
        def find_side_effect(svc, name, parent):
            call_count[0] += 1
            if "msg001" in name:
                return existing_md
            return None

        patches = _base_patches(list_messages_return=stubs)
        patches["src.app.find_file_by_name"] = MagicMock(side_effect=find_side_effect)
        data = _run_sync(client, patches)
        assert data["processed"] == 1
        assert data["skipped"] == 1


# ── Attachments ──────────────────────────────────────────────────────── #

class TestSyncAttachments:
    def test_attachment_downloaded_and_uploaded(self, env, client):
        att = _attachment()
        msg = _parsed_message(message_id="msg001", attachments=[att])
        stubs = [{"id": "msg001"}]
        patches = _base_patches(
            list_messages_return=stubs,
            fetch_message_return=msg,
            find_file_by_name_return=None,
        )
        _run_sync(client, patches)
        patches["src.app.download_attachment"].assert_called_once()
        patches["src.app.upload_attachment"].assert_called_once()

    def test_attachment_download_failure_is_non_fatal(self, env, client):
        """If one attachment fails, the message should still be processed."""
        att = _attachment()
        msg = _parsed_message(message_id="msg001", attachments=[att])
        stubs = [{"id": "msg001"}]
        patches = _base_patches(
            list_messages_return=stubs,
            fetch_message_return=msg,
            find_file_by_name_return=None,
        )
        patches["src.app.download_attachment"] = MagicMock(
            side_effect=Exception("network error")
        )
        data = _run_sync(client, patches)
        # The message is still processed (upsert called with empty drive_files)
        assert data["processed"] == 1
        patches["src.app.upsert_markdown"].assert_called_once()


# ── Error handling ───────────────────────────────────────────────────── #

class TestSyncErrors:
    def test_auth_failure_returns_error_status(self, env, client):
        patches = _base_patches()
        patches["src.app.build_credentials"] = MagicMock(
            side_effect=Exception("RefreshError: token expired")
        )
        data = _run_sync(client, patches)
        assert data["status"] == "error"
        assert data["errors"] == 1

    def test_list_messages_failure_returns_error_status(self, env, client):
        patches = _base_patches()
        patches["src.app.list_messages"] = MagicMock(
            side_effect=Exception("HttpError 403")
        )
        data = _run_sync(client, patches)
        assert data["status"] == "error"

    def test_fetch_message_failure_increments_errors(self, env, client):
        stubs = [{"id": "msg001"}]
        patches = _base_patches(
            list_messages_return=stubs,
            find_file_by_name_return=None,
        )
        patches["src.app.fetch_message"] = MagicMock(
            side_effect=Exception("HttpError 404")
        )
        data = _run_sync(client, patches)
        assert data["errors"] == 1
        assert data["processed"] == 0

    def test_upsert_failure_increments_errors(self, env, client):
        stubs = [{"id": "msg001"}]
        patches = _base_patches(
            list_messages_return=stubs,
            find_file_by_name_return=None,
        )
        patches["src.app.upsert_markdown"] = MagicMock(
            side_effect=Exception("Drive 500")
        )
        data = _run_sync(client, patches)
        assert data["errors"] == 1
        assert data["processed"] == 0

    def test_partial_status_when_some_succeed_some_fail(self, env, client):
        """3 messages: 2 succeed, 1 fails on fetch → partial."""
        stubs = [{"id": f"msg{i:03d}"} for i in range(3)]

        call_count = [0]

        def fetch_side_effect(svc, msg_id):
            call_count[0] += 1
            if msg_id == "msg001":
                raise Exception("fetch failed")
            return _parsed_message(message_id=msg_id)

        patches = _base_patches(
            list_messages_return=stubs,
            find_file_by_name_return=None,
        )
        patches["src.app.fetch_message"] = MagicMock(side_effect=fetch_side_effect)
        data = _run_sync(client, patches)
        assert data["processed"] == 2
        assert data["errors"] == 1
        assert data["status"] == "partial"

    def test_find_file_failure_counts_as_error(self, env, client):
        stubs = [{"id": "msg001"}]
        patches = _base_patches(list_messages_return=stubs)
        patches["src.app.find_file_by_name"] = MagicMock(
            side_effect=Exception("Drive 403")
        )
        data = _run_sync(client, patches)
        assert data["errors"] == 1

    def test_all_fail_status_is_error(self, env, client):
        stubs = [{"id": "msg001"}, {"id": "msg002"}]
        patches = _base_patches(
            list_messages_return=stubs,
            find_file_by_name_return=None,
        )
        patches["src.app.fetch_message"] = MagicMock(side_effect=Exception("all fail"))
        data = _run_sync(client, patches)
        assert data["status"] == "error"
        assert data["processed"] == 0
        assert data["errors"] == 2


# ── Multi-account ────────────────────────────────────────────────────── #

class TestSyncMultiAccount:
    """GMAIL_ACCOUNTS_JSON with two accounts — each account's messages are
    processed independently; Drive service is shared."""

    _ACCOUNTS_JSON = (
        '[{"email":"alice@example.com","refresh_token":"tok_a"},'
        '{"email":"bob@example.com","refresh_token":"tok_b"}]'
    )

    def test_messages_from_both_accounts_processed(self, env, monkeypatch, client):
        monkeypatch.setenv("GMAIL_ACCOUNTS_JSON", self._ACCOUNTS_JSON)
        stubs = [{"id": "msg001"}]
        patches = _base_patches(list_messages_return=stubs, find_file_by_name_return=None)
        data = _run_sync(client, patches)
        # Two accounts × one message each = 2 processed
        assert data["processed"] == 2

    def test_one_account_auth_failure_others_continue(self, env, monkeypatch, client):
        monkeypatch.setenv("GMAIL_ACCOUNTS_JSON", self._ACCOUNTS_JSON)
        call_count = [0]

        def creds_side_effect(*args, **kwargs):
            call_count[0] += 1
            # First call = Drive (succeeds); second = account A (fails); third = account B (ok)
            if call_count[0] == 2:
                raise Exception("token revoked")
            return MagicMock()

        stubs = [{"id": "msg001"}]
        patches = _base_patches(list_messages_return=stubs, find_file_by_name_return=None)
        patches["src.app.build_credentials"] = MagicMock(side_effect=creds_side_effect)
        data = _run_sync(client, patches)
        # Account A failed (errors=1) but account B processed 1 message → partial
        assert data["errors"] == 1
        assert data["processed"] == 1
        assert data["status"] == "partial"

    def test_multi_account_ok_status_when_all_succeed(self, env, monkeypatch, client):
        monkeypatch.setenv("GMAIL_ACCOUNTS_JSON", self._ACCOUNTS_JSON)
        patches = _base_patches(list_messages_return=[], find_file_by_name_return=None)
        data = _run_sync(client, patches)
        assert data["status"] == "ok"
        assert data["errors"] == 0


# ── Response shape ───────────────────────────────────────────────────── #

class TestSyncResponseShape:
    def test_always_returns_200(self, env, client):
        patches = _base_patches()
        patches["src.app.build_credentials"] = MagicMock(side_effect=Exception("boom"))
        from contextlib import ExitStack
        with ExitStack() as stack:
            for target, mock in patches.items():
                stack.enter_context(patch(target, mock))
            resp = client.post("/sync")
        assert resp.status_code == 200

    def test_ok_response_has_no_note_field(self, env, client):
        data = _run_sync(client, _base_patches(list_messages_return=[]))
        assert "note" not in data

    def test_error_response_has_note_field(self, env, client):
        patches = _base_patches()
        patches["src.app.build_credentials"] = MagicMock(side_effect=Exception("boom"))
        data = _run_sync(client, patches)
        assert "note" in data


# ── Local migration: twh_*.md → subject-based name ─────────────────── #

class TestSyncLocalMigration:
    """When a Drive commit-marker already exists (skipped message) and
    LOCAL_OUTPUT_DIR contains the old twh_*.md file, the sync endpoint
    should copy it to a subject-based filename so Obsidian wiki-links work."""

    def test_migration_creates_subject_file(self, env, monkeypatch, client, tmp_path):
        """Old twh_msg001.md should be copied to Hello.md on first skip."""
        monkeypatch.setenv("LOCAL_OUTPUT_DIR", str(tmp_path))
        # Write the old-style file
        old_file = tmp_path / "twh_msg001.md"
        old_file.write_text(
            '---\nsubject: "Hello"\nmessage_id: msg001\n---\nbody\n',
            encoding="utf-8",
        )
        stubs = [{"id": "msg001"}]
        existing_md = _drive_file(name="twh_msg001.md", created=False)
        _run_sync(
            client,
            _base_patches(list_messages_return=stubs, find_file_by_name_return=existing_md),
        )
        assert (tmp_path / "Hello.md").exists()

    def test_migration_does_not_overwrite_existing_subject_file(self, env, monkeypatch, client, tmp_path):
        """If Hello.md already exists, migration must NOT overwrite it."""
        monkeypatch.setenv("LOCAL_OUTPUT_DIR", str(tmp_path))
        old_file = tmp_path / "twh_msg001.md"
        old_file.write_text('---\nsubject: "Hello"\n---\nold content\n', encoding="utf-8")
        # Pre-existing subject file with different content
        new_file = tmp_path / "Hello.md"
        new_file.write_text("preserved content", encoding="utf-8")

        stubs = [{"id": "msg001"}]
        existing_md = _drive_file(name="twh_msg001.md", created=False)
        _run_sync(
            client,
            _base_patches(list_messages_return=stubs, find_file_by_name_return=existing_md),
        )
        assert new_file.read_text(encoding="utf-8") == "preserved content"

    def test_migration_skipped_when_no_local_dir(self, env, monkeypatch, client, tmp_path):
        """No LOCAL_OUTPUT_DIR → migration block is not entered, no crash."""
        monkeypatch.delenv("LOCAL_OUTPUT_DIR", raising=False)
        stubs = [{"id": "msg001"}]
        existing_md = _drive_file(name="twh_msg001.md", created=False)
        data = _run_sync(
            client,
            _base_patches(list_messages_return=stubs, find_file_by_name_return=existing_md),
        )
        # Still skipped=1, no error
        assert data["skipped"] == 1
        assert data["errors"] == 0

    def test_migration_skipped_when_old_file_absent(self, env, monkeypatch, client, tmp_path):
        """If twh_*.md does not exist locally, migration is silently skipped."""
        monkeypatch.setenv("LOCAL_OUTPUT_DIR", str(tmp_path))
        # Do NOT create old file
        stubs = [{"id": "msg001"}]
        existing_md = _drive_file(name="twh_msg001.md", created=False)
        data = _run_sync(
            client,
            _base_patches(list_messages_return=stubs, find_file_by_name_return=existing_md),
        )
        assert data["skipped"] == 1
        assert data["errors"] == 0
        assert not (tmp_path / "Hello.md").exists()

    def test_migration_handles_unsafe_subject_chars(self, env, monkeypatch, client, tmp_path):
        """Subject with path-unsafe chars is sanitised (slashes removed etc.)."""
        monkeypatch.setenv("LOCAL_OUTPUT_DIR", str(tmp_path))
        old_file = tmp_path / "twh_msg002.md"
        old_file.write_text(
            '---\nsubject: "Report: Q1/2026"\n---\nbody\n', encoding="utf-8"
        )
        stubs = [{"id": "msg002"}]
        existing_md = _drive_file(name="twh_msg002.md", created=False)
        _run_sync(
            client,
            _base_patches(list_messages_return=stubs, find_file_by_name_return=existing_md),
        )
        # "Report: Q1/2026" → "Report Q12026.md"  (colon and slash removed)
        expected = tmp_path / "Report Q12026.md"
        assert expected.exists()
