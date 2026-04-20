"""Unit tests for config.load() and config.validate_for_sync().

All tests are pure Python — no network, no files.
"""
from __future__ import annotations

import pytest

import json

from src import config as cfg_module
from src.config import AccountConfig, Config, load, validate_for_sync

# ── Required-field sentinel ──────────────────────────────────────────── #

_REQUIRED_VARS = {
    "DRIVE_OUTPUT_FOLDER_ID": "folder_id",
    "GOOGLE_OAUTH_CLIENT_ID": "client_id",
    "GOOGLE_OAUTH_CLIENT_SECRET": "client_secret",
    "GOOGLE_OAUTH_REFRESH_TOKEN": "refresh_token",
}


@pytest.fixture()
def full_env(monkeypatch):
    """Set all four required env vars."""
    for k, v in _REQUIRED_VARS.items():
        monkeypatch.setenv(k, v)


# ── load() — defaults ────────────────────────────────────────────────── #

class TestLoadDefaults:
    def test_returns_config_instance(self, monkeypatch):
        # Clear required vars so we're reading defaults only.
        for k in _REQUIRED_VARS:
            monkeypatch.delenv(k, raising=False)
        c = load()
        assert isinstance(c, Config)

    def test_gmail_label_defaults_to_inbox(self, monkeypatch):
        monkeypatch.delenv("GMAIL_LABEL_ID", raising=False)
        assert load().gmail_label_id == "INBOX"

    def test_max_messages_defaults_to_50(self, monkeypatch):
        monkeypatch.delenv("MAX_MESSAGES_PER_RUN", raising=False)
        assert load().max_messages_per_run == 50

    def test_timezone_defaults_to_utc(self, monkeypatch):
        monkeypatch.delenv("TIMEZONE", raising=False)
        assert load().timezone == "UTC"

    def test_log_format_defaults_to_json(self, monkeypatch):
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        assert load().log_format == "json"

    def test_log_level_defaults_to_info(self, monkeypatch):
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        assert load().log_level == "INFO"

    def test_drive_folder_defaults_to_empty(self, monkeypatch):
        monkeypatch.delenv("DRIVE_OUTPUT_FOLDER_ID", raising=False)
        assert load().drive_output_folder_id == ""

    def test_oauth_fields_default_to_empty(self, monkeypatch):
        for k in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
                  "GOOGLE_OAUTH_REFRESH_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        c = load()
        assert c.google_oauth_client_id == ""
        assert c.google_oauth_client_secret == ""
        assert c.google_oauth_refresh_token == ""


# ── load() — env var overrides ───────────────────────────────────────── #

