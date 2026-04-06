"""Assignee extractor for daily digest.

Two-step approach:
  1. Regex scan for Korean name+title patterns (e.g. 박은진대리님, 이해랑팀장님)
  2. Gemini fallback when no names found — infers likely assignees from context

Returns a deduplicated list of name strings (titles stripped).
"""
from __future__ import annotations

import logging
import re

import requests

log = logging.getLogger(__name__)

# Matches: Korean name (2-4 chars) + optional space + title + optional 님
_NAME_RE = re.compile(
    r"([가-힣]{2,4})\s*(?:대리|팀장|과장|부장|사원|주임|차장|이사|선임|수석)님?"
)

_INFER_PROMPT = """\
이 업무 메일에서 조치나 확인이 필요한 담당자를 추론해줘.
이름만 쉼표로 구분해서 한 줄로 출력해. 없으면 아무것도 출력하지 마.
예시: 박은진, 이해랑

제목: {subject}
보낸 사람: {sender}

본문:
{body}"""

_BODY_CHAR_LIMIT = 2_000
_MODEL = "gemini-2.5-flash"
_API_URL = (
    "https://generativelanguage.googleapis.com"
    "/v1beta/models/{model}:generateContent?key={key}"
)


def extract_assignees(
    subject: str,
    sender: str,
    body_text: str,
    api_key: str,
) -> list[str]:
    """Return deduplicated list of assignee names, or [] if none found.

    Step 1: regex scan for Korean name+title patterns in subject + body.
    Step 2: Gemini inference if regex finds nothing and api_key is set.
    Never raises.
    """
    names = _regex_extract(subject + " " + body_text)
    if names:
        log.info("assignees extracted by regex", extra={"names": names})
        return names

    if api_key:
        return _gemini_infer(subject, sender, body_text, api_key)

    return []


def _regex_extract(text: str) -> list[str]:
    """Extract unique names from Korean name+title patterns."""
    seen: dict[str, None] = {}
    for match in _NAME_RE.finditer(text):
        seen[match.group(1)] = None
    return list(seen)


def _gemini_infer(
    subject: str,
    sender: str,
    body_text: str,
    api_key: str,
) -> list[str]:
    """Ask Gemini to infer assignees when regex finds nothing."""
    try:
        prompt = _INFER_PROMPT.format(
            subject=subject,
            sender=sender,
            body=body_text[:_BODY_CHAR_LIMIT],
        )
        url = _API_URL.format(model=_MODEL, key=api_key)
        resp = requests.post(
            url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if not raw:
            return []
        names = [n.strip() for n in raw.split(",") if n.strip()]
        log.info("assignees inferred by Gemini", extra={"names": names})
        return names
    except Exception as exc:
        log.warning("assignee inference failed: %s", repr(exc))
        return []
