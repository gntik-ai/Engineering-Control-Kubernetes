"""NEW-130: paths that agent CLIs load as configuration.

Codex loads every `.codex/` folder between its cwd and the project root; Claude Code
loads `.claude/` and `.mcp.json`. A ChangeSet must never add, change, delete, rename or
alias (symlink) any of them automatically.
"""

from __future__ import annotations

AGENT_CONFIG_NAMES = frozenset({".codex", ".claude", ".mcp.json"})


def is_agent_config_path(path: str) -> bool:
    """True when any component of a repository-relative path (or symlink target),
    including the last one, is an agent configuration name."""
    return any(part in AGENT_CONFIG_NAMES for part in path.replace("\\", "/").split("/"))


def overlaps(first: str, second: str) -> bool:
    """True when one repository-relative path equals or contains the other."""
    return first == second or first.startswith(second + "/") or second.startswith(first + "/")
