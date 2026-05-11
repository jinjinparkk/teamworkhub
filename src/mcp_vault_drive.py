"""TeamWorkHub Vault — MCP server backed by Google Drive API.

Drop-in replacement for mcp_vault.py (local filesystem) that runs on
Cloud Run.  All 8 tool names and parameters are identical; only the
internal storage layer changes from pathlib to Drive API calls.

Mount via FastAPI:
    mcp_app = mcp.http_app(path="/")
    app.mount("/mcp", mcp_app)
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from fastmcp import FastMCP
from googleapiclient.discovery import Resource
from googleapiclient.errors import HttpError

from src.auth import build_credentials, build_drive_service
from src.config import Config, load as load_config
from src.drive_client import (
    download_file_content,
    find_file_by_name,
    list_files_in_folder,
    upsert_markdown,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Drive service — module-level cached singleton
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_drive_svc: Resource | None = None
_cfg: Config | None = None


def _get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def _get_drive_service() -> Resource:
    """Return a cached Drive service, rebuilding on first call or after reset."""
    global _drive_svc
    if _drive_svc is not None:
        return _drive_svc
    with _lock:
        if _drive_svc is not None:
            return _drive_svc
        cfg = _get_config()
        creds = build_credentials(cfg)
        _drive_svc = build_drive_service(creds)
        log.info("MCP Drive service initialised")
        return _drive_svc


def _reset_drive_service() -> None:
    """Reset the cached service so the next call rebuilds credentials."""
    global _drive_svc
    with _lock:
        _drive_svc = None
    log.info("MCP Drive service reset (will rebuild on next call)")


def _svc() -> Resource:
    """Shorthand used by all tools.  Retries once on 401."""
    return _get_drive_service()


# ---------------------------------------------------------------------------
# Folder mapping
# ---------------------------------------------------------------------------

def _folder_id(folder: str) -> str:
    """Map a logical folder name to its Drive folder ID."""
    cfg = _get_config()
    mapping: dict[str, str] = {
        "TeamWorkHub": cfg.drive_output_folder_id,
        "TeamWorkHub_Daily": cfg.daily_output_folder_id,
        "TeamWorkHub_Weekly": cfg.weekly_output_folder_id,
        "TeamWorkHub_Monthly": cfg.monthly_output_folder_id,
        "TeamWorkHub_Dashboard": cfg.dashboard_output_folder_id,
    }
    fid = mapping.get(folder)
    if not fid:
        raise ValueError(
            f"'{folder}' is not configured or its folder ID is empty. "
            f"Available: {', '.join(k for k, v in mapping.items() if v)}"
        )
    return fid


ALLOWED_FOLDERS = {
    "TeamWorkHub",
    "TeamWorkHub_Daily",
    "TeamWorkHub_Weekly",
    "TeamWorkHub_Monthly",
    "TeamWorkHub_Dashboard",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _retry_on_401(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call *fn*, and on 401 reset credentials and retry once."""
    try:
        return fn(*args, **kwargs)
    except HttpError as exc:
        if exc.resp.status == 401:
            _reset_drive_service()
            return fn(*args, **kwargs)
        raise


def _parse_frontmatter(content: str) -> dict[str, str]:
    """Extract YAML front-matter as a flat key->value dict."""
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
    m = re.match(r"^---\s*\n.*?\n---\s*\n", content, re.DOTALL)
    return content[m.end():] if m else content


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("teamworkhub-vault")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_notes(folder: str = "TeamWorkHub") -> str:
    """List markdown notes in a folder (most recent first by name).

    Args:
        folder: One of TeamWorkHub, TeamWorkHub_Daily, TeamWorkHub_Weekly,
                TeamWorkHub_Monthly, TeamWorkHub_Dashboard.
    """
    if folder not in ALLOWED_FOLDERS:
        raise ValueError(
            f"'{folder}' is not an allowed folder. "
            f"Choose from: {', '.join(sorted(ALLOWED_FOLDERS))}"
        )

    def _do() -> str:
        fid = _folder_id(folder)
        files = list_files_in_folder(_svc(), fid)
        md_files = [f for f in files if f.name.endswith(".md")]
        md_files.sort(key=lambda f: f.name, reverse=True)
        if not md_files:
            return f"No notes found in '{folder}'."
        lines: list[str] = [f"## {folder} ({len(md_files)} notes)\n"]
        for f in md_files[:100]:
            lines.append(f"- `{folder}/{f.name}`")
        if len(md_files) > 100:
            lines.append(f"\n... and {len(md_files) - 100} more")
        return "\n".join(lines)

    return _retry_on_401(_do)


