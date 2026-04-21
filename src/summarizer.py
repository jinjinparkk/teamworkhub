"""Claude Haiku 4.5 email analyzer — uses anthropic SDK.

Single API call per email returns both summary and assignees.
Falls back gracefully when the API key is not set or the call fails.

Usage
─────
  from src.summarizer import summarize, analyze_email

  # Combined (recommended — one Claude call per email):
  summary, assignees = analyze_email(subject, sender, body_text, api_key)

  # Summary only (legacy):
  summary = summarize(subject, sender, body_text, api_key)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import anthropic

from src.assignee import normalize_name, extract_assignees_from_email, is_valid_assignee

log = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    """Structured result from a single Claude analysis call."""
    summary: str = ""
    assignees: list[str] = field(default_factory=list)
    priority: str = "보통"    # "긴급" | "보통" | "낮음"
    category: str = "일반"    # "보고" | "승인요청" | "공지" | "미팅" | "일반"
    short_title: str = ""     # 20자 이내 핵심 요약 제목
    description: str = ""     # 100자 이내 메일 한 줄 요약
    source: str = "fallback"  # "claude" | "fallback"

def _fallback_summary(body_text: str) -> str:
    """Generate a simple extractive summary from the email body.

    Used when Claude is unavailable.  Takes the first 3 non-empty lines
    and formats them as bullet points so the detail page is still useful.
    """
    if not body_text or not body_text.strip():
        return ""
    lines = [ln.strip() for ln in body_text.strip().splitlines() if ln.strip()]
    selected = lines[:3]
    return "\n".join(f"- {ln}" if not ln.startswith("- ") else ln for ln in selected)


_BODY_CHAR_LIMIT = 3_000
_MODEL = "claude-haiku-4-5-20251001"
_MULTI_BLANK = re.compile(r"\n{3,}")

# ── Trivial-reply detection ────────────────────────────────────────── #
# Signature / closing lines stripped before checking substance
_SIGNATURE_STRIP = re.compile(
    r"(?:"
    r"감사합니다\.?\s*$|"
    r"수고하세요\.?\s*$|"
    r"잘\s*부탁드립니다\.?\s*$|"
    r"\S{1,5}\s*드림\.?\s*$|"            # 이름 드림
    r"^-+\s*$|"                           # dashes
    r"^Sent from\s.*$|"
    r"^Regards,?\s*$|^Best,?\s*$|^Thanks,?\s*$"
    r")",
    re.MULTILINE | re.IGNORECASE,
)

_TRIVIAL_PHRASES = re.compile(
    r"^(?:"
    r"확인\s*바랍니다|확인\s*부탁드립니다|확인했습니다|확인\s*했습니다|"
    r"네[,.]?\s*확인했습니다|네[,.]?\s*확인\s*부탁드립니다|"
    r"네[,.]?\s*알겠습니다|알겠습니다|"
    r"넵|네|OK|ok|Yes|yes|"
    r"수신인\s*추가\s*(?:드립니다|합니다)|"
    r"참조\s*추가\s*(?:드립니다|합니다)|"
    r"확인|동의합니다|"
    r"공유\s*드립니다|전달\s*드립니다"
    r")\.?\s*$",
    re.IGNORECASE,
)


def _is_trivial_reply(text: str, min_substance: int = 30) -> bool:
    """Return True if *text* is just an acknowledgment / greeting / signature.

    Strips common closing lines first, then checks whether the remaining
    content is all trivial phrases or too short (< *min_substance* chars).
    """
    if not text or not text.strip():
        return True

    cleaned = _SIGNATURE_STRIP.sub("", text).strip()
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if not lines:
        return True

    if all(_TRIVIAL_PHRASES.match(ln) for ln in lines):
        return True

    return len("\n".join(lines)) < min_substance


# ── Reply-chain boundary patterns ──────────────────────────────────── #
_REPLY_CHAIN_PATTERNS = [
    # 1. Outlook / generic "-----Original Message-----"
    re.compile(r"^-{3,}\s*(?:Original Message|원본\s*메시지)\s*-{3,}", re.MULTILINE | re.IGNORECASE),
    # 2. Gmail "On ... wrote:" / Korean variant
    re.compile(r"^On\s.+wrote:\s*$|^.+에\s.+님이\s작성:\s*$", re.MULTILINE),
    # 3. Outlook header block (From/Sent/To/Subject — EN or KR)
    re.compile(
        r"^(?:From|보낸\s*사람)\s*:.*\n"
        r"(?:Sent|보낸\s*날짜)\s*:.*\n"
        r"(?:To|받는\s*사람)\s*:.*\n"
        r"(?:Subject|제목)\s*:",
        re.MULTILINE | re.IGNORECASE,
    ),
    # 4. Long separator lines (_____ or =====, 5+ chars) — skip '-' to avoid markdown HR false positives
    re.compile(r"^[_=]{5,}\s*$", re.MULTILINE),
    # 5. Quoted lines: 3+ consecutive '>' lines
    re.compile(r"(?:^>.*\n){3,}", re.MULTILINE),
]


def _extract_latest_reply(body_text: str | None, min_chars: int = 50) -> str:
    """Return only the most recent reply from an email chain.

    Scans *body_text* for reply-chain boundary markers and returns
    everything before the earliest one.  If the result is too short
    (< *min_chars*), extends to include the next section.  If no
    boundary is found the original text is returned unchanged.
    """
    if not body_text or not body_text.strip():
        return ""

    # Normalize line endings
    body_text = body_text.replace("\r\n", "\n").replace("\r", "\n")

    # Collect all boundary positions
    positions: list[int] = []
    for pat in _REPLY_CHAIN_PATTERNS:
        for m in pat.finditer(body_text):
            positions.append(m.start())

    if not positions:
        return _MULTI_BLANK.sub("\n\n", body_text)

    positions.sort()

    # Take text before earliest boundary
    first = positions[0]
    latest = body_text[:first].strip()

    # If too short, extend to second boundary (or full text)
    if len(latest) < min_chars:
        if len(positions) >= 2:
            latest = body_text[:positions[1]].strip()
        else:
            return _MULTI_BLANK.sub("\n\n", body_text)

    # If the latest reply is trivial (e.g. "확인바랍니다"), include the
    # next section so Claude can generate meaningful short_title/description.
    if _is_trivial_reply(latest):
        if len(positions) >= 2:
            latest = body_text[:positions[1]].strip()
        else:
            return _MULTI_BLANK.sub("\n\n", body_text)

    return _MULTI_BLANK.sub("\n\n", latest)

# Legacy single-purpose prompt (used by summarize())
_SUMMARY_PROMPT = """\
다음 업무 메일을 읽고 핵심 내용을 한국어 불릿포인트 3개로 요약해줘.
각 불릿은 1~2문장(30자 이상)으로 구체적으로 작성해.
불릿1: 핵심 주제와 배경, 불릿2: 요청사항·액션아이템, 불릿3: 마감일·수치·참고사항.
이메일 체인인 경우 가장 최근 회신을 중심으로 요약해.
불릿포인트만 출력하고 다른 설명은 넣지 마.