class TestLoadOverrides:
    def test_gmail_label_id_from_env(self, monkeypatch):
        monkeypatch.setenv("GMAIL_LABEL_ID", "Label_123")
        assert load().gmail_label_id == "Label_123"

    def test_max_messages_from_env(self, monkeypatch):
        monkeypatch.setenv("MAX_MESSAGES_PER_RUN", "10")
        assert load().max_messages_per_run == 10

    def test_max_messages_is_int(self, monkeypatch):
        monkeypatch.setenv("MAX_MESSAGES_PER_RUN", "25")
        assert isinstance(load().max_messages_per_run, int)

    def test_drive_folder_from_env(self, monkeypatch):
        monkeypatch.setenv("DRIVE_OUTPUT_FOLDER_ID", "abc123")
        assert load().drive_output_folder_id == "abc123"

    def test_oauth_client_id_from_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "my-client-id")
        assert load().google_oauth_client_id == "my-client-id"

    def test_oauth_client_secret_from_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret!")
        assert load().google_oauth_client_secret == "secret!"

    def test_oauth_refresh_token_from_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "1//token")
        assert load().google_oauth_refresh_token == "1//token"

    def test_timezone_from_env(self, monkeypatch):
        monkeypatch.setenv("TIMEZONE", "Asia/Seoul")
        assert load().timezone == "Asia/Seoul"

    def test_log_format_pretty_from_env(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "pretty")
        assert load().log_format == "pretty"

    def test_log_level_debug_from_env(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        assert load().log_level == "DEBUG"


# ── validate_for_sync() ──────────────────────────────────────────────── #

class TestValidateForSync:
    def _cfg(self, **overrides) -> Config:
        """Build a Config with all required fields set by default."""
        defaults = dict(
            gmail_label_id="INBOX",
            max_messages_per_run=50,
            drive_output_folder_id="folder",
            google_oauth_client_id="cid",
            google_oauth_client_secret="csecret",
            google_oauth_refresh_token="rtoken",
            local_output_dir="",
            gemini_api_key="",
            timezone="UTC",
            log_format="json",
            log_level="INFO",
        )
        defaults.update(overrides)
        return Config(**defaults)

    def test_empty_list_when_all_required_present(self):
        assert validate_for_sync(self._cfg()) == []

    def test_returns_list(self):
        assert isinstance(validate_for_sync(self._cfg()), list)

    def test_missing_drive_folder_reported(self):
        missing = validate_for_sync(self._cfg(drive_output_folder_id=""))
        assert "DRIVE_OUTPUT_FOLDER_ID" in missing

    def test_missing_client_id_reported(self):
        missing = validate_for_sync(self._cfg(google_oauth_client_id=""))
        assert "GOOGLE_OAUTH_CLIENT_ID" in missing

    def test_missing_client_secret_reported(self):
        missing = validate_for_sync(self._cfg(google_oauth_client_secret=""))
        assert "GOOGLE_OAUTH_CLIENT_SECRET" in missing

    def test_missing_refresh_token_reported(self):
        missing = validate_for_sync(self._cfg(google_oauth_refresh_token=""))
        assert "GOOGLE_OAUTH_REFRESH_TOKEN" in missing

    def test_all_missing_returns_four_items(self):
        missing = validate_for_sync(
            self._cfg(
                drive_output_folder_id="",
                google_oauth_client_id="",
                google_oauth_client_secret="",
                google_oauth_refresh_token="",
            )
        )
        assert len(missing) == 4

    def test_partial_missing_correct_count(self):
        missing = validate_for_sync(
            self._cfg(google_oauth_client_id="", google_oauth_client_secret="")
        )
        assert len(missing) == 2

    def test_non_required_fields_not_validated(self):
        """Optional fields like gmail_label_id must not appear in missing list."""
        missing = validate_for_sync(self._cfg(gmail_label_id=""))
        assert "GMAIL_LABEL_ID" not in missing

    def test_load_then_validate_round_trip(self, monkeypatch):
        """load() → validate_for_sync() with full env should return []."""
        for k, v in _REQUIRED_VARS.items():
            monkeypatch.setenv(k, v)
        assert validate_for_sync(load()) == []

    def test_load_then_validate_with_missing_env(self, monkeypatch):
        """load() → validate_for_sync() with empty env returns non-empty list."""
        for k in _REQUIRED_VARS:
            monkeypatch.delenv(k, raising=False)
        assert validate_for_sync(load()) != []


# ── AccountConfig ────────────────────────────────────────────────────── #

class TestAccountConfig:
    def test_email_and_refresh_token_stored(self):
        ac = AccountConfig(email="alice@example.com", refresh_token="tok123")
        assert ac.email == "alice@example.com"
        assert ac.refresh_token == "tok123"

    def test_empty_email_allowed(self):
        ac = AccountConfig(email="", refresh_token="tok")
        assert ac.email == ""


# ── Multi-account: load() with GMAIL_ACCOUNTS_JSON ───────────────────── #

class TestLoadMultiAccount:
    def test_gmail_accounts_defaults_to_empty_tuple(self, monkeypatch):
        monkeypatch.delenv("GMAIL_ACCOUNTS_JSON", raising=False)
        assert load().gmail_accounts == ()

    def test_gmail_accounts_parsed_from_json(self, monkeypatch):
        data = [
            {"email": "a@example.com", "refresh_token": "tok_a"},
            {"email": "b@example.com", "refresh_token": "tok_b"},
        ]
        monkeypatch.setenv("GMAIL_ACCOUNTS_JSON", json.dumps(data))
        accounts = load().gmail_accounts
        assert len(accounts) == 2
        assert accounts[0].email == "a@example.com"
        assert accounts[1].refresh_token == "tok_b"

    def test_gmail_accounts_is_tuple(self, monkeypatch):
        data = [{"email": "a@example.com", "refresh_token": "tok"}]
        monkeypatch.setenv("GMAIL_ACCOUNTS_JSON", json.dumps(data))
        assert isinstance(load().gmail_accounts, tuple)

    def test_single_account_in_json(self, monkeypatch):
        data = [{"email": "solo@example.com", "refresh_token": "solo_tok"}]
        monkeypatch.setenv("GMAIL_ACCOUNTS_JSON", json.dumps(data))
        accounts = load().gmail_accounts
        assert len(accounts) == 1
        assert accounts[0].email == "solo@example.com"

    def test_invalid_json_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("GMAIL_ACCOUNTS_JSON", "not-valid-json")
        with pytest.raises(ValueError, match="GMAIL_ACCOUNTS_JSON"):
            load()


# ── Dashboard optional fields ────────────────────────────────────────── #

class TestOptionalOutputDirs:
    def test_local_dashboard_dir_defaults_to_empty(self, monkeypatch):
        monkeypatch.delenv("LOCAL_DASHBOARD_DIR", raising=False)
        assert load().local_dashboard_dir == ""

    def test_local_dashboard_dir_from_env(self, monkeypatch):
        monkeypatch.setenv("LOCAL_DASHBOARD_DIR", "/path/to/dashboard")
        assert load().local_dashboard_dir == "/path/to/dashboard"
