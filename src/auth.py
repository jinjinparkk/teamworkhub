"""OAuth2 credentials builder — used by both Gmail and Drive clients.

Flow: refresh-token grant (no browser, no redirect URI needed at runtime).
The refresh token is loaded from env / Secret Manager by config.py; this
module only handles the token-exchange step.

Scopes
──────
  gmail.readonly  — list + fetch messages; we never send or delete.
  drive.file      — create/update only files this app created; not full Drive.
"""
from __future__ import annotations

import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build

from src.config import Config

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

_TOKEN_URI = "https://oauth2.googleapis.com/token"


def build_credentials(cfg: Config, refresh_token: str | None = None) -> Credentials:
    """Exchange the stored refresh token for a short-lived access token.

    Args:
        cfg:           Shared config (supplies client_id, client_secret, scopes).
        refresh_token: Override the token from cfg.  Pass an account-specific
                       refresh token in multi-account mode; omit to use
                       cfg.google_oauth_refresh_token (single-account / Drive).

    Raises google.auth.exceptions.RefreshError if the token is revoked
    or the client credentials are wrong.
    """
    token = refresh_token if refresh_token is not None else cfg.google_oauth_refresh_token
    creds = Credentials(
        token=None,                              # will be obtained via refresh
        refresh_token=token,
        token_uri=_TOKEN_URI,
        client_id=cfg.google_oauth_client_id,
        client_secret=cfg.google_oauth_client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    log.info("OAuth credentials refreshed", extra={"scopes": SCOPES})
    return creds


def build_gmail_service(creds: Credentials) -> Resource:
    return build("gmail", "v1", credentials=creds)


def build_drive_service(creds: Credentials) -> Resource:
    return build("drive", "v3", credentials=creds)