제목: {subject}
보낸 사람: {sender}

본문:
{body}"""

# Combined prompt — returns JSON with summary + assignees + priority + category
_ANALYZE_PROMPT = """\
다음 업무 메일을 분석해서 아래 JSON 형식으로만 출력해줘. 다른 말은 하지 마.

{{
  "short_title": "데이터검증 일일보고",
  "description": "CM360 4월 캠페인 데이터 검증 결과, 결측치 비율 2.3%로 이전 대비 개선됨. 금요일까지 피드백 요청.",
  "summary": ["- 핵심내용1", "- 핵심내용2"],
  "assignees": ["이름1", "이름2"],
  "priority": "보통",
  "category": "일반"
}}

규칙:
- short_title: 메일 핵심을 한국어로 15자 이내로 압축. RE:/FW:/회신:/전달: 접두사 제거, 날짜·코드·번호 생략, 핵심 주제어만 남겨라. 영어 키워드도 한국어로 번역해.
  - 나쁜 예: "FW: (2) [Daily Report] 데이터 검증_2026-04-20" (원본 제목 그대로)
  - 나쁜 예: "Affiliate Tracking 권" (영어 혼용, 단어 잘림)
  - 좋은 예: "데이터검증 일일보고" (핵심만 한국어 15자 이내)
  - 좋은 예: "어필리에이트 트래킹 요청" (영어→한국어 번역)