@mcp.tool()
def read_note(path: str) -> str:
    """Read the full content of a note by its vault-relative path.

    Args:
        path: Relative path inside the vault, e.g. "TeamWorkHub_Daily/2026-05-06.md".
    """
    folder, _, filename = path.partition("/")
    if not filename:
        raise ValueError("path must be folder/filename.md format.")
    if folder not in ALLOWED_FOLDERS:
        raise ValueError(f"'{folder}' is not an allowed folder.")

    def _do() -> str:
        fid = _folder_id(folder)
        found = find_file_by_name(_svc(), filename, fid)
        if not found:
            raise FileNotFoundError(f"Note not found: {path}")
        return download_file_content(_svc(), found.file_id)

    return _retry_on_401(_do)


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

    def _do() -> str:
        for fld in folders:
            if fld not in ALLOWED_FOLDERS:
                continue
            try:
                fid = _folder_id(fld)
            except ValueError:
                continue
            files = list_files_in_folder(_svc(), fid)
            md_files = [f for f in files if f.name.endswith(".md")]
            for df in md_files:
                try:
                    text = download_file_content(_svc(), df.file_id)
                except Exception:
                    continue
                lower = text.lower()
                if all(kw in lower for kw in keywords):
                    preview = ""
                    for line in text.splitlines():
                        if any(kw in line.lower() for kw in keywords):
                            preview = line.strip()[:200]
                            break
                    hits.append((f"{fld}/{df.name}", preview))
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

    return _retry_on_401(_do)


@mcp.tool()
def get_daily(date_str: str = "") -> str:
    """Read a Daily Note by date. If date is omitted, returns the most recent one.

    Args:
        date_str: Date in YYYY-MM-DD format. Leave empty for the latest daily note.
    """

    def _do() -> str:
        fid = _folder_id("TeamWorkHub_Daily")
        if date_str:
            filename = f"{date_str}.md"
            found = find_file_by_name(_svc(), filename, fid)
            if not found:
                return f"Daily note for {date_str} not found."
            return download_file_content(_svc(), found.file_id)

        files = list_files_in_folder(_svc(), fid)
        md_files = [f for f in files if f.name.endswith(".md")]
        if not md_files:
            return "No daily notes found."
        md_files.sort(key=lambda f: f.name, reverse=True)
        return download_file_content(_svc(), md_files[0].file_id)

    return _retry_on_401(_do)


@mcp.tool()
def get_weekly(week: str = "") -> str:
    """Read a Weekly Note. If week is omitted, returns the most recent one.

    Args:
        week: Week identifier like "2026-W18". Leave empty for the latest weekly note.
    """

    def _do() -> str:
        fid = _folder_id("TeamWorkHub_Weekly")
        if week:
            filename = f"{week}.md"
            found = find_file_by_name(_svc(), filename, fid)
            if not found:
                return f"Weekly note for {week} not found."
            return download_file_content(_svc(), found.file_id)

        files = list_files_in_folder(_svc(), fid)
        md_files = [f for f in files if f.name.endswith(".md")]
        if not md_files:
            return "No weekly notes found."
        md_files.sort(key=lambda f: f.name, reverse=True)
        return download_file_content(_svc(), md_files[0].file_id)

    return _retry_on_401(_do)


