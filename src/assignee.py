"""Assignee extractor for daily digest.

Two-step approach:
  1. Regex scan for Korean name+title patterns (e.g. 박은진대리님, 이해랑팀장님)
  2. Claude fallback when no names found — infers likely assignees from context

Returns a deduplicated list of name strings (titles stripped).
"""
from __future__ import annotations

import logging
import re

import anthropic

log = logging.getLogger(__name__)

# Matches: Korean name (2-4 chars) + optional space + title + optional 님
_NAME_RE = re.compile(
    r"([가-힣]{2,4})\s*(?:대리|팀장|과장|부장|사원|주임|차장|이사|선임|수석|파트장|그룹장)님?"
)

# 키워드 → 풀네임 매핑 (텍스트에 키워드가 포함되면 해당 풀네임으로 매칭)
_NICKNAME_MAP: dict[str, str] = {
    # 한글 닉네임/이름 → 풀네임
    "해랑": "이해랑",
    "자명": "이자명",
    "원영": "최원영",
    "찬우": "송찬우",
    "기정": "이기정",
    "은진": "박은진",
    "동규": "이동규",
    "혜령": "정혜령",
    "효수": "이효수",
    "치성": "김치성",
    "양규": "문양규",
    "종화": "윤종화",
    "민지": "심민지",
    "태혁": "이태혁",
    "종표": "김종표",
    "차유나": "차윤나",
    "유나": "차윤나",
    "윤나": "차윤나",
    "이혜랑": "이해랑",
    "김기정": "이기정",
    "원영대": "최원영",
    "예지": "권예지",
    "찬희": "염찬희",
    "혜진": "박혜진",
    # 직함 첫 글자가 성으로 오인되는 패턴 방어
    "차기정": "이기정",    # "기정 차장님" → 오탐 "차기정"
    "팀해랑": "이해랑",    # "해랑 팀장님" → 오탐 "팀해랑"
    "대원영": "최원영",    # "원영 대리님" → 오탐 "대원영"
    "차해랑": "이해랑",    # "해랑 차장님" 등
    "이해량": "이해랑",    # 오타
    "이기종": "이기정",    # 오타
}

# 영문 이름 → 풀네임 매핑 (case-insensitive로 검색)
_ENGLISH_NAME_MAP: dict[str, str] = {
    "haerang": "이해랑",
    "jamyeong": "이자명",
    "ja myeong": "이자명",
    "eunjin": "박은진",
    "eun jin": "박은진",
    "dongkyu": "이동규",
    "dong kyu": "이동규",
    "hyesu": "이효수",
    "hyosu": "이효수",
    "chisung": "김치성",
    "chi sung": "김치성",
    "jonghwa": "윤종화",
    "jong hwa": "윤종화",
    "jessie": "윤종화",
    "wongyoung": "최원영",
    "won young": "최원영",
    "hyeryeong": "정혜령",
    "hye ryeong": "정혜령",
    "hannah": "정혜령",
    "yanggyu": "문양규",
    "yang gyu": "문양규",
    "taehyeok": "이태혁",
    "tae hyeok": "이태혁",
    "minji": "심민지",
    "min ji": "심민지",
    "jongpyo": "김종표",
    "jong pyo": "김종표",
    "gijeong": "이기정",
    "gi jeong": "이기정",
    "kijeong": "이기정",
    "ki jeong": "이기정",
    "yuna": "차윤나",
    "yun na": "차윤나",
    "chanwoo": "송찬우",
    "chan woo": "송찬우",
    "kyungseok": "김경석",
    "kyung seok": "김경석",
    "juwon": "김주원",
    "ju won": "김주원",
    "buyoung": "곽부영",
    "bailey": "곽부영",
    "yeji": "권예지",
    "ye ji": "권예지",
    "yejikwon": "권예지",
}

# 일반 단어인데 2~3글자 한글이라 이름으로 오탐되는 블랙리스트
_BLACKLIST: set[str] = {
    "관련", "예전에", "이전에", "현재는", "그리고", "하지만", "그래서",
    "때문에", "대해서", "위해서", "통해서", "따라서", "가능한", "필요한",
    "확인이", "진행이", "처리가", "요청이", "문의가", "검토가", "공유가",
    "데이터", "시스템", "프로젝트", "이슈가", "결과가", "내용이",
    "이메일", "메일이", "파일이", "항목이", "건에서", "경우에",
    "예전에는", "이전에는", "현재는",
    "데이트는", "데이트", "미팅은", "미팅이",
    # 본문에서 자주 오탐되는 동사/부사/명사
    "맞는지", "아마도", "아마", "확인해", "부탁해", "대시보",
    "로직이", "대해서", "같은데", "있는지", "없는지", "하는지",
    "판단로", "공유해", "전달해", "요청해", "건이라",
}

