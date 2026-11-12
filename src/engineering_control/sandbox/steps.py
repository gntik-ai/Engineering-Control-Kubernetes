"""What a step asks its sandbox to do, and what its result means.

A step is described to the runner in the sandbox as a job: the commands to run, in order.
The job never holds a credential: it names the file the account's sign-in is mounted at.
What comes back is, for every command, its exit code and the end of what it printed; this
module turns that into a verdict in the platform's words.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from engineering_control.sandbox.pods import ACCOUNT, ACCOUNT_HOME, WORKSPACE

MARKER: Final = "::sandbox-result::"
CHECK_PROMPT: Final = "Reply with the single word ready and nothing else."
CHECK_SECONDS: Final = 180

# What a provider's client says when the sign-in is no longer accepted.
_SIGN_IN = re.compile(
    r"not logged in|please run /login|/login|invalid api key|invalid.{0,20}token|"
    r"authentication_error|oauth token (has )?expired|token (has )?expired|"
    r"refresh.token|unauthorized|\b401\b|sign in again|login required|not authenticated",
    re.IGNORECASE,
)
# What it says when the subscription refuses more work for now.
_LIMIT = re.compile(
    r"usage limit|rate.limit|rate_limit|quota exceeded|too many requests|\b429\b|"
    r"limit reached|out of (extra )?usage|try again (at|in)|resets? (at|in)",
    re.IGNORECASE,
)
_RESET_IN = re.compile(
    r"(?:try again|resets?) in (?:(\d+) ?(?:days?|d))? ?(?:(\d+) ?(?:hours?|h))? ?"
    r"(?:(\d+) ?(?:minutes?|min|m))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Verdict:
    """What a step's result means. ``account_state`` is set when the step taught the
    platform something about the account it worked with."""

    succeeded: bool
    outcome: str
    detail: str | None = None
    account_state: str | None = None
    account_reason: str | None = None
    limited_until: datetime | None = None


def account_setup(provider: str, generation: int) -> dict[str, Any]:
    """How a sandbox makes the leased account's sign-in available to its client."""
    if provider == "claude":
        return {"env_files": {"CLAUDE_CODE_OAUTH_TOKEN": f"{ACCOUNT}/token"}}
    if provider == "chatgpt":
        # Codex renews this file in place, so it lives on the account's own volume and is
        # replaced only when the administrator signs the account in again.
        return {
            "seed": [
                {
                    "from": f"{ACCOUNT}/auth.json",
                    "to": f"{ACCOUNT_HOME}/auth.json",
                    "generation": generation,
                }
            ]
        }
    raise ValueError(f"no client for provider {provider}")


def client_environment(provider: str) -> dict[str, str]:
    if provider == "chatgpt":
        return {"CODEX_HOME": ACCOUNT_HOME}
    return {}


def check_job(provider: str, generation: int) -> dict[str, Any]:
    """Ask the account's client one trivial question, with no tools and nothing to read."""
    if provider == "claude":
        argv = [
            "claude",
            "--print",
            "--output-format",
            "text",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--tools",
            "",
            CHECK_PROMPT,
        ]
    elif provider == "chatgpt":
        argv = [
            "codex",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--cd",
            WORKSPACE,
            CHECK_PROMPT,
        ]
    else:
        raise ValueError(f"no client for provider {provider}")
    return {
        **account_setup(provider, generation),
        "commands": [
            {
                "name": "check",
                "argv": argv,
                "cwd": WORKSPACE,
                "env": client_environment(provider),
                "timeout": CHECK_SECONDS,
            }
        ],
    }


def parse_result(log: str) -> list[dict[str, Any]] | None:
    """The commands' results from the last line the runner printed, or None when the
    sandbox ended without one."""
    position = log.rfind(MARKER)
    if position < 0:
        return None
    encoded = log[position + len(MARKER) :].split("\n", 1)[0].strip()
    try:
        document = json.loads(base64.b64decode(encoded, validate=True))
    except (ValueError, binascii.Error):
        return None
    commands = document.get("commands") if isinstance(document, dict) else None
    if not isinstance(commands, list) or not all(isinstance(item, dict) for item in commands):
        return None
    return commands


def _limited_until(text: str, now: datetime) -> datetime:
    """When the provider says work is accepted again; an hour from now when it says no
    time, and never more than a week away."""
    found = _RESET_IN.search(text)
    if found and any(found.groups()):
        days, hours, minutes = (int(part) if part else 0 for part in found.groups())
        wait = timedelta(days=days, hours=hours, minutes=minutes)
        if timedelta(0) < wait <= timedelta(days=7):
            return now + wait
    return now + timedelta(hours=1)


def _said(command: dict[str, Any]) -> str:
    return f"{command.get('stdout') or ''}\n{command.get('stderr') or ''}"


def _short(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[-limit:]


def account_problem(command: dict[str, Any], now: datetime) -> Verdict | None:
    """What a failed client command says about the account, if it says anything."""
    text = _said(command)
    if _SIGN_IN.search(text):
        return Verdict(
            False,
            "sign_in_required",
            "The provider no longer accepts the stored sign-in.",
            account_state="sign_in_required",
            account_reason="The provider refused the stored sign-in. Sign the account in again.",
        )
    if _LIMIT.search(text):
        until = _limited_until(text, now)
        return Verdict(
            False,
            "limited",
            "The subscription refuses more work for now.",
            account_state="limited",
            account_reason=f"The subscription reached its limit. Tried again after "
            f"{until.astimezone(UTC):%d %b %H:%M} UTC.",
            limited_until=until,
        )
    return None


def interpret_check(commands: list[dict[str, Any]] | None, now: datetime) -> Verdict:
    if not commands:
        return Verdict(False, "no_result", "The sandbox ended without a result.")
    command = commands[-1]
    if command.get("name") == "setup":
        return Verdict(
            False,
            "sign_in_required",
            "The stored sign-in could not be used.",
            account_state="sign_in_required",
            account_reason="The stored sign-in could not be read. Sign the account in again.",
        )
    if command.get("timed_out"):
        return Verdict(False, "timed_out", "The client did not answer in time.")
    if command.get("exit_code") == 0 and "ready" in str(command.get("stdout") or "").lower():
        return Verdict(True, "ready", "The account answered.", account_state="ready")
    problem = account_problem(command, now)
    if problem is not None:
        return problem
    return Verdict(
        False,
        "failed",
        f"The client ended with code {command.get('exit_code')}: {_short(_said(command))}",
    )
