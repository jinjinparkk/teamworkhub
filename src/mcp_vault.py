"""TeamWorkHub Obsidian Vault — MCP server for Claude Desktop."""

from __future__ import annotations

import os
import re
from datetime import date, timedelta
from pathlib import Path

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VAULT_ROOT = Path(
    os.environ.get("OBSIDIAN_VAULT_PATH", "G:/공유 드라이브/obsidian/artience_pm3")
)

ALLOWED_FOLDERS = {
    "TeamWorkHub",
    "TeamWorkHub_Daily",
    "TeamWorkHub_Weekly",
    "TeamWorkHub_Monthly",
    "TeamWorkHub_Dashboard",
}

BLOCKED_PATTERNS = {".obsidian", "template", "TeamWorkHub_Backup"}

mcp = FastMCP("teamworkhub-vault")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_folder(folder: str) -> Path:
    """Validate *folder* against the whitelist and return its absolute path."""
    if folder not in ALLOWED_FOLDERS:
        raise ValueError(
            f"'{folder}' is not an allowed folder. "
            f"Choose from: {', '.join(sorted(ALLOWED_FOLDERS))}"
        )
    return VAULT_ROOT / folder


def _resolve_path(path: str) -> Path:
    """Resolve a vault-relative *path* and validate it stays inside VAULT_ROOT."""
    full = (VAULT_ROOT / path).resolve()
    vault_resolved = VAULT_ROOT.resolve()
    if not str(full).startswith(str(vault_resolved)):
        raise ValueError("Path escapes the vault root.")
    parts = full.relative_to(vault_resolved).parts
    if any(p in BLOCKED_PATTERNS for p in parts):
        raise ValueError("Access to this folder is blocked.")
    return full


def _safe_read(path: str) -> str:
    """Read a vault-relative *path* after verifying it stays inside VAULT_ROOT."""
    full = _resolve_path(path)
    if not full.is_file():
        raise FileNotFoundError(f"Note not found: {path}")
    return full.read_text(encoding="utf-8")


def _parse_frontmatter(content: str) -> dict[str, str]:
    """Extract YAML front-matter as a flat key→value dict (no pyyaml needed)."""
    m = re.match(r"^---\s*\n(.*?\n)---\s*\n", content, re.DOTALL)
    if not m:
        return {}
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" ") and not line.startswith("\t"):
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip().strip('"').strip("'")
    return meta