# 이메일 주소 prefix → 풀네임 매핑 (To/CC에서 담당자 추출)
_EMAIL_MAP: dict[str, str] = {
    "hrlee": "이해랑",
    "jmlee": "이자명",
    "wychoi": "최원영",
    "cwsong": "송찬우",
    "kjlee": "이기정",
    "ejpark": "박은진",
}

_INFER_PROMPT = """\
이 업무 메일에서 조치나 확인이 필요한 담당자를 추론해줘.
우리 팀원 목록: 이해랑, 이기정, 박은진, 이자명, 최원영, 송찬우, 이효수
이 중에서 해당되는 사람만 골라줘. 1~2명만.
이름만 쉼표로 구분해서 한 줄로 출력해. 없으면 아무것도 출력하지 마.
예시: 박은진, 이해랑

제목: {subject}
보낸 사람: {sender}

본문:
{body}"""

_BODY_CHAR_LIMIT = 2_000
_MODEL = "claude-haiku-4-5-20251001"

# 유효한 한국어 이름: 완성형 음절(가-힣) 2~3자
_VALID_NAME_RE = re.compile(r"^[가-힣]{2,3}$")

# 알려진 이름 집합 (닉네임 매핑 + 이메일 매핑 + 영문 매핑 값)
_KNOWN_NAMES: set[str] = set(_NICKNAME_MAP.values()) | set(_EMAIL_MAP.values()) | set(_ENGLISH_NAME_MAP.values())


def is_valid_assignee(name: str) -> bool:
    """Check if *name* looks like a real Korean person name.

    Accepts:
    - Known team members (from nickname/email maps)
    - 2~3 syllable Korean names (가-힣) not in blacklist

    Rejects garbage like 'ㅋㅋㅋ', '관련', '예전에는', '#태그', empty strings, etc.
    """
    if not name:
        return False
    if name in _BLACKLIST:
        return False
    if name in _KNOWN_NAMES:
        return True
    return bool(_VALID_NAME_RE.match(name))


def extract_assignees_from_email(to: str, cc: str) -> list[str]:
    """Extract assignee name from the first matching To/CC email address."""
    for addr in (to + "," + cc).split(","):
        addr = addr.strip().lower()
        if not addr:
            continue
        # "Name <email>" 또는 "email" 형태에서 @ 앞 prefix 추출
        if "<" in addr and ">" in addr:
            addr = addr.split("<")[1].split(">")[0]
        prefix = addr.split("@")[0] if "@" in addr else ""
        if prefix in _EMAIL_MAP:
            return [_EMAIL_MAP[prefix]]
    return []


def extract_names_from_recipients(recipient_str: str) -> list[str]:
    """Extract assignee names from a raw recipient string (To or CC header value).

    Handles formats like:
      - "예지 <yj@company.com>; 이해랑 <hrlee@company.com>"
      - "yj@company.com; hrlee@company.com"
      - "예지; 이해랑"
      - "Park Yeji <yj@company.com>"

    Strategy:
      1. Try email prefix mapping via _EMAIL_MAP
      2. Try display name mapping via normalize_name() + is_valid_assignee()
      3. Try English name mapping via _ENGLISH_NAME_MAP
    """
    if not recipient_str or not recipient_str.strip():
        return []

    names: list[str] = []
    seen: set[str] = set()
    # Split by ; or , (common separators in To/CC)
    parts = re.split(r"[;,]", recipient_str)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        display_name = ""
        email_addr = ""

        # Parse "Display Name <email@domain>" format
        angle_match = re.match(r"(.+?)\s*<([^>]+)>", part)
        if angle_match:
            display_name = angle_match.group(1).strip().strip("'\"")
            email_addr = angle_match.group(2).strip().lower()
        elif "@" in part:
            email_addr = part.strip().lower()
        else:
            display_name = part.strip()

        # 1. Try email prefix mapping
        if email_addr:
            prefix = email_addr.split("@")[0]
            if prefix in _EMAIL_MAP and _EMAIL_MAP[prefix] not in seen:
                names.append(_EMAIL_MAP[prefix])
                seen.add(_EMAIL_MAP[prefix])
                continue

        # 2. Try display name — Korean name or nickname
        if display_name:
            normalized = normalize_name(display_name)
            if is_valid_assignee(normalized) and normalized not in seen:
                names.append(normalized)
                seen.add(normalized)
                continue
            # Try English name mapping
            lower_name = display_name.lower().strip()
            if lower_name in _ENGLISH_NAME_MAP and _ENGLISH_NAME_MAP[lower_name] not in seen:
                names.append(_ENGLISH_NAME_MAP[lower_name])
                seen.add(_ENGLISH_NAME_MAP[lower_name])
                continue

    return names


