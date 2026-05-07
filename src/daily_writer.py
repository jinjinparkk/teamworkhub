"""Daily digest Markdown composer.

Generates a single Obsidian-compatible Daily Note that aggregates all
overnight emails (18:00 ~ 08:59) into one readable page.

Output filename pattern: YYYY-MM-DD.md  (Obsidian Daily Notes format)

Usage
─────
  from src.daily_writer import compose_daily, filename_for_date, parse_checked_items

  md = compose_daily(messages_with_analysis, "2025-04-02",
                     "2025-04-01 18:00", "2025-04-02 08:59", "Asia/Seoul")
  name = filename_for_date("2025-04-02")   # "2025-04-02.md"
"""
from __future__ import annotations

import re
from datetime import date as _date
from typing import TYPE_CHECKING

from src.config import KNOWN_ASSIGNEES
from src.md_writer import filename_for_subject, _extract_sender_name

if TYPE_CHECKING:
    from src.gmail_client import ParsedMessage
    from src.summarizer import AnalysisResult


def _normalize_assignee(name: str) -> str | None:
    """Map a name to a KNOWN_ASSIGNEES entry. Returns None if no match."""
    if name in KNOWN_ASSIGNEES:
        return name
    # Fuzzy: find a known assignee sharing 2+ consecutive characters
    for member in KNOWN_ASSIGNEES:
        for i in range(len(name) - 1):
            if name[i:i+2] in member:
                return member
    return None


def _clean_assignees(names: list[str]) -> list[str]:
    """Filter and auto-correct assignee names against TEAM_MEMBERS."""
    result: list[str] = []
    seen: set[str] = set()
    for name in names:
        corrected = _normalize_assignee(name)
        if corrected and corrected not in seen:
            result.append(corrected)
            seen.add(corrected)
    return result

# Prefixes to strip when normalising email subjects for thread detection
_THREAD_PREFIX_RE = re.compile(
    r"^(?:re|fw|fwd|회신|전달|답장|답변)\s*:\s*", flags=re.IGNORECASE
)

# 요일별 정기 업무 (0=월 ~ 4=금, 5=토/6=일은 항목 없음)
_RECURRING_TASKS: dict[int, str] = {
    0: "RPA",
    1: "로직점검",
    2: "수정기",
    3: "목정기",
    4: "금정기",
}

# Regex to extract wiki-link targets from checked checkbox lines.
# Use non-greedy .*? to handle subjects containing literal ] (e.g. [Follow-up]).
_CHECKED_RE = re.compile(r"^-\s*\[[xX]\]\s*\[\[(.*?)(?:\]\]|\|)", re.MULTILINE)

# Regex to extract ALL wiki-link targets (the part before | if present).
# Matches from [[ to the first ]] or |, allowing ] inside the target.
_WIKI_LINK_RE = re.compile(r"\[\[(.*?)(?:\]\]|\|)")

# Regex helpers for frontmatter parsing.
_FM_ASSIGNEES_RE = re.compile(r"^assignees:\s*\[(.+?)\]", re.MULTILINE)
_FM_NAME_RE = re.compile(r"""['"]([^'"]+)['"]""")


def parse_checked_items(content: str) -> set[str]:
    """Extract wiki-link targets from checked (``[x]``) checkbox lines.

    Given a Daily Note's Markdown content, returns a set of wiki-link names
    (without folder prefix or display text) that the user has manually
    checked off in Obsidian.

    Example matched line::

        - [x] [[TeamWorkHub/업무 보고|업무 보고]] #박은진

    Returns ``{"TeamWorkHub/업무 보고"}`` (the part before ``|``).
    """
    return set(_CHECKED_RE.findall(content))


def filename_for_date(date_str: str) -> str:
    """Return the Daily Note filename for *date_str* (``YYYY-MM-DD``).

    Example: "2025-04-02" → "2025-04-02.md"
    """
    return f"{date_str}.md"