- description: 메일 전체 내용을 100자 이내 한 문장~두 문장으로 요약. 구체적 수치·일정·요청사항 포함.
- summary: 한국어 불릿 3개, 각 불릿은 반드시 1~2문장(30자 이상)으로 구체적으로 작성. 앞에 "- " 붙여서.
  - 불릿1: 메일의 핵심 주제와 배경 (무엇에 대한 메일인지)
  - 불릿2: 요청사항·액션아이템·결정사항 (누가 무엇을 해야 하는지)
  - 불릿3: 마감일·일정·수치 등 구체적 정보 (없으면 현재 진행 상황이나 참고사항)
  - 나쁜 예: "- 보고서 검토 요청" (너무 짧고 추상적)
  - 좋은 예: "- CM360 4월 캠페인 성과 보고서 초안이 첨부되어 있으며, 금주 금요일까지 검토 후 피드백 요청"
- assignees: 메일 본문/제목에 언급된 담당자 이름만 (직함·호칭 제외). 없으면 []
- priority: 메일 긴급도 → "긴급" (당일·즉시 처리 필요) | "보통" (수일 내) | "낮음" (여유 있음)
- category: 메일 성격 → "보고" | "승인요청" | "공지" | "미팅" | "일반"
- 본문이 이메일 체인(RE: RE:)인 경우, 가장 최근 회신 내용을 중심으로 분석해. 인용된 이전 메시지는 맥락 참고만 해.

제목: {subject}
보낸 사람: {sender}

본문:
{body}"""


def analyze_email(
    subject: str,
    sender: str,
    body_text: str,
    api_key: str,
    to: str = "",
    cc: str = "",
) -> AnalysisResult:
    """Single Claude call → AnalysisResult (summary, assignees, priority, category).

    Returns default AnalysisResult on failure or missing key — never raises.
    Prefer this over separate summarize() + extract_assignees() calls.
    """
    if not api_key or not (body_text or "").strip():
        return AnalysisResult(summary=_fallback_summary(body_text))

    try:
        cleaned_body = _extract_latest_reply(body_text)
        prompt = _ANALYZE_PROMPT.format(
            subject=subject,
            sender=sender,
            body=cleaned_body[:_BODY_CHAR_LIMIT],
        )
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()

        # Strip markdown code fences if Claude wraps in ```json ... ```
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)

        summary_bullets = data.get("summary", [])
        # Claude occasionally returns a plain string instead of a list
        if isinstance(summary_bullets, str):
            summary_bullets = [summary_bullets] if summary_bullets.strip() else []
        summary = "\n".join(
            b if b.startswith("- ") else f"- {b}" for b in summary_bullets
        )
        raw_assignees = [normalize_name(str(a).strip()) for a in data.get("assignees", []) if str(a).strip()]
        assignees = [n for n in raw_assignees if is_valid_assignee(n)]
        if len(assignees) < len(raw_assignees):
            log.warning("invalid assignee names filtered: %s", [n for n in raw_assignees if not is_valid_assignee(n)])
        # Claude가 담당자를 못 찾으면 To/CC 이메일에서 fallback
        if not assignees and (to or cc):
            assignees = extract_assignees_from_email(to, cc)
        priority = data.get("priority", "보통") if data.get("priority") in ("긴급", "보통", "낮음") else "보통"
        category = data.get("category", "일반") if data.get("category") in ("보고", "승인요청", "공지", "미팅", "일반") else "일반"
        short_title = str(data.get("short_title", "")).strip()[:30]
        description = str(data.get("description", "")).strip()[:100]

        log.info("email analyzed", extra={
            "summary_lines": len(summary_bullets),
            "assignees": assignees,
            "priority": priority,
            "category": category,
        })
        return AnalysisResult(
            summary=summary, assignees=assignees, priority=priority,
            category=category, short_title=short_title,
            description=description, source="claude",
        )

    except Exception as exc:
        log.warning("analyze_email failed -- using fallback summary: %s", repr(exc))
        return AnalysisResult(summary=_fallback_summary(body_text))


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
        cleaned_body = _extract_latest_reply(body_text)
        prompt = _SUMMARY_PROMPT.format(
            subject=subject,
            sender=sender,
            body=cleaned_body[:_BODY_CHAR_LIMIT],
        )
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = message.content[0].text.strip()
        log.info("email summarized", extra={"chars": len(summary)})
        return summary

    except Exception as exc:
        log.warning("summarize failed - skipping: %s", repr(exc))
        return ""
