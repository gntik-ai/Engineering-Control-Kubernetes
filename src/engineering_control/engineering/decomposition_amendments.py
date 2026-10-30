"""NEW-234 PR6: the operator publishes a decomposition's story amendments with their own gh.

Hermes never edits an issue. `engineering-ctl backlog-decomposition-story-amendments`
fetches the approved plan's amended story bodies, re-checks each body against its hash,
writes it to a private file, and prints its diff and the exact `gh api -X PATCH` command
the operator runs. With --verify-current it first reads each story issue (read-only, the
operator's gh) and withholds the command for an issue whose body is no longer the one the
plan was made from (or that already carries the amendment).
"""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

from engineering_control.engineering.change_request_publisher import (
    ChangePublishError,
    GhRunner,
    OperatorAPI,
    run_gh,
)
from engineering_control.engineering.decomposition import DECOMPOSITION_PROFILES, STORY_KEY
from engineering_control.engineering.decomposition_fanout import amendment_command
from engineering_control.engineering.decomposition_inventory import body_sha256

# NEW-242: a story amendment edits a story issue in a decomposition project's own repository.
_SOURCE_REPOSITORIES = frozenset(
    profile.source_repository for profile in DECOMPOSITION_PROFILES.values()
)


def _write_private(path: Path, text: str) -> None:
    """Create the body file 0600 (an identical existing file is kept; never overwritten)."""
    data = text.encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise ChangePublishError(f"{path} exists with other content") from None
        return
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def story_amendment_edits(
    client: OperatorAPI,
    decomposition_id: str,
    *,
    output_dir: Path | None = None,
    verify_current: bool = False,
    gh: GhRunner = run_gh,
) -> dict[str, Any]:
    view = client.request("GET", f"/v1/backlog-decompositions/{decomposition_id}/story-amendments")
    if not isinstance(view, dict) or not isinstance(view.get("amendments"), list):
        raise ChangePublishError("story amendment response is invalid")
    directory = output_dir or Path(tempfile.mkdtemp(prefix="decomposition-amendments-"))
    if not directory.is_dir() or directory.is_symlink():
        raise ChangePublishError(f"{directory} is not a directory")
    entries: list[dict[str, Any]] = []
    for item in view["amendments"]:
        repository = str(item.get("repository") or "")
        issue = item.get("issue")
        story_id = str(item.get("story_id") or "")
        amended = item.get("amended_body")
        if (
            repository not in _SOURCE_REPOSITORIES
            or type(issue) is not int
            or issue < 1
            or not STORY_KEY.fullmatch(story_id)
            or not isinstance(amended, str)
            or body_sha256(amended) != item.get("amended_body_sha256")
        ):
            raise ChangePublishError("a story amendment is incomplete or does not match its hash")
        path = directory / f"story-{issue}-{story_id}.md"
        _write_private(path, amended)
        state = "UNVERIFIED"
        if verify_current:
            current = gh(["api", f"repos/{repository}/issues/{issue}", "--jq", ".body"])
            digest = body_sha256(current)
            state = (
                "CURRENT"
                if digest == item.get("current_body_sha256")
                else "ALREADY_APPLIED"
                if digest == item["amended_body_sha256"]
                else "DRIFTED"
            )
        publishable = state in {"CURRENT", "UNVERIFIED"}
        entries.append(
            {
                "story_id": story_id,
                "repository": repository,
                "issue": issue,
                "reason": item.get("reason"),
                "current_state": state,
                "current_body_sha256": item.get("current_body_sha256"),
                "body_file": str(path),
                "diff": str(item.get("diff") or ""),
                "manual": list(item.get("manual") or []),
                "command": (
                    shlex.join(amendment_command(repository, issue, str(path)))
                    if publishable
                    else None
                ),
            }
        )
    return {
        "decomposition_id": decomposition_id,
        "plan_sha256": view.get("plan_sha256"),
        "output_dir": str(directory),
        "amendments": entries,
    }


def render_story_amendment_edits(result: dict[str, Any]) -> str:
    """The operator-facing text: every diff, then the command that publishes it."""
    lines = [
        f"# Story amendments of backlog decomposition {result['decomposition_id']}",
        "# Hermes never edits an issue: review each diff, then run its command yourself.",
        "",
    ]
    if not result["amendments"]:
        lines.append("No story amendments in this plan.")
    for entry in result["amendments"]:
        lines.append(
            f"## {entry['story_id']}: {entry['repository']}#{entry['issue']} "
            f"({entry['current_state']})"
        )
        lines.append("")
        lines.append(entry["diff"].rstrip("\n"))
        lines.append("")
        for change in entry["manual"]:
            lines.append(
                f"! not placed in the body's own section (listed in the amendment section): "
                f"{change['section']} {change['action']} {change.get('target') or ''}".rstrip()
            )
        if entry["command"]:
            if entry["current_state"] == "UNVERIFIED":
                lines.append(
                    f"# the issue body must still have sha256 {entry['current_body_sha256']} "
                    "(normalized); --verify-current checks it"
                )
            lines.append(entry["command"])
        else:
            lines.append(f"# no command: the issue is {entry['current_state']}")
        lines.append("")
    return "\n".join(lines)
