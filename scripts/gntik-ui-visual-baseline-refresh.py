#!/usr/bin/env python3
"""NEW-220: refresh gntik-ui's visual baselines on the CI runner, then verify the result.

gntik-ui's ci.yml re-records the visual baselines only when a pull request's head commit
message contains ``[visual-update]`` (job ``visual-update``). It commits them to the PR
branch as github-actions[bot], and a bot commit starts no CI. Hermes makers may not do this
(the gntik-ui maker rules forbid the marker), so a stale baseline is the operator's to
refresh. This script runs that operator path with checks the manual steps lacked:

1. an empty commit on top of main whose message carries the marker, on a fresh branch;
2. a pull request for it, then a wait for the bot's baseline commit;
3. a check that the bot commit is github-actions[bot]'s, sits directly on the marker
   commit, and changes only ``apps/docs/e2e/__screenshots__/``;
4. close and reopen the PR, so CI runs on the recorded baselines;
5. a report of the ``visual`` check on that commit, with the merge command.

It never merges. Run it as the operator, with the operator's own ``gh`` login:
    scripts/gntik-ui-visual-baseline-refresh.py [--timeout-minutes 40]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

REPOSITORY = "gntik-ai/gntik-ui"
MARKER = "[visual-update]"
BOT_LOGIN = "github-actions[bot]"
BASELINE_PREFIX = "apps/docs/e2e/__screenshots__/"
RECORD_JOB = "visual-update"
VISUAL_CHECK = "visual"
POLL_SECONDS = 30

Gh = Callable[[list[str]], str]


class RefreshError(RuntimeError):
    """The refresh stopped; the message says why and what was left open."""


def run_gh(arguments: list[str]) -> str:
    executable = shutil.which("gh")
    if executable is None:
        raise RefreshError("the GitHub CLI (gh) is not installed")
    completed = subprocess.run(  # noqa: S603 - fixed gh calls built by this script
        [executable, *arguments], check=False, capture_output=True, text=True, timeout=120
    )
    if completed.returncode != 0:
        raise RefreshError(f"gh {' '.join(arguments[:3])} failed: {completed.stderr.strip()}")
    return completed.stdout


def _json(gh: Gh, arguments: list[str]) -> Any:
    return json.loads(gh(arguments))


def create_marker_pull_request(gh: Gh, now: datetime) -> tuple[int, str, str]:
    """An empty marker commit on top of main, its branch and a PR; returns (pr, branch, sha)."""
    main = _json(
        gh,
        [
            "api",
            f"repos/{REPOSITORY}/commits/main",
            "--jq",
            "{sha: .sha, tree: .commit.tree.sha}",
        ],
    )
    message = (
        f"{MARKER} Re-record changed visual baselines on the CI runner\n\n"
        "Empty commit: the visual-update job re-records only the screenshots that fail "
        "their comparison and commits them to this branch."
    )
    commit = _json(
        gh,
        [
            "api",
            "-X",
            "POST",
            f"repos/{REPOSITORY}/git/commits",
            "-f",
            f"message={message}",
            "-f",
            f"tree={main['tree']}",
            "-f",
            f"parents[]={main['sha']}",
            "--jq",
            "{sha: .sha}",
        ],
    )
    branch = f"visual-baseline-refresh-{now:%Y%m%d%H%M%S}"
    gh(
        [
            "api",
            "-X",
            "POST",
            f"repos/{REPOSITORY}/git/refs",
            "-f",
            f"ref=refs/heads/{branch}",
            "-f",
            f"sha={commit['sha']}",
        ]
    )
    url = gh(
        [
            "pr",
            "create",
            "--repo",
            REPOSITORY,
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            "Re-record changed visual baselines",
            "--body",
            "Operator visual-baseline refresh (engineering-control NEW-220). Merge only "
            "after the script has verified the bot commit and the visual check on it.",
        ]
    ).strip()
    number = int(url.rstrip("/").rsplit("/", 1)[1])
    return number, branch, commit["sha"]


def _record_job_finished(gh: Gh, sha: str) -> bool:
    runs = _json(
        gh,
        [
            "api",
            f"repos/{REPOSITORY}/commits/{sha}/check-runs?per_page=100",
            "--jq",
            "[.check_runs[] | {name, status}]",
        ],
    )
    return any(run.get("name") == RECORD_JOB and run.get("status") == "completed" for run in runs)


def wait_for_bot_commit(
    gh: Gh, number: int, marker_sha: str, deadline: float, sleep: Callable[[float], None]
) -> str | None:
    """The bot's baseline commit, or None when the record job finished without one."""
    while True:
        commits = _json(
            gh,
            [
                "api",
                f"repos/{REPOSITORY}/pulls/{number}/commits",
                "--jq",
                "[.[] | {sha, author: .author.login, parents: [.parents[].sha]}]",
            ],
        )
        if len(commits) > 1:
            return verify_bot_commit(gh, commits, marker_sha)
        if _record_job_finished(gh, marker_sha):
            # Re-read once: the job may have pushed just before it finished.
            commits = _json(
                gh,
                [
                    "api",
                    f"repos/{REPOSITORY}/pulls/{number}/commits",
                    "--jq",
                    "[.[] | {sha, author: .author.login, parents: [.parents[].sha]}]",
                ],
            )
            return verify_bot_commit(gh, commits, marker_sha) if len(commits) > 1 else None
        if time.monotonic() >= deadline:
            raise RefreshError(f"no baseline commit on PR #{number} before the timeout")
        sleep(POLL_SECONDS)


