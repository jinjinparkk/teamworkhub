"""Unit tests for auth — OAuth credential builder and service factories.

All Google API calls are mocked; no network access required.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from src.auth import SCOPES, build_credentials, build_drive_service, build_gmail_service
from src.config import Config

# ── Helper ───────────────────────────────────────────────────────────── #

def _cfg(**overrides) -> Config:
    defaults = dict(
        gmail_label_id="INBOX",
        max_messages_per_run=50,
        drive_output_folder_id="folder",
        google_oauth_client_id="my-client-id",
        google_oauth_client_secret="my-secret",
        google_oauth_refresh_token="my-refresh-token",
        local_output_dir="",
        gemini_api_key="",
        timezone="UTC",
        log_format="json",
        log_level="INFO",
    )
    defaults.update(overrides)
    return Config(**defaults)


# ── build_credentials() ──────────────────────────────────────────────── #

class TestBuildCredentials:
    @patch("src.auth.Request")
    @patch("src.auth.Credentials")
    def test_returns_credentials_object(self, mock_creds_cls, mock_request_cls):
        creds_instance = MagicMock()
        mock_creds_cls.return_value = creds_instance
        result = build_credentials(_cfg())
        assert result is creds_instance

    @patch("src.auth.Request")
    @patch("src.auth.Credentials")
    def test_refresh_is_called(self, mock_creds_cls, mock_request_cls):
        creds_instance = MagicMock()
        mock_creds_cls.return_value = creds_instance
        build_credentials(_cfg())
        creds_instance.refresh.assert_called_once()

    @patch("src.auth.Request")
    @patch("src.auth.Credentials")
    def test_client_id_passed_to_credentials(self, mock_creds_cls, mock_request_cls):
        mock_creds_cls.return_value = MagicMock()
        build_credentials(_cfg(google_oauth_client_id="test-client"))
        _, kwargs = mock_creds_cls.call_args
        assert kwargs["client_id"] == "test-client"

    @patch("src.auth.Request")
    @patch("src.auth.Credentials")
    def test_client_secret_passed_to_credentials(self, mock_creds_cls, mock_request_cls):
        mock_creds_cls.return_value = MagicMock()
        build_credentials(_cfg(google_oauth_client_secret="top-secret"))
        _, kwargs = mock_creds_cls.call_args
        assert kwargs["client_secret"] == "top-secret"

    @patch("src.auth.Request")
    @patch("src.auth.Credentials")
    def test_refresh_token_passed_to_credentials(self, mock_creds_cls, mock_request_cls):
        mock_creds_cls.return_value = MagicMock()
        build_credentials(_cfg(google_oauth_refresh_token="rtoken123"))
        _, kwargs = mock_creds_cls.call_args
        assert kwargs["refresh_token"] == "rtoken123"

    @patch("src.auth.Request")
    @patch("src.auth.Credentials")
    def test_scopes_passed_to_credentials(self, mock_creds_cls, mock_request_cls):
        mock_creds_cls.return_value = MagicMock()
        build_credentials(_cfg())
        _, kwargs = mock_creds_cls.call_args
        assert kwargs["scopes"] == SCOPES

    @patch("src.auth.Request")
    @patch("src.auth.Credentials")
    def test_token_is_none_initially(self, mock_creds_cls, mock_request_cls):
        mock_creds_cls.return_value = MagicMock()
        build_credentials(_cfg())
        _, kwargs = mock_creds_cls.call_args
        assert kwargs["token"] is None

    @patch("src.auth.Request")
    @patch("src.auth.Credentials")
    def test_refresh_error_propagates(self, mock_creds_cls, mock_request_cls):
        creds_instance = MagicMock()
        creds_instance.refresh.side_effect = Exception("RefreshError: token revoked")
        mock_creds_cls.return_value = creds_instance
        with pytest.raises(Exception, match="RefreshError"):
            build_credentials(_cfg())

    @patch("src.auth.Request")
    @patch("src.auth.Credentials")
    def test_refresh_token_override_used_when_provided(self, mock_creds_cls, mock_request_cls):
        mock_creds_cls.return_value = MagicMock()
        build_credentials(_cfg(), refresh_token="override_token")
        _, kwargs = mock_creds_cls.call_args
        assert kwargs["refresh_token"] == "override_token"

    @patch("src.auth.Request")
    @patch("src.auth.Credentials")
    def test_config_refresh_token_used_when_no_override(self, mock_creds_cls, mock_request_cls):
        mock_creds_cls.return_value = MagicMock()
        build_credentials(_cfg(google_oauth_refresh_token="cfg_token"))
        _, kwargs = mock_creds_cls.call_args
        assert kwargs["refresh_token"] == "cfg_token"

    @patch("src.auth.Request")
    @patch("src.auth.Credentials")
    def test_override_takes_precedence_over_config_token(self, mock_creds_cls, mock_request_cls):
        mock_creds_cls.return_value = MagicMock()
        build_credentials(_cfg(google_oauth_refresh_token="cfg_token"), refresh_token="override")
        _, kwargs = mock_creds_cls.call_args
        assert kwargs["refresh_token"] == "override"


# ── SCOPES constant ──────────────────────────────────────────────────── #

class TestScopes:
    def test_scopes_is_list(self):
        assert isinstance(SCOPES, list)

    def test_gmail_readonly_scope_present(self):
        assert any("gmail.readonly" in s for s in SCOPES)

    def test_drive_file_scope_present(self):
        assert any("drive.file" in s for s in SCOPES)

    def test_exactly_two_scopes(self):
        assert len(SCOPES) == 2


# ── build_gmail_service() ────────────────────────────────────────────── #

class TestBuildGmailService:
    @patch("src.auth.build")
    def test_calls_build_with_gmail(self, mock_build):
        creds = MagicMock()
        build_gmail_service(creds)
        mock_build.assert_called_once_with("gmail", "v1", credentials=creds)

    @patch("src.auth.build")
    def test_returns_resource(self, mock_build):
        fake_resource = MagicMock()
        mock_build.return_value = fake_resource
        result = build_gmail_service(MagicMock())
        assert result is fake_resource


# ── build_drive_service() ────────────────────────────────────────────── #

class TestBuildDriveService:
    @patch("src.auth.build")
    def test_calls_build_with_drive(self, mock_build):
        creds = MagicMock()
        build_drive_service(creds)
        mock_build.assert_called_once_with("drive", "v3", credentials=creds)

    @patch("src.auth.build")
    def test_returns_resource(self, mock_build):
        fake_resource = MagicMock()
        mock_build.return_value = fake_resource
        result = build_drive_service(MagicMock())
        assert result is fake_resource
