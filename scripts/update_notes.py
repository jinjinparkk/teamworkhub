"""Add description and tag fields to existing individual notes."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.md_writer import _extract_media_subsidiary_tags, _yaml_scalar

VAULT = r"C:\Users\ParkEunJin\OneDrive - Artience Inc\문서\Obsidian Vault\TeamWorkHub"


def update_note(fpath: str, fname: str) -> None:
    content = open(fpath, encoding="utf-8").read()

    if "description:" in content and "tag:" in content:
        print(f"  SKIP (already has fields): {fname}")
        return

    if not content.startswith("---"):
        print(f"  SKIP (no frontmatter): {fname}")
        return

    parts = content.split("---", 2)
    if len(parts) < 3:
        print(f"  SKIP (bad frontmatter): {fname}")
        return

    yaml_part = parts[1]
    body_part = parts[2]

    # Extract description from summary section
    desc = ""
    summary_match = re.search(r"### 요약\s*\n(.*?)(?=\n###|\Z)", body_part, re.DOTALL)
    if summary_match:
        summary_text = summary_match.group(1).strip()
        first_line = summary_text.split("\n")[0] if summary_text else ""
        desc = first_line.lstrip("- ").strip()
        if desc == "_(요약 없음)_":
            desc = ""

    # Extract tags from full body text
    full_text = fname + " " + body_part
    ms_tags = _extract_media_subsidiary_tags(full_text)

    # Build new fields
    if desc:
        desc_field = f"description: {_yaml_scalar(desc)}\n"
    else:
        desc_field = "description:\n"

    if ms_tags:
        tag_field = "tag:\n" + "".join(f"  - {t}\n" for t in ms_tags)
    else:
        tag_field = "tag:\n"

    new_fields = desc_field + tag_field

    # Insert before 'result:'
    if "result:" in yaml_part:
        yaml_part = yaml_part.replace("result:", new_fields + "result:")
    else:
        yaml_part = yaml_part.rstrip() + "\n" + new_fields

    new_content = "---" + yaml_part + "---" + body_part
    open(fpath, "w", encoding="utf-8").write(new_content)
    tag_str = ", ".join(ms_tags) if ms_tags else "(none)"
    desc_short = (desc[:40] + "...") if len(desc) > 40 else (desc or "(none)")
    print(f"  UPDATED: {fname} | desc={desc_short} | tag={tag_str}")


def main():
    for fname in sorted(os.listdir(VAULT)):
        if fname.endswith(".md"):
            update_note(os.path.join(VAULT, fname), fname)
    print("\nDone!")


if __name__ == "__main__":
    main()