def _normalise_subject(subject: str) -> str:
    """Strip RE:/FW: prefixes repeatedly until none remain."""
    s = subject.strip()
    while True:
        new = _THREAD_PREFIX_RE.sub("", s).strip()
        if new == s:
            return s.lower()
        s = new


# Words too short or too common to be meaningful for subject similarity.
_SIMILARITY_MIN_WORD_LEN = 3
# Date-like tokens (YYYY-MM-DD, YYMMDD) are stripped by this regex.
_DATE_TOKEN_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{6})\b")


def _subject_keywords(subject: str) -> set[str]:
    """Extract meaningful keywords from an email subject for similarity check."""
    s = _normalise_subject(subject)
    s = _DATE_TOKEN_RE.sub("", s)
    return {w for w in s.split() if len(w) >= _SIMILARITY_MIN_WORD_LEN}


def _find_similar_wiki(wiki_name: str, sender: str,
                       seen: dict[str, tuple[str, set[str]]]) -> str | None:
    """Check if *wiki_name* is similar to any already-seen wiki entry.

    *seen* maps wiki_name → (sender, keywords).
    Returns the matching wiki_name if similar, else None.
    """
    kw_new = _subject_keywords(wiki_name)
    if len(kw_new) < 2:
        return None
    sender_norm = sender.strip().lower()
    for existing_wiki, (existing_sender, existing_kw) in seen.items():
        if existing_sender != sender_norm:
            continue
        common = kw_new & existing_kw
        if len(common) >= 2:
            return existing_wiki
    return None


