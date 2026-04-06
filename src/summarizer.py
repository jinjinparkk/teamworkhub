"""Gemini API email analyzer — uses REST directly (no SDK).

Single API call per email returns both summary and assignees.
Falls back gracefully when the API key is not set or the call fails.

Usage
─────
  from src.summarizer import summarize, analyze_email

  # Combined (recommended — one Gemini call per email):
  summary, assignees = analyze_email(subject, sender, body_text, api_key)

  # Summary only (legacy):
  summary = summarize(subject, sender, body_text, api_key)
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    """Structured result from a single Gemini analysis call."""
    summary: str = ""
    assignees: list[str] = field(default_factory=list)
    priority: str = "보통"    # "긴급" | "보통" | "낮음"
    category: str = "일반"    # "보고" | "승인요청" | "공지" | "미팅" | "일반"

_BODY_CHAR_LIMIT = 3_000
_MODEL = "gemini-2.5-flash"
_API_URL = (
    "https://generativelanguage.googleapis.com"
    "/v1beta/models/{model}:generateContent?key={key}"
)

# Legacy single-purpose prompt (used by summarize())
_SUMMARY_PROMPT = """\
다음 업무 메일을 읽고 핵심 내용을 한국어 불릿포인트 2-3개로 요약해줘.
포함할 내용: (1) 주요 주제, (2) 필요한 액션/결정사항, (3) 마감일/날짜(있을 경우).
불릿포인트만 출력하고 다른 설명은 넣지 마.

제목: {subject}
보낸 사람: {sender}

본문:
{body}"""

# Combined prompt — returns JSON with summary + assignees + priority + category
_ANALYZE_PROMPT = """\
다음 업무 메일을 분석해서 아래 JSON 형식으로만 출력해줘. 다른 말은 하지 마.

{{
  "summary": ["- 핵심내용1", "- 핵심내용2"],
  "assignees": ["이름1", "이름2"],
  "priority": "보통",
  "category": "일반"
}}

규칙:
- summary: 주요 주제·액션·마감일 중심 한국어 불릿 2-3개 (앞에 "- " 붙여서)
- assignees: 메일 본문/제목에 언급된 담당자 이름만 (직함·호칭 제외). 없으면 []
- priority: 메일 긴급도 → "긴급" (당일·즉시 처리 필요) | "보통" (수일 내) | "낮음" (여유 있음)
- category: 메일 성격 → "보고" | "승인요청" | "공지" | "미팅" | "일반"

제목: {subject}
보낸 사람: {sender}

본문:
{body}"""


def analyze_email(
    subject: str,
    sender: str,
    body_text: str,
    api_key: str,
) -> AnalysisResult:
    """Single Gemini call → AnalysisResult (summary, assignees, priority, category).

    Returns default AnalysisResult on failure or missing key — never raises.
    Prefer this over separate summarize() + extract_assignees() calls.
    """
    if not api_key or not body_text.strip():
        return AnalysisResult()

    try:
        prompt = _ANALYZE_PROMPT.format(
            subject=subject,
            sender=sender,
            body=body_text[:_BODY_CHAR_LIMIT],
        )
        url = _API_URL.format(model=_MODEL, key=api_key)

        # Retry up to 3 times with exponential backoff on 429 rate-limit errors
        for attempt in range(3):
            resp = requests.post(
                url,
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=20,
            )
            if resp.status_code == 429:
                wait = 15 * (2 ** attempt)  # 15s, 30s, 60s
                log.warning("analyze_email -- 429 rate limit, retrying in %ds (attempt %d/3)",
                            wait, attempt + 1)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        else:
            resp.raise_for_status()
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Strip markdown code fences if Gemini wraps in ```json ... ```
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)

        summary_bullets = data.get("summary", [])
        # Gemini occasionally returns a plain string instead of a list
        if isinstance(summary_bullets, str):
            summary_bullets = [summary_bullets] if summary_bullets.strip() else []
        summary = "\n".join(
            b if b.startswith("- ") else f"- {b}" for b in summary_bullets
        )
        assignees = [str(a).strip() for a in data.get("assignees", []) if str(a).strip()]
        priority = data.get("priority", "보통") if data.get("priority") in ("긴급", "보통", "낮음") else "보통"
        category = data.get("category", "일반") if data.get("category") in ("보고", "승인요청", "공지", "미팅", "일반") else "일반"

        log.info("email analyzed", extra={
            "summary_lines": len(summary_bullets),
            "assignees": assignees,
            "priority": priority,
            "category": category,
        })
        return AnalysisResult(summary=summary, assignees=assignees, priority=priority, category=category)

    except Exception as exc:
        log.warning("analyze_email failed - skipping: %s", repr(exc))
        return AnalysisResult()


def summarize(
    subject: str,
    sender: str,
    body_text: str,
    api_key: str,
) -> str:
    """Return a bullet-point summary string, or '' on failure / missing key.

    Legacy single-purpose function. For daily digest use analyze_email() instead.
    Never raises — any exception is caught and logged as a warning.
    """
    if not api_key or not body_text.strip():
        return ""

    try:
        prompt = _SUMMARY_PROMPT.format(
            subject=subject,
            sender=sender,
            body=body_text[:_BODY_CHAR_LIMIT],
        )
        url = _API_URL.format(model=_MODEL, key=api_key)
        resp = requests.post(
            url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=15,
        )
        resp.raise_for_status()
        summary = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        log.info("email summarized", extra={"chars": len(summary)})
        return summary

    except Exception as exc:
        log.warning("summarize failed - skipping: %s", repr(exc))
        return ""
