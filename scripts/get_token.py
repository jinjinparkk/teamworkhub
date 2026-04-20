"""One-time script: exchange a Desktop-app OAuth client for a refresh token.

Run this ONCE locally to obtain GOOGLE_OAUTH_REFRESH_TOKEN.
The token is long-lived — you only need to re-run if it is revoked.

Prerequisites
─────────────
1.  Google Cloud Console → APIs & Services → Credentials
    → Create Credentials → OAuth 2.0 Client ID → Application type: Desktop app
    → Download JSON → save as  scripts/client_secret.json  (gitignored)

2.  Enable the APIs in your project:
      Gmail API  https://console.cloud.google.com/apis/library/gmail.googleapis.com
      Drive API  https://console.cloud.google.com/apis/library/drive.googleapis.com

3.  OAuth consent screen → add your Google account as a Test User
    (required while the app is in "Testing" publishing status)

Usage
─────
    py -3 scripts/get_token.py

The script opens a browser for the consent flow, then prints the three
values you need.  Copy them into .env (local) or Secret Manager (prod).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# ── Dependency check ────────────────────────────────────────────────── #
try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    sys.exit(
        "ERROR: google-auth-oauthlib is not installed.\n"
        "Run:  pip install google-auth-oauthlib"
    )

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
]

CLIENT_SECRET_FILE = Path(__file__).parent / "client_secret.json"


def main() -> None:
    if not CLIENT_SECRET_FILE.exists():
        sys.exit(
            f"ERROR: {CLIENT_SECRET_FILE} not found.\n"
            "Download it from Cloud Console → Credentials → your Desktop OAuth client."
        )

    print("Opening browser for OAuth consent flow...")
    print("Sign in with the Google account whose Gmail/Drive you want to sync.\n")

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRET_FILE),
        scopes=SCOPES,
    )
    # run_local_server starts a temporary HTTP server on localhost to catch
    # the redirect URI — no public server needed.
    creds = flow.run_local_server(port=0)

    print("\n" + "=" * 60)
    print("SUCCESS - copy these values into your .env or Secret Manager:")
    print("=" * 60)
    print(f"GOOGLE_OAUTH_CLIENT_ID={creds.client_id}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={creds.client_secret}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 60)
    print("\nThe refresh token is long-lived.")
    print("Store GOOGLE_OAUTH_REFRESH_TOKEN in Secret Manager for production.")


if __name__ == "__main__":
    main()