def compose_daily(
    messages: list[tuple["ParsedMessage", "AnalysisResult"]],
    date_str: str,
    period_start: str,
    period_end: str,
    timezone_name: str = "Asia/Seoul",
    daily_folder: str = "TeamWorkHub_Daily",
    note_folder: str = "",
) -> str:
    """Return a Daily Note Markdown string aggregating overnight emails.

    Args:
        messages:      List of (ParsedMessage, AnalysisResult) pairs.
        date_str:      ISO date of the digest day, e.g. "2025-04-02".
        period_start:  Human-readable start of the collection window.
        period_end:    Human-readable end of the collection window.
        timezone_name: Timezone label shown in the note header.
        daily_folder:  Obsidian folder name for daily notes (used in Dataview
                       queries). Defaults to "TeamWorkHub_Daily".
        note_folder:   Obsidian folder name for individual email notes. When set,
                       wiki-links include the folder path so Obsidian resolves
                       cross-folder links correctly (e.g. "TeamWorkHub/제목").

    Returns a UTF-8 string ready to be written as a .md file.
    """
    tz_short = timezone_name.split("/")[-1] if "/" in timezone_name else timezone_name
    count = len(messages)
    note_date = _date.fromisoformat(date_str)

    lines: list[str] = []

    # ── Today's work ────────────────────────────────────────────────── #
    # Build To-do items first, then derive frontmatter from displayed items only.
    todo_lines: list[str] = []
    all_assignees: set[str] = set()
    has_urgent = False

    if messages:
        seen_wiki: dict[str, tuple[str, set[str]]] = {}  # wiki_name → (sender, keywords)
        for msg, ar in messages:
            subject = msg.subject or "(제목 없음)"
            wiki_name = filename_for_subject(subject).removesuffix(".md")
            sender = getattr(msg, "sender", "") or ""
            # Exact match dedup
            if wiki_name in seen_wiki:
                continue
            # Subject similarity dedup (same sender + 2+ common keywords)
            if _find_similar_wiki(wiki_name, sender, seen_wiki):
                continue
            seen_wiki[wiki_name] = (sender.strip().lower(), _subject_keywords(wiki_name))
            display = ar.short_title or wiki_name
            if note_folder:
                wiki_target = f"{note_folder}/{wiki_name}"
                wiki_link = f"{wiki_target}|{display}"
            else:
                wiki_target = wiki_name
                wiki_link = f"{wiki_name}|{display}" if ar.short_title else wiki_name
            # Use action_items assignees first, fallback to ar.assignees
            if ar.action_items:
                ai_assignees = list(dict.fromkeys(
                    item.get("assignee", "") for item in ar.action_items
                    if item.get("assignee")
                ))
                cleaned = _clean_assignees(ai_assignees) or _clean_assignees(ar.assignees)
            else:
                cleaned = _clean_assignees(ar.assignees)
            tag_parts = [f"#{a}" for a in cleaned] if cleaned else ["#미지정"]
            sender_tag = _extract_sender_name(sender).replace(' ', '_')
            if sender_tag:
                tag_parts.append(f"#{sender_tag}")
            tags = " ".join(tag_parts)
            todo_lines.append(f"- [[{wiki_link}]] {tags}")
            all_assignees.update(cleaned)
            if ar.priority == "긴급":
                has_urgent = True

    # ── YAML frontmatter──────────────────────────────────────────────── #
    sorted_assignees = sorted(all_assignees)

    lines.append("---")
    lines.append("Type: daily_note")
    lines.append(f"date: {date_str}")
    lines.append(f'period: "{period_start} ~ {period_end} ({tz_short})"')
    lines.append(f"email_count: {count}")
    if sorted_assignees:
        lines.append(f"assignees: {sorted_assignees}")
    else:
        lines.append("assignees: []")
    lines.append(f"has_urgent: {str(has_urgent).lower()}")
    lines.append("---")
    lines.append("")

    lines.append("### Today's work")
    lines.append("#### To do list")
    if todo_lines:
        lines.extend(todo_lines)
    else:
        lines.append("- (없음)")

    lines.append("")

    # ── 업무 상세 (Dataview: individual note tasks for this date) ──── #
    lines.append("#### 업무 상세")
    lines.append("```dataview")
    lines.append("  TASK")
    lines.append('  WHERE contains(file.name, dateformat(this.file.day, "yyyy-MM-dd"))')
    lines.append("    AND file.name != this.file.name")
    lines.append("  GROUP BY file.link")
    lines.append("```")
    lines.append("")

    # Static sections
    lines.append("#### 정기적인 일")
    recurring = _RECURRING_TASKS.get(note_date.weekday())
    if recurring:
        lines.append(f"- {recurring}")
    else:
        lines.append("- (없음)")
    lines.append("")

    # ── 미완료 (Dataview) ───────────────────────────────────────────── #
    _dv_folder = note_folder or daily_folder
    lines.append("### 미완료")
    lines.append("")
    lines.append("```dataview")
    lines.append(f'TASK FROM "{_dv_folder}"')
    lines.append('WHERE !completed AND date >= date(today) - dur(14d) AND text != ""')
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


# ── Incremental merge helpers ──────────────────────────────────────── #

def _extract_wiki_links(content: str) -> set[str]:
    """Extract all wiki-link targets from Markdown content.

    Matches ``[[target]]`` and ``[[target|display]]``, returning
    the *target* part (before ``|``).
    """
    return set(_WIKI_LINK_RE.findall(content))


def _update_frontmatter(
    content: str,
    *,
    email_count: int,
    period: str,
    assignees: list[str],
    has_urgent: bool,
) -> str:
    """Update specific frontmatter fields, preserving everything else.

    Only touches ``email_count``, ``period``, ``assignees``, and
    ``has_urgent``.  All other lines are kept as-is.
    """
    lines = content.split("\n")

    # Find frontmatter boundaries (first and second ``---``)
    fm_start = -1
    fm_end = -1
    for i, line in enumerate(lines):
        if line.strip() == "---":
            if fm_start == -1:
                fm_start = i
            else:
                fm_end = i
                break

    if fm_start == -1 or fm_end == -1:
        return content  # no valid frontmatter — return unchanged

    for i in range(fm_start + 1, fm_end):
        line = lines[i]
        if line.startswith("email_count:"):
            lines[i] = f"email_count: {email_count}"
        elif line.startswith("period:"):
            lines[i] = f'period: "{period}"'
        elif line.startswith("assignees:"):
            lines[i] = f"assignees: {assignees}" if assignees else "assignees: []"
        elif line.startswith("has_urgent:"):
            lines[i] = f"has_urgent: {str(has_urgent).lower()}"

    return "\n".join(lines)


