"""Load the Odoo-QA skill (the playbook) and assemble the Claude Code system prompt.

The skill body in `skills/odoo-qa/SKILL.md` is the single source of truth for *how* Sentinel
tests an Odoo module. We inject it as the system prompt (rather than relying on filesystem
skill-discovery) so we don't have to drop a `.claude/` folder inside the user's addon.
"""

from __future__ import annotations

from pathlib import Path

from sentinel.paths import repo_root

_SKILL_PATH = repo_root() / "skills" / "odoo-qa" / "SKILL.md"


def load_skill() -> str:
    """Return the skill body (Markdown after the YAML front-matter)."""
    if not _SKILL_PATH.exists():
        return "You are Sentinel, a QA agent for Odoo. Read the code and report bugs/gaps."
    text = _SKILL_PATH.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :]
    return text.strip()


def build_system_prompt(system_map_summary: str | None, *, has_source: bool = True) -> str:
    parts = [load_skill()]
    if has_source:
        parts.append(
            "\n\n---\n\n# MODE — SOURCE AVAILABLE\n"
            "The addon source code is provided. Read it with Read/Grep/Glob and ground every finding "
            "in a concrete `file.py:line` (or `model.method`)."
        )
    else:
        parts.append(
            "\n\n---\n\n# MODE — NO SOURCE CODE (live instance only)\n"
            "No addon source is available. Do NOT try to read files, and do NOT cite `file:line` or "
            "code snippets. Work entirely from the **System Map** (the live structure/config) and your "
            "knowledge of how Odoo works. Focus on **UI behaviour and logic-FLOW gaps**: how the process "
            "flows across models / states / actions, where steps can break, validations or guards that "
            "appear to be missing, security/access gaps visible in the map, and view/menu problems. "
            "Phrase everything **functionally** for a non-developer — in place of an 'Evidence (file:line)' "
            "line, name the model / action / state involved and give a `Flow:` walkthrough. Reports cover "
            "the process and functional gaps only. Be clear about what would need the source code or the "
            "live record to confirm."
        )
    if system_map_summary:
        parts.append("\n\n---\n\n# SYSTEM MAP (live introspection of this module)\n")
        parts.append(system_map_summary)
    else:
        parts.append("\n\n(No System Map yet — ask the user to run Understand first.)")
    return "".join(parts)