@mcp.tool()
def get_assignee_summary(name: str) -> str:
    """Get an assignee's dashboard page and their recent mail notes.

    Args:
        name: Assignee name in Korean, e.g. "박은진".
    """

    def _do() -> str:
        parts: list[str] = []

        # 1. Dashboard page
        try:
            dash_fid = _folder_id("TeamWorkHub_Dashboard")
            dash_file = find_file_by_name(_svc(), f"{name}.md", dash_fid)
            if dash_file:
                parts.append(f"## Dashboard: {name}\n")
                parts.append(download_file_content(_svc(), dash_file.file_id))
            else:
                parts.append(f"No dashboard page found for '{name}'.")
        except ValueError:
            parts.append(f"No dashboard page found for '{name}' (folder not configured).")

        # 2. Recent mail notes mentioning this assignee in TeamWorkHub
        try:
            mail_fid = _folder_id("TeamWorkHub")
            mail_files = list_files_in_folder(_svc(), mail_fid)
            md_mails = [f for f in mail_files if f.name.endswith(".md")]
            mentions: list[tuple[str, str]] = []
            for df in md_mails:
                try:
                    text = download_file_content(_svc(), df.file_id)
                except Exception:
                    continue
                if name in text:
                    meta = _parse_frontmatter(text)
                    title = meta.get("email_title", df.name.removesuffix(".md"))
                    mentions.append((title, df.name))
                if len(mentions) >= 10:
                    break
            if mentions:
                parts.append(
                    f"\n## Recent mail notes mentioning {name} ({len(mentions)} shown)\n"
                )
                for title, fname in mentions:
                    parts.append(f"- **{title}**  `TeamWorkHub/{fname}`")
        except ValueError:
            pass

        # 3. Recent daily notes mentioning this assignee
        try:
            daily_fid = _folder_id("TeamWorkHub_Daily")
            daily_files = list_files_in_folder(_svc(), daily_fid)
            md_dailies = sorted(
                [f for f in daily_files if f.name.endswith(".md")],
                key=lambda f: f.name,
                reverse=True,
            )[:14]
            daily_hits: list[str] = []
            for df in md_dailies:
                try:
                    text = download_file_content(_svc(), df.file_id)
                except Exception:
                    continue
                if name in text:
                    daily_hits.append(f"- `TeamWorkHub_Daily/{df.name}`")
            if daily_hits:
                parts.append(f"\n## Recent daily notes mentioning {name}\n")
                parts.extend(daily_hits)
        except ValueError:
            pass

        return "\n".join(parts)

    return _retry_on_401(_do)


@mcp.tool()
def edit_note(path: str, old_text: str, new_text: str) -> str:
    """Replace a specific text fragment in a note. Read the note first to get exact text.

    Args:
        path: Vault-relative path, e.g. "TeamWorkHub/2026-05-07 image attatchment test.md".
        old_text: Exact text to find and replace (must match exactly, including whitespace).
        new_text: Replacement text.
    """
    folder, _, filename = path.partition("/")
    if not filename:
        raise ValueError("path must be folder/filename.md format.")
    if folder not in ALLOWED_FOLDERS:
        raise ValueError(f"'{folder}' is not an allowed folder.")

    def _do() -> str:
        fid = _folder_id(folder)
        found = find_file_by_name(_svc(), filename, fid)
        if not found:
            raise FileNotFoundError(f"Note not found: {path}")
        content = download_file_content(_svc(), found.file_id)
        if old_text not in content:
            raise ValueError(
                "old_text not found in the note. Read the note first to get exact text."
            )
        count = content.count(old_text)
        if count > 1:
            raise ValueError(
                f"old_text appears {count} times. Provide more context to make it unique."
            )
        updated = content.replace(old_text, new_text, 1)
        upsert_markdown(_svc(), fid, filename, updated)
        return f"Updated `{path}` — replaced 1 occurrence."

    return _retry_on_401(_do)


@mcp.tool()
def update_frontmatter_field(path: str, field: str, value: str) -> str:
    """Update a single YAML frontmatter field in a note.

    Args:
        path: Vault-relative path, e.g. "TeamWorkHub/2026-05-07 some note.md".
        field: Frontmatter field name, e.g. "result", "link", "description".
        value: New value for the field.
    """
    folder, _, filename = path.partition("/")
    if not filename:
        raise ValueError("path must be folder/filename.md format.")
    if folder not in ALLOWED_FOLDERS:
        raise ValueError(f"'{folder}' is not an allowed folder.")

    def _do() -> str:
        fid = _folder_id(folder)
        found = find_file_by_name(_svc(), filename, fid)
        if not found:
            raise FileNotFoundError(f"Note not found: {path}")
        content = download_file_content(_svc(), found.file_id)

        m = re.match(r"^(---\s*\n)(.*?\n)(---\s*\n)", content, re.DOTALL)
        if not m:
            raise ValueError("Note has no YAML frontmatter.")

        fm_lines = m.group(2).splitlines()
        found_field = False
        for i, line in enumerate(fm_lines):
            if ":" in line and not line.startswith(" ") and not line.startswith("\t"):
                key = line.partition(":")[0].strip()
                if key == field:
                    fm_lines[i] = f"{field}: {value}"
                    found_field = True
                    break
        if not found_field:
            fm_lines.append(f"{field}: {value}")

        new_fm = "\n".join(fm_lines) + "\n"
        updated = m.group(1) + new_fm + m.group(3) + content[m.end():]
        upsert_markdown(_svc(), fid, filename, updated)
        action = "updated" if found_field else "added"
        return f"Frontmatter field `{field}` {action} in `{path}`."

    return _retry_on_401(_do)
