"""Runtime configuration — all values come from environment variables.

Never import credentials from a file here.
For GCP Secret Manager usage see README § Secret Manager.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AccountConfig:
    """OAuth identity for one Gmail account to sync.

    In multi-account mode, each entry needs only its own refresh_token.
    The OAuth client_id and client_secret are shared across all accounts
    (same GCP OAuth app).
    """
    email: str           # used as filename prefix; empty string = no prefix
    refresh_token: str   # long-lived token for this account


@dataclass(frozen=True)
class Config:
    # ── Gmail ────────────────────────────────────────────────────────── #
    gmail_label_id: str          # label id or "INBOX"
    max_messages_per_run: int    # guard against runaway fetches

    # ── Drive ────────────────────────────────────────────────────────── #
    drive_output_folder_id: str  # parent folder for all output files

    # ── OAuth (shared OAuth app credentials) ─────────────────────────── #
    google_oauth_client_id: str
    google_oauth_client_secret: str
    google_oauth_refresh_token: str  # used for Drive + single-account Gmail

    # ── Local Obsidian output (optional) ────────────────────────────── #
    local_output_dir: str        # absolute path to Obsidian vault folder; empty = disabled

    # ── Gemini summarization (optional) ─────────────────────────────── #
    gemini_api_key: str          # empty string disables summarization gracefully

    # ── Misc ─────────────────────────────────────────────────────────── #
    timezone: str                # e.g. "Asia/Seoul"; used for note timestamps
    log_format: str              # "json" | "pretty"
    log_level: str               # "DEBUG" | "INFO" | "WARNING" | "ERROR"

    # ── Multi-account Gmail (optional) ───────────────────────────────── #
    # Empty tuple = single-account mode (uses google_oauth_refresh_token for Gmail).
    # When set, each AccountConfig supplies its own refresh_token for Gmail;
    # google_oauth_refresh_token is still required for Drive writes.
    gmail_accounts: tuple[AccountConfig, ...] = ()

    # ── Daily digest (optional) ──────────────────────────────────────── #
    # POST /daily collects overnight emails (18:00~08:59) into one Daily Note.
    # Falls back to drive_output_folder_id / local_output_dir when empty.
    daily_output_folder_id: str = ""    # Drive folder for YYYY-MM-DD.md files
    local_daily_output_dir: str = ""    # Local Obsidian daily notes folder

    # ── Weekly digest (optional) ─────────────────────────────────────── #
    # POST /weekly generates a weekly report (YYYY-WNN.md).
    # Falls back to daily_output_folder_id → drive_output_folder_id when empty.
    weekly_output_folder_id: str = ""   # Drive folder for YYYY-WNN.md files
    local_weekly_output_dir: str = ""   # Local Obsidian weekly notes folder

    # ── Dashboard & assignee pages (optional) ─────────────────────────── #
    # POST /dashboard writes Dashboard.md + per-assignee pages.
    # Assignee pages are also auto-written on each POST /daily run.
    local_dashboard_dir: str = ""       # Local folder for Dashboard.md + assignee pages

    # ── Drive email archive scan (optional) ────────────────────────────── #
    # POST /scan-archive reads mail folders from a shared Drive folder.
    drive_email_archive_folder_id: str = ""  # parent folder containing date_sender_subject subfolders


def load() -> Config:
    """Build Config from environment variables.  Never raises on missing keys;
    call ``validate_for_sync()`` when a real sync is about to run."""
    accounts_raw = os.environ.get("GMAIL_ACCOUNTS_JSON", "")
    if accounts_raw:
        try:
            gmail_accounts: tuple[AccountConfig, ...] = tuple(
                AccountConfig(**a) for a in json.loads(accounts_raw)
            )
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            raise ValueError(
                f"GMAIL_ACCOUNTS_JSON is invalid -- check JSON format: {exc}"
            ) from exc
    else:
        gmail_accounts = ()

    return Config(
        gmail_label_id=os.environ.get("GMAIL_LABEL_ID", "INBOX"),
        max_messages_per_run=int(os.environ.get("MAX_MESSAGES_PER_RUN", "50")),
        drive_output_folder_id=os.environ.get("DRIVE_OUTPUT_FOLDER_ID", ""),
        google_oauth_client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        google_oauth_client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        google_oauth_refresh_token=os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", ""),
        local_output_dir=os.environ.get("LOCAL_OUTPUT_DIR", ""),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        timezone=os.environ.get("TIMEZONE", "UTC"),
        log_format=os.environ.get("LOG_FORMAT", "json"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        gmail_accounts=gmail_accounts,
        daily_output_folder_id=os.environ.get("DAILY_OUTPUT_FOLDER_ID", ""),
        local_daily_output_dir=os.environ.get("LOCAL_DAILY_OUTPUT_DIR", ""),
        weekly_output_folder_id=os.environ.get("WEEKLY_OUTPUT_FOLDER_ID", ""),
        local_weekly_output_dir=os.environ.get("LOCAL_WEEKLY_OUTPUT_DIR", ""),
        local_dashboard_dir=os.environ.get("LOCAL_DASHBOARD_DIR", ""),
        drive_email_archive_folder_id=os.environ.get("DRIVE_EMAIL_ARCHIVE_FOLDER_ID", ""),
    )


def validate_for_sync(cfg: Config) -> list[str]:
    """Return a list of env-var names that are required for a real sync run
    but are currently empty.  Empty list means the config is complete."""
    required = {
        "DRIVE_OUTPUT_FOLDER_ID": cfg.drive_output_folder_id,
        "GOOGLE_OAUTH_CLIENT_ID": cfg.google_oauth_client_id,
        "GOOGLE_OAUTH_CLIENT_SECRET": cfg.google_oauth_client_secret,
        "GOOGLE_OAUTH_REFRESH_TOKEN": cfg.google_oauth_refresh_token,
    }
    return [k for k, v in required.items() if not v]


def validate_for_scan_archive(cfg: Config) -> list[str]:
    """Return a list of env-var names required for /scan-archive but currently empty."""
    required = {
        "DRIVE_EMAIL_ARCHIVE_FOLDER_ID": cfg.drive_email_archive_folder_id,
        "GOOGLE_OAUTH_CLIENT_ID": cfg.google_oauth_client_id,
        "GOOGLE_OAUTH_CLIENT_SECRET": cfg.google_oauth_client_secret,
        "GOOGLE_OAUTH_REFRESH_TOKEN": cfg.google_oauth_refresh_token,
    }
    return [k for k, v in required.items() if not v]