def extract_assignees(
    subject: str,
    sender: str,
    body_text: str,
    api_key: str,
    to: str = "",
    cc: str = "",
) -> list[str]:
    """Return deduplicated list of assignee names, or [] if none found.

    Priority order:
      1. TO recipients (highest priority — the person who received the email)
      2. Subject regex (folder명에 수신자 포함됨)
      3. Claude inference
      4. Full body regex (최종 fallback — 오탐 가능성 있으므로 마지막에만)
      5. CC recipients

    Never raises.
    """
    # Step 1: TO recipients — highest priority
    to_names = extract_names_from_recipients(to) if to else []
    if to_names:
        # Merge with subject regex (not full body)
        subject_names = _regex_extract(subject)
        merged = list(dict.fromkeys(to_names + subject_names))
        log.info("assignees extracted from TO + subject", extra={"names": merged})
        return merged

    # Step 2: subject-only regex (folder명에 수신자 이름 포함)
    subject_names = _regex_extract(subject)
    if subject_names:
        log.info("assignees extracted from subject", extra={"names": subject_names})
        return subject_names

    # Step 3: Claude inference
    if api_key:
        inferred = _claude_infer(subject, sender, body_text, api_key)
        if inferred:
            return inferred

    # Step 4: Full body regex (최종 fallback)
    body_names = _regex_extract(subject + " " + body_text)
    if body_names:
        log.info("assignees extracted from body regex", extra={"names": body_names})
        return body_names

    # Step 5: CC fallback
    cc_names = extract_names_from_recipients(cc) if cc else []
    if cc_names:
        log.info("assignees extracted from CC", extra={"names": cc_names})
        return cc_names

    return []


def normalize_name(name: str) -> str:
    """Map a nickname/partial name to full name if known.

    Also strips common suffixes (님, 씨, 프로) and checks English name map.
    """
    # 1. 영문 이름 매핑 (case-insensitive)
    lower = name.lower().strip()
    if lower in _ENGLISH_NAME_MAP:
        return _ENGLISH_NAME_MAP[lower]

    # 2. 접미사 제거 후 한글 매핑 시도 (프로님, 매니저님 등 이중 접미사 포함)
    stripped = re.sub(
        r"(프로님|매니저님|대리님|팀장님|과장님|부장님|차장님|이사님|주임님|선임님|수석님|파트장님|그룹장님"
        r"|님|씨|프로|매니저|대리|팀장|과장|부장|차장|이사|주임|선임|수석|파트장|그룹장)$",
        "", name,
    )
    if stripped in _NICKNAME_MAP:
        return _NICKNAME_MAP[stripped]
    if name in _NICKNAME_MAP:
        return _NICKNAME_MAP[name]
    # 접미사만 제거된 결과라도 유효하면 반환
    if stripped != name and stripped in _KNOWN_NAMES:
        return stripped
    return stripped if stripped else name


def _regex_extract(text: str) -> list[str]:
    """Extract unique names from Korean name+title patterns and nickname map."""
    seen: dict[str, None] = {}
    # 1) 풀네임+직함 패턴 (예: 이해랑팀장님)
    for match in _NAME_RE.finditer(text):
        name = normalize_name(match.group(1))
        if is_valid_assignee(name):
            seen[name] = None
    # 2) 키워드 단순 포함 매칭 (예: 해랑, 자명프로님, 해랑 팀장님 등 전부)
    for keyword, fullname in _NICKNAME_MAP.items():
        if keyword in text:
            seen[fullname] = None
    return list(seen)


def _claude_infer(
    subject: str,
    sender: str,
    body_text: str,
    api_key: str,
) -> list[str]:
    """Ask Claude to infer assignees when regex finds nothing."""
    try:
        prompt = _INFER_PROMPT.format(
            subject=subject,
            sender=sender,
            body=body_text[:_BODY_CHAR_LIMIT],
        )
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if not raw:
            return []
        names = [normalize_name(n.strip()) for n in raw.split(",") if n.strip()]
        valid = [n for n in names if is_valid_assignee(n)]
        if len(valid) < len(names):
            log.warning("invalid assignee names filtered out: %s", [n for n in names if not is_valid_assignee(n)])
        log.info("assignees inferred by Claude", extra={"names": valid})
        return valid
    except Exception as exc:
        log.warning("assignee inference failed: %s", repr(exc))
        return []
