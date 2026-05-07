"""Shared helpers used by multiple route handlers."""
from __future__ import annotations

import logging
import uuid
from zoneinfo import ZoneInfo

from src import config as cfg_module
from src.auth import build_credentials, build_drive_service, build_gmail_service
from src.gmail_client import fetch_message, list_messages
from src.summarizer import AnalysisResult, _fallback_summary, analyze_email
from src.assignee import extract_assignees

log = logging.getLogger(__name__)


def get_timezone(c: cfg_module.Config) -> ZoneInfo:
    """Return configured timezone with Seoul fallback."""
    try:
        return ZoneInfo(c.timezone)
    except Exception:
        return ZoneInfo("Asia/Seoul")


def build_drive(c: cfg_module.Config):
    """Build credentials + Drive service. Raises on failure."""
    creds = build_credentials(c)
    drive_svc = build_drive_service(creds)
    return creds, drive_svc


def generate_run_id() -> str:
    return uuid.uuid4().hex[:8]


def _collect_messages(
    c: cfg_module.Config,
    creds,
    gmail_q: str,
    run_id: str,
    label: str,
) -> list[tuple]:
    """Fetch and analyze Gmail messages matching *gmail_q* across all accounts.

    Args:
        c:        Loaded Config.
        creds:    Drive-level credentials (reused for single-account Gmail).
        gmail_q:  Gmail search query string (e.g. "after:... before:...").
        run_id:   8-char correlation ID for structured logs.
        label:    Human-readable endpoint label for log messages ("daily" | "weekly" | "monthly").

    Returns a list of (ParsedMessage, AnalysisResult) tuples; never raises.
    """
    account_list = list(c.gmail_accounts) if c.gmail_accounts else [
        cfg_module.AccountConfig(email="", refresh_token="")
    ]
    results: list[tuple] = []

    for account in account_list:
        try:
            if account.refresh_token:
                acc_creds = build_credentials(c, refresh_token=account.refresh_token)
                gmail_svc = build_gmail_service(acc_creds)
            else:
                gmail_svc = build_gmail_service(creds)
        except Exception as exc:
            log.error(f"{label} -- OAuth failed for account",
                      extra={"run_id": run_id, "account": account.email, "error": str(exc)})
            continue

        try:
            stubs = list_messages(gmail_svc, c.gmail_label_id, c.max_messages_per_run, q=gmail_q)
        except Exception as exc:
            log.error(f"{label} -- list_messages failed",
                      extra={"run_id": run_id, "account": account.email, "error": str(exc)})
            continue

        for stub in stubs:
            msg_id = stub.get("id", "")
            try:
                msg = fetch_message(gmail_svc, msg_id)
            except Exception as exc:
                log.error(f"{label} -- fetch_message failed",
                          extra={"run_id": run_id, "message_id": msg_id, "error": str(exc)})
                continue

            try:
                analysis = analyze_email(msg.subject, msg.sender, msg.body_text, c.anthropic_api_key, msg.to, msg.cc)
                if not analysis.assignees:
                    analysis.assignees = extract_assignees(
                        msg.subject, msg.sender, msg.body_text, "", msg.to, msg.cc
                    )
            except Exception as exc:
                log.warning(f"{label} -- analyze/extract failed, using fallback",
                            extra={"run_id": run_id, "message_id": msg_id, "error": str(exc)})
                analysis = AnalysisResult(summary=_fallback_summary(msg.body_text))

            results.append((msg, analysis))
            log.info(f"{label} -- message collected",
                     extra={"run_id": run_id, "message_id": msg_id,
                            "analysis_source": analysis.source})
            # Claude Haiku has generous rate limits; no sleep needed.

    # ── Thread-based dedup: keep latest message per thread_id ────── #
    if len(results) > 1:
        by_thread: dict[str, tuple] = {}
        for pair in results:
            tid = pair[0].thread_id
            if tid not in by_thread or pair[0].date_utc > by_thread[tid][0].date_utc:
                by_thread[tid] = pair
        if len(by_thread) < len(results):
            dropped = len(results) - len(by_thread)
            log.info(f"{label} -- thread dedup dropped {dropped} duplicate(s)",
                     extra={"run_id": run_id, "before": len(results),
                            "after": len(by_thread)})
            results = list(by_thread.values())

    return results