def merge_daily(
    existing_content: str,
    messages: list[tuple["ParsedMessage", "AnalysisResult"]],
    period_start: str,
    period_end: str,
    timezone_name: str = "Asia/Seoul",
    note_folder: str = "",
) -> str:
    """Merge new email items into an existing Daily Note.

    Preserves all user edits: check states (``[x]``), manually added lines,
    recurring tasks, Dataview section, and free-form notes.

    Only two things change:

    1. Frontmatter fields (``email_count``, ``period``, ``assignees``,
       ``has_urgent``) are updated.
    2. New To-do items are appended for emails not already wiki-linked.

    Args:
        existing_content: Current daily note Markdown content.
        messages:         All (ParsedMessage, AnalysisResult) pairs from this run.
        period_start:     Human-readable period start label.
        period_end:       Human-readable period end label.
        timezone_name:    Timezone for the period label.
        note_folder:      Obsidian folder prefix for wiki-links.

    Returns the updated Markdown content.
    """
    tz_short = (
        timezone_name.split("/")[-1] if "/" in timezone_name else timezone_name
    )

    # ── Detect existing wiki-links ───────────────────────────────────── #
    existing_links = _extract_wiki_links(existing_content)
    existing_base_names: set[str] = set()
    for link in existing_links:
        if "/" in link:
            existing_base_names.add(link.rsplit("/", 1)[1])
        else:
            existing_base_names.add(link)

    # ── Build new To-do items ────────────────────────────────────────── #
    new_items: list[str] = []
    all_msg_assignees: set[str] = set()
    any_urgent = False
    seen_wiki: dict[str, tuple[str, set[str]]] = {}  # wiki_name → (sender, keywords)

    for msg, ar in messages:
        subject = msg.subject or "(제목 없음)"
        wiki_name = filename_for_subject(subject).removesuffix(".md")
        sender = getattr(msg, "sender", "") or ""

        # Exact match dedup
        if wiki_name in seen_wiki:
            continue
        # Subject similarity dedup (same sender + 2+ common keywords)
        if _find_similar_wiki(wiki_name, sender, seen_wiki):
            continue
        seen_wiki[wiki_name] = (sender.strip().lower(), _subject_keywords(wiki_name))

        wiki_target = f"{note_folder}/{wiki_name}" if note_folder else wiki_name

        # Use action_items assignees first, fallback to ar.assignees
        if ar.action_items:
            ai_assignees = list(dict.fromkeys(
                item.get("assignee", "") for item in ar.action_items
                if item.get("assignee")
            ))
            cleaned = _clean_assignees(ai_assignees) or _clean_assignees(ar.assignees)
        else:
            cleaned = _clean_assignees(ar.assignees)

        if wiki_target in existing_links or wiki_name in existing_base_names:
            # Already in the note — still count assignees/urgency for frontmatter
            all_msg_assignees.update(cleaned)
            if ar.priority == "긴급":
                any_urgent = True
            continue

        # Build wiki-link line (no checkbox)
        display = ar.short_title or wiki_name
        if note_folder:
            wiki_link = f"{wiki_target}|{display}"
        else:
            wiki_link = (
                f"{wiki_name}|{display}" if ar.short_title else wiki_name
            )
        tag_parts = [f"#{a}" for a in cleaned] if cleaned else ["#미지정"]
        sender_tag = _extract_sender_name(sender).replace(' ', '_')
        if sender_tag:
            tag_parts.append(f"#{sender_tag}")
        tags = " ".join(tag_parts)
        new_items.append(f"- [[{wiki_link}]] {tags}")

        # Only count assignees/urgency for items that actually appear in To-do
        all_msg_assignees.update(cleaned)
        if ar.priority == "긴급":
            any_urgent = True

    # ── Insert new items into To-do list section ─────────────────────── #
    lines = existing_content.split("\n")
    if new_items:
        todo_start = -1
        todo_end = len(lines)
        for i, line in enumerate(lines):
            if todo_start == -1 and line.strip() == "#### To do list":
                todo_start = i
                continue
            if todo_start != -1 and (
                line.startswith("#### ") or line.startswith("### ")
            ):
                todo_end = i
                break

        if todo_start != -1:
            # Remove bare placeholder (0-message initial note)
            for i in range(todo_start + 1, todo_end):
                if lines[i].strip() in ("- [ ]", "- (없음)"):
                    lines.pop(i)
                    todo_end -= 1
                    break

            # Find last non-blank line in section for insertion point
            insert_at = todo_start + 1
            for i in range(todo_end - 1, todo_start, -1):
                if lines[i].strip():
                    insert_at = i + 1
                    break

            for j, item in enumerate(new_items):
                lines.insert(insert_at + j, item)

    # Migrate existing checkbox lines to plain list items
    _checkbox_re = re.compile(r'^(\s*)-\s*\[[xX ]\]\s*(.*)$')
    for i, line in enumerate(lines):
        m = _checkbox_re.match(line)
        if m and "[[" in line:
            lines[i] = f"{m.group(1)}- {m.group(2)}"

    # Ensure "업무 상세" section exists (migrate from Detailed_list or add new)
    has_detail_section = any(
        line.strip() in ("#### 업무 상세", "#### Detailed_list")
        for line in lines
    )
    if not has_detail_section:
        # Insert before "#### 정기적인 일"
        insert_idx = None
        for i, line in enumerate(lines):
            if line.strip() == "#### 정기적인 일":
                insert_idx = i
                break
        if insert_idx is not None:
            detail_lines = [
                "#### 업무 상세",
                "```dataview",
                "  TASK",
                '  WHERE contains(file.name, dateformat(this.file.day, "yyyy-MM-dd"))',
                "    AND file.name != this.file.name",
                "  GROUP BY file.link",
                "```",
                "",
            ]
            for j, dl in enumerate(detail_lines):
                lines.insert(insert_idx + j, dl)

    # Rename "Detailed_list" to "업무 상세" if present
    for i, line in enumerate(lines):
        if line.strip() == "#### Detailed_list":
            lines[i] = "#### 업무 상세"

    # Migrate old Dataview queries: TeamWorkHub_Daily → note_folder (TeamWorkHub)
    _dv_folder = note_folder or "TeamWorkHub"
    for i, line in enumerate(lines):
        if 'TASK FROM "TeamWorkHub_Daily"' in line:
            lines[i] = line.replace(
                'TASK FROM "TeamWorkHub_Daily"', f'TASK FROM "{_dv_folder}"'
            )
        # Also fix old WHERE clause (date(file.name) → date)
        if "date(file.name)" in line and "dateformat" not in line:
            lines[i] = line.replace("date(file.name)", "date")

    result = "\n".join(lines)

    # ── Compute merged frontmatter values ────────────────────────────── #
    existing_assignees: set[str] = set()
    fm_match = _FM_ASSIGNEES_RE.search(existing_content)
    if fm_match:
        existing_assignees = set(_FM_NAME_RE.findall(fm_match.group(1)))
    merged_assignees = sorted(existing_assignees | all_msg_assignees)

    existing_urgent = bool(
        re.search(r"^has_urgent:\s*true", existing_content, re.MULTILINE)
    )

    existing_count = 0
    count_match = re.search(r"^email_count:\s*(\d+)", existing_content, re.MULTILINE)
    if count_match:
        existing_count = int(count_match.group(1))
    total_count = existing_count + len(new_items)

    period = f"{period_start} ~ {period_end} ({tz_short})"

    result = _update_frontmatter(
        result,
        email_count=total_count,
        period=period,
        assignees=merged_assignees,
        has_urgent=existing_urgent or any_urgent,
    )

    return result
