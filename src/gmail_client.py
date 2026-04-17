"""Gmail API client — step 3.

Public API
──────────
  list_messages(service, label_id, max_results) -> list[dict]
  fetch_message(service, message_id)            -> ParsedMessage
  download_attachment(service, msg_id, att_id)  -> bytes

Logging contract (hard rule — never violate):
  ✓ Log: message_id, subject[:80], label counts, error codes.
  ✗ Never log: body_text, raw headers, attachment bytes.

# Phase 2 (do not implement now):
# Replace list_messages() polling with a Pub/Sub push-notification handler
# so Cloud Run is triggered on arrival instead of on a schedule.
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import html2text
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

_SUBJECT_LOG_LIMIT = 80   # hard limit: never log more than this many subject chars


# ── Data models ────────────────────────────────────────────────────── #

@dataclass
class Attachment:
    attachment_id: str
    filename: str
    mime_type: str
    size: int          # bytes, as reported by the API


@dataclass
class ParsedMessage:
    message_id: str
    thread_id: str
    subject: str       # raw subject — log only [:80]
    sender: str        # "Name <email>" as-is from From header
    to: str            # To header as-is
    cc: str            # CC header as-is
    date_utc: str      # ISO-8601 UTC, e.g. "2024-01-15T10:30:00+00:00"
    body_text: str     # plain-text body ── NEVER LOG THIS FIELD
    attachments: list[Attachment] = field(default_factory=list)


# ── Private helpers ────────────────────────────────────────────────── #

def _b64decode(data: str) -> bytes:
    """Decode a base64url-encoded string (Gmail omits padding)."""
    # Restore padding to a multiple of 4.
    padding = (-len(data)) % 4
    return base64.urlsafe_b64decode(data + "=" * padding)


def _html_to_text(html: str) -> str:
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.body_width = 0        # no hard line-wrap
    return h.handle(html).strip()


def _extract_body(payload: dict) -> str:
    """Recursively extract the best plain-text representation of a message.

    Priority: text/plain > html2text(text/html) > recursed multipart.
    Returns an empty string if nothing usable is found.
    """
    mime = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime == "text/plain" and body_data:
        return _b64decode(body_data).decode("utf-8", errors="replace")

    if mime == "text/html" and body_data:
        raw_html = _b64decode(body_data).decode("utf-8", errors="replace")
        return _html_to_text(raw_html)

    parts = payload.get("parts", [])
    if not parts:
        return ""

    # Prefer text/plain sub-part first.
    for part in parts:
        if part.get("mimeType") == "text/plain":
            text = _extract_body(part)
            if text:
                return text

    # Recurse into nested multipart containers (skip html for now).
    for part in parts:
        if part.get("mimeType", "").startswith("multipart/"):
            text = _extract_body(part)
            if text:
                return text

    # Last resort: take the first html part.
    for part in parts:
        if part.get("mimeType") == "text/html":
            text = _extract_body(part)
            if text:
                return text

    return ""


def _extract_attachments(payload: dict) -> list[Attachment]:
    """Walk the MIME tree and return every part with an attachmentId."""
    results: list[Attachment] = []

    def _walk(part: dict) -> None:
        att_id = part.get("body", {}).get("attachmentId", "")
        filename = part.get("filename", "")
        if att_id and filename:
            results.append(Attachment(
                attachment_id=att_id,
                filename=filename,
                mime_type=part.get("mimeType", "application/octet-stream"),
                size=part.get("body", {}).get("size", 0),
            ))
        for child in part.get("parts", []):
            _walk(child)

    _walk(payload)
    return results


def _parse_date(date_str: str) -> str:
    """Parse an RFC 2822 Date header to an ISO-8601 UTC string.

    Falls back to the current time if parsing fails (rather than crashing).
    """
    if not date_str:
        return datetime.now(tz=timezone.utc).isoformat()
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        log.warning("Could not parse date header", extra={"raw_date": date_str[:40]})
        return datetime.now(tz=timezone.utc).isoformat()


def _parse_message(raw: dict) -> ParsedMessage:
    payload = raw.get("payload", {})
    headers: dict[str, str] = {
        h["name"].lower(): h["value"]
        for h in payload.get("headers", [])
        if "name" in h and "value" in h
    }
    subject = headers.get("subject", "(no subject)")

    return ParsedMessage(
        message_id=raw["id"],
        thread_id=raw.get("threadId", raw["id"]),
        subject=subject,
        sender=headers.get("from", ""),
        to=headers.get("to", ""),
        cc=headers.get("cc", ""),
        date_utc=_parse_date(headers.get("date", "")),
        body_text=_extract_body(payload),           # NEVER LOG
        attachments=_extract_attachments(payload),
    )


# ── Public API ─────────────────────────────────────────────────────── #

def list_messages(
    service,
    label_id: str,
    max_results: int = 50,
    q: str = "",
) -> list[dict]:
    """Return message stubs [{id, threadId}, ...] for *label_id*.

    Args:
        service:     Authenticated Gmail API resource.
        label_id:    Gmail label id (e.g. "INBOX") to filter by.
        max_results: Upper bound on stubs returned.
        q:           Optional Gmail search query (e.g. "after:1700000000 before:1700100000").
                     Supports the same operators as the Gmail search box.

    Uses the list → list_next pagination pattern; stops once *max_results*
    stubs have been collected.  The caller decides which to fetch in full.
    """
    collected: list[dict] = []
    # Clamp batch size: Gmail API max per page is 500.
    batch = min(max_results, 500)

    list_kwargs: dict = {"userId": "me", "labelIds": [label_id], "maxResults": batch}
    if q:
        list_kwargs["q"] = q

    request = service.users().messages().list(**list_kwargs)

    while request is not None and len(collected) < max_results:
        try:
            response = request.execute()
        except HttpError as exc:
            log.error(
                "list_messages API error",
                extra={"label_id": label_id, "status": exc.status_code},
            )
            raise

        collected.extend(response.get("messages", []))

        request = service.users().messages().list_next(
            previous_request=request,
            previous_response=response,
        )

    result = collected[:max_results]
    log.info(
        "list_messages complete",
        extra={"label_id": label_id, "fetched": len(result)},
    )
    return result


def fetch_message(service, message_id: str) -> ParsedMessage:
    """Fetch and parse one message.  Logs id + subject[:80] only."""
    try:
        raw = service.users().messages().get(
            userId="me",
            id=message_id,
            format="full",
        ).execute()
    except HttpError as exc:
        log.error(
            "fetch_message API error",
            extra={"message_id": message_id, "status": exc.status_code},
        )
        raise

    msg = _parse_message(raw)
    log.info(
        "fetched message",
        extra={
            "message_id": msg.message_id,
            "subject": msg.subject[:_SUBJECT_LOG_LIMIT],   # hard limit
            "attachments": len(msg.attachments),
        },
    )
    return msg


def download_attachment(service, message_id: str, attachment_id: str) -> bytes:
    """Return raw bytes for one attachment.  Never logs the content."""
    try:
        resp = service.users().messages().attachments().get(
            userId="me",
            messageId=message_id,
            id=attachment_id,
        ).execute()
    except HttpError as exc:
        log.error(
            "download_attachment API error",
            extra={
                "message_id": message_id,
                "attachment_id": attachment_id,
                "status": exc.status_code,
            },
        )
        raise

    data = resp.get("data", "")
    raw_bytes = _b64decode(data)
    log.info(
        "attachment downloaded",
        extra={"message_id": message_id, "size_bytes": len(raw_bytes)},
    )
    return raw_bytes