def verify_bot_commit(gh: Gh, commits: list[dict[str, Any]], marker_sha: str) -> str:
    """Exactly one commit after the marker: the bot's, on the marker, baselines only."""
    if len(commits) != 2 or commits[0].get("sha") != marker_sha:
        raise RefreshError("the PR does not hold exactly the marker commit and one bot commit")
    bot = commits[1]
    if bot.get("author") != BOT_LOGIN:
        raise RefreshError(f"the commit after the marker is not {BOT_LOGIN}'s")
    if bot.get("parents") != [marker_sha]:
        raise RefreshError("the bot commit does not sit directly on the marker commit")
    files = _json(
        gh,
        [
            "api",
            f"repos/{REPOSITORY}/commits/{bot['sha']}",
            "--jq",
            "[.files[].filename]",
        ],
    )
    outside = [path for path in files if not str(path).startswith(BASELINE_PREFIX)]
    if not files or outside:
        raise RefreshError(f"the bot commit changes files outside {BASELINE_PREFIX}: {outside}")
    return str(bot["sha"])


def wait_for_visual(gh: Gh, sha: str, deadline: float, sleep: Callable[[float], None]) -> str:
    """The conclusion of the visual check on the recorded baselines."""
    while True:
        runs = _json(
            gh,
            [
                "api",
                f"repos/{REPOSITORY}/commits/{sha}/check-runs?per_page=100",
                "--jq",
                "[.check_runs[] | {name, status, conclusion}]",
            ],
        )
        for run in runs:
            if run.get("name") == VISUAL_CHECK and run.get("status") == "completed":
                return str(run.get("conclusion") or "unknown")
        if time.monotonic() >= deadline:
            raise RefreshError(f"the {VISUAL_CHECK} check on {sha[:12]} did not finish in time")
        sleep(POLL_SECONDS)


def refresh(
    gh: Gh = run_gh,
    *,
    timeout_minutes: int = 40,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_minutes * 60
    number, branch, marker_sha = create_marker_pull_request(gh, now or datetime.now(UTC))
    bot_sha = wait_for_bot_commit(gh, number, marker_sha, deadline, sleep)
    if bot_sha is None:
        gh(["pr", "close", str(number), "--repo", REPOSITORY, "--delete-branch"])
        return {"status": "NO_BASELINE_CHANGED", "pull_request": number}
    gh(["pr", "close", str(number), "--repo", REPOSITORY])
    gh(["pr", "reopen", str(number), "--repo", REPOSITORY])
    conclusion = wait_for_visual(gh, bot_sha, deadline, sleep)
    result: dict[str, Any] = {
        "status": "VERIFIED" if conclusion == "success" else "VISUAL_NOT_GREEN",
        "pull_request": number,
        "branch": branch,
        "baseline_commit": bot_sha,
        "visual": conclusion,
    }
    if conclusion == "success":
        result["merge_command"] = f"gh pr merge {number} --merge -R {REPOSITORY}"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout-minutes", type=int, default=40)
    arguments = parser.parse_args()
    try:
        result = refresh(timeout_minutes=arguments.timeout_minutes)
    except RefreshError as error:
        print(json.dumps({"status": "STOPPED", "reason": str(error)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"VERIFIED", "NO_BASELINE_CHANGED"} else 1


if __name__ == "__main__":
    sys.exit(main())