def _body_without_frontmatter(content: str) -> str:
    """Return content with YAML front-matter stripped."""
    m = re.match(r"^---\s*\n.*?\n---\s*\n", content, re.DOTALL)
    return content[m.end() :] if m else content


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_notes(folder: str = "TeamWorkHub") -> str:
    """List markdown notes in a folder (most recent first).

    Args:
        folder: One of TeamWorkHub, TeamWorkHub_Daily, TeamWorkHub_Weekly,
                TeamWorkHub_Monthly, TeamWorkHub_Dashboard.
    """
    folder_path = _resolve_folder(folder)
    if not folder_path.is_dir():
        return f"Folder '{folder}' does not exist."

    files = sorted(folder_path.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return f"No notes found in '{folder}'."

    lines: list[str] = [f"## {folder} ({len(files)} notes)\n"]
    for f in files[:100]:
        rel = f"{folder}/{f.name}"
        lines.append(f"- `{rel}`")
    if len(files) > 100:
        lines.append(f"\n… and {len(files) - 100} more")
    return "\n".join(lines)


@mcp.tool()
def read_note(path: str) -> str:
    """Read the full content of a note by its vault-relative path.

    Args:
        path: Relative path inside the vault, e.g. "TeamWorkHub_Daily/2026-05-06.md".
    """
    return _safe_read(path)


@mcp.tool()
def search_notes(query: str, folder: str | None = None, max_results: int = 20) -> str:
    """Search notes by keyword (AND logic for multiple words). Returns matching snippets.

    Args:
        query: Search keywords (space-separated, AND logic). Korean and English supported.
        folder: Optional folder to limit search. If omitted, searches all allowed folders.
        max_results: Maximum results to return (default 20, max 50).
    """
    max_results = min(max(1, max_results), 50)
    keywords = query.lower().split()
    if not keywords:
        return "Please provide at least one search keyword."

    folders = [folder] if folder else sorted(ALLOWED_FOLDERS)
    hits: list[tuple[str, str]] = []

    for fld in folders:
        try:
            folder_path = _resolve_folder(fld)
        except ValueError:
            continue
        if not folder_path.is_dir():
            continue
        for md in folder_path.glob("*.md"):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            lower = text.lower()
            if all(kw in lower for kw in keywords):
                # Build a preview: find first line containing any keyword
                preview = ""
                for line in text.splitlines():
                    if any(kw in line.lower() for kw in keywords):
                        preview = line.strip()[:200]
                        break
                rel = f"{fld}/{md.name}"
                hits.append((rel, preview))
                if len(hits) >= max_results:
                    break
        if len(hits) >= max_results:
            break

    if not hits:
        return f"No notes matched '{query}'."

    lines = [f"## Search results for '{query}' ({len(hits)} hits)\n"]
    for rel, preview in hits:
        lines.append(f"- **`{rel}`**")
        if preview:
            lines.append(f"  > {preview}")
    return "\n".join(lines)


@mcp.tool()
def get_daily(date_str: str = "") -> str:
    """Read a Daily Note by date. If date is omitted, returns the most recent one.

    Args:
        date_str: Date in YYYY-MM-DD format. Leave empty for the latest daily note.
    """
    daily_dir = _resolve_folder("TeamWorkHub_Daily")
    if not daily_dir.is_dir():
        return "TeamWorkHub_Daily folder not found."

    if date_str:
        target = daily_dir / f"{date_str}.md"
        if not target.is_file():
            return f"Daily note for {date_str} not found."
        return target.read_text(encoding="utf-8")

    # Find most recent
    files = sorted(daily_dir.glob("*.md"), reverse=True)
    if not files:
        return "No daily notes found."
    return files[0].read_text(encoding="utf-8")


@mcp.tool()
def get_weekly(week: str = "") -> str:
    """Read a Weekly Note. If week is omitted, returns the most recent one.

    Args:
        week: Week identifier like "2026-W18". Leave empty for the latest weekly note.
    """
    weekly_dir = _resolve_folder("TeamWorkHub_Weekly")
    if not weekly_dir.is_dir():
        return "TeamWorkHub_Weekly folder not found."

    if week:
        target = weekly_dir / f"{week}.md"
        if not target.is_file():
            return f"Weekly note for {week} not found."
        return target.read_text(encoding="utf-8")

    files = sorted(weekly_dir.glob("*.md"), reverse=True)
    if not files:
        return "No weekly notes found."
    return files[0].read_text(encoding="utf-8")


@mcp.tool()
def get_assignee_summary(name: str) -> str:
    """Get an assignee's dashboard page and their recent mail notes.

    Args:
        name: Assignee name in Korean, e.g. "박은진".
    """
    # 1. Dashboard page
    dashboard_dir = _resolve_folder("TeamWorkHub_Dashboard")
    dashboard_file = dashboard_dir / f"{name}.md"
    parts: list[str] = []

    if dashboard_file.is_file():
        parts.append(f"## Dashboard: {name}\n")
        parts.append(dashboard_file.read_text(encoding="utf-8"))
    else:
        parts.append(f"No dashboard page found for '{name}'.")

    # 2. Recent mail notes mentioning this assignee in TeamWorkHub
    mail_dir = VAULT_ROOT / "TeamWorkHub"
    if mail_dir.is_dir():
        recent: list[tuple[float, Path]] = []
        for md in mail_dir.glob("*.md"):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            if name in text:
                recent.append((md.stat().st_mtime, md))

        recent.sort(key=lambda x: x[0], reverse=True)
        if recent:
            parts.append(f"\n## Recent mail notes mentioning {name} ({len(recent)} total)\n")
            for _, md in recent[:10]:
                meta = _parse_frontmatter(md.read_text(encoding="utf-8"))
                title = meta.get("email_title", md.stem)
                parts.append(f"- **{title}**  `TeamWorkHub/{md.name}`")

    # 3. Recent daily notes mentioning this assignee
    daily_dir = VAULT_ROOT / "TeamWorkHub_Daily"
    if daily_dir.is_dir():
        daily_hits: list[str] = []
        for md in sorted(daily_dir.glob("*.md"), reverse=True)[:14]:
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            if name in text:
                daily_hits.append(f"- `TeamWorkHub_Daily/{md.name}`")
        if daily_hits:
            parts.append(f"\n## Recent daily notes mentioning {name}\n")
            parts.extend(daily_hits)

    return "\n".join(parts)


@mcp.tool()
def edit_note(path: str, old_text: str, new_text: str) -> str:
    """Replace a specific text fragment in a note. Read the note first to get exact text.

    Args:
        path: Vault-relative path, e.g. "TeamWorkHub/2026-05-07 image attatchment test.md".
        old_text: Exact text to find and replace (must match exactly, including whitespace).
        new_text: Replacement text.
    """
    full = _resolve_path(path)
    if not full.is_file():
        raise FileNotFoundError(f"Note not found: {path}")
    content = full.read_text(encoding="utf-8")
    if old_text not in content:
        raise ValueError("old_text not found in the note. Read the note first to get exact text.")
    count = content.count(old_text)
    if count > 1:
        raise ValueError(f"old_text appears {count} times. Provide more context to make it unique.")
    updated = content.replace(old_text, new_text, 1)
    full.write_text(updated, encoding="utf-8")
    return f"Updated `{path}` — replaced 1 occurrence."


@mcp.tool()
def update_frontmatter_field(path: str, field: str, value: str) -> str:
    """Update a single YAML frontmatter field in a note.

    Args:
        path: Vault-relative path, e.g. "TeamWorkHub/2026-05-07 some note.md".
        field: Frontmatter field name, e.g. "result", "link", "description".
        value: New value for the field.
    """
    full = _resolve_path(path)
    if not full.is_file():
        raise FileNotFoundError(f"Note not found: {path}")
    content = full.read_text(encoding="utf-8")

    m = re.match(r"^(---\s*\n)(.*?\n)(---\s*\n)", content, re.DOTALL)
    if not m:
        raise ValueError("Note has no YAML frontmatter.")

    fm_lines = m.group(2).splitlines()
    found = False
    for i, line in enumerate(fm_lines):
        if ":" in line and not line.startswith(" ") and not line.startswith("\t"):
            key = line.partition(":")[0].strip()
            if key == field:
                fm_lines[i] = f"{field}: {value}"
                found = True
                break
    if not found:
        fm_lines.append(f"{field}: {value}")

    new_fm = "\n".join(fm_lines) + "\n"
    updated = m.group(1) + new_fm + m.group(3) + content[m.end():]
    full.write_text(updated, encoding="utf-8")
    action = "updated" if found else "added"
    return f"Frontmatter field `{field}` {action} in `{path}`."


# ---------------------------------------------------------------------------
# Entry point — stdio transport for Claude Desktop
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
