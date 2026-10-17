#!/usr/bin/env python3
"""NEW-220: refresh gntik-ui's visual baselines on the CI runner, then verify the result.

gntik-ui's ci.yml re-records the visual baselines only when a pull request's head commit
message contains ``[visual-update]`` (job ``visual-update``). It pushes them to the PR branch
as github-actions[bot], and that push starts no CI. Hermes makers may not do this (the
gntik-ui maker rules forbid the marker), so a stale baseline is the operator's to refresh.
This script runs that path and fails closed at every step:

1. an empty commit on top of main whose message carries the marker, on a fresh branch,
   and a pull request for it;
2. a wait for the visual-update job, which must succeed, and for at most one new commit;
3. for that commit: it sits directly on the marker; the repository activity log shows
   github-actions[bot] (the authenticated pusher, not commit metadata) pushed exactly it on
   top of the marker; every changed path, both sides of a rename included and across all
   pages, is under ``apps/docs/e2e/__screenshots__/``;
4. close and reopen the PR, so CI runs on the recorded baselines, then wait for a visual run
   that did not exist before the reopen;
5. a re-check that the PR is still open with exactly those two commits at that head, and a
   merge command pinned to it (``--match-head-commit``).

It never merges. Any stop prints what it created and how to undo it. Run it as the operator,
with the operator's own ``gh`` login:
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
# GitHub lists at most 3000 changed files for a commit, across all pages.
COMMIT_FILE_LIMIT = 3000
ALLOWED_FILE_STATUSES = frozenset({"added", "modified", "changed", "removed", "renamed"})

Gh = Callable[[list[str]], str]


class RefreshError(RuntimeError):
    """The refresh stopped; the message says why, the state what it left behind."""

    def __init__(self, message: str, state: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.state = dict(state or {})


def run_gh(arguments: list[str]) -> str:
    executable = shutil.which("gh")
    if executable is None:
        raise RefreshError("the GitHub CLI (gh) is not installed")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed gh calls built by this script
            [executable, *arguments], check=False, capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired as error:
        raise RefreshError(f"gh {' '.join(arguments[:3])} timed out") from error
    if completed.returncode != 0:
        raise RefreshError(f"gh {' '.join(arguments[:3])} failed: {completed.stderr.strip()}")
    return completed.stdout


class Refresh:
    def __init__(
        self,
        gh: Gh,
        *,
        timeout_minutes: int,
        now: datetime,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        self.gh = gh
        self.sleep = sleep
        self.clock = clock
        self.deadline = clock() + timeout_minutes * 60
        self.now = now
        self.state: dict[str, Any] = {"stage": "starting"}

    # -- helpers ------------------------------------------------------------------------

    def fail(self, message: str) -> RefreshError:
        return RefreshError(message, self.state)

    def json(self, arguments: list[str]) -> Any:
        try:
            return json.loads(self.gh(arguments))
        except RefreshError as error:
            raise self.fail(str(error)) from error
        except json.JSONDecodeError as error:
            raise self.fail(f"gh {' '.join(arguments[:2])} returned invalid JSON") from error

    def call(self, arguments: list[str]) -> str:
        try:
            return self.gh(arguments)
        except RefreshError as error:
            raise self.fail(str(error)) from error

    def wait(self, what: str) -> None:
        if self.clock() >= self.deadline:
            raise self.fail(f"timed out waiting for {what}")
        self.sleep(POLL_SECONDS)

    def pull_request(self, number: int) -> dict[str, Any]:
        return self.json(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                REPOSITORY,
                "--json",
                "headRefOid,state,commits",
                "--jq",
                "{head: .headRefOid, state, commits: [.commits[].oid]}",
            ]
        )

    def check_runs(self, sha: str, name: str) -> list[dict[str, Any]]:
        runs = self.json(
            [
                "api",
                f"repos/{REPOSITORY}/commits/{sha}/check-runs?per_page=100",
                "--jq",
                "[.check_runs[] | {id, name, status, conclusion}]",
            ]
        )
        return sorted(
            (run for run in runs if run.get("name") == name and isinstance(run.get("id"), int)),
            key=lambda run: run["id"],
        )

    # -- steps --------------------------------------------------------------------------

    def create(self) -> tuple[int, str, str]:
        main = self.json(
            ["api", f"repos/{REPOSITORY}/commits/main", "--jq", "{sha, tree: .commit.tree.sha}"]
        )
        message = (
            f"{MARKER} Re-record changed visual baselines on the CI runner\n\n"
            "Empty commit: the visual-update job re-records only the screenshots that fail "
            "their comparison and commits them to this branch."
        )
        marker = self.json(
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
                "{sha}",
            ]
        )["sha"]
        branch = f"visual-baseline-refresh-{self.now:%Y%m%d%H%M%S}"
        self.state.update(marker_commit=marker)
        self.call(
            [
                "api",
                "-X",
                "POST",
                f"repos/{REPOSITORY}/git/refs",
                "-f",
                f"ref=refs/heads/{branch}",
                "-f",
                f"sha={marker}",
            ]
        )
        self.state.update(stage="branch-created", branch=branch)
        url = self.call(
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
                "with the pinned command the script prints after its checks pass.",
            ]
        ).strip()
        try:
            number = int(url.rstrip("/").rsplit("/", 1)[1])
        except (IndexError, ValueError) as error:
            raise self.fail(f"gh pr create returned no PR URL: {url!r}") from error
        self.state.update(stage="pull-request-open", pull_request=number)
        return number, branch, marker

    def recorded_commit(self, number: int, marker: str) -> str | None:
        """The one commit the recording job pushed, or None when it recorded nothing."""
        nothing_seen = False
        while True:
            pull = self.pull_request(number)
            commits = pull.get("commits")
            runs = self.check_runs(marker, RECORD_JOB)
            record = runs[-1] if runs else None
            if pull.get("state") != "OPEN" or not isinstance(commits, list):
                raise self.fail(f"PR #{number} is no longer open")
            if not commits or commits[0] != marker:
                raise self.fail("the PR no longer starts with the marker commit")
            if len(commits) > 2 or pull.get("head") != commits[-1]:
                raise self.fail(f"the PR holds unexpected commits: {commits}")
            if record is not None and record.get("status") == "completed":
                if record.get("conclusion") != "success":
                    raise self.fail(f"the {RECORD_JOB} job concluded {record.get('conclusion')!r}")
                if len(commits) == 2:
                    return str(commits[1])
                if nothing_seen:
                    return None
                # The commit listing can lag the job's push: look once more first.
                nothing_seen = True
            self.wait(f"the {RECORD_JOB} job on {marker[:12]}")

    def verify(self, branch: str, marker: str, recorded: str) -> None:
        parents = self.json(
            ["api", f"repos/{REPOSITORY}/commits/{recorded}", "--jq", "[.parents[].sha]"]
        )
        if parents != [marker]:
            raise self.fail("the recorded commit does not sit directly on the marker commit")
        pushes = self.json(
            [
                "api",
                f"repos/{REPOSITORY}/activity?ref=refs/heads/{branch}"
                "&activity_type=push&per_page=100",
                "--jq",
                "[.[] | {before, after, actor: .actor.login}]",
            ]
        )
        if not any(
            push.get("before") == marker
            and push.get("after") == recorded
            and push.get("actor") == BOT_LOGIN
            for push in pushes
        ):
            raise self.fail(f"no push of {recorded[:12]} onto the marker by {BOT_LOGIN}")
        lines = self.call(
            [
                "api",
                "--paginate",
                f"repos/{REPOSITORY}/commits/{recorded}",
                "--jq",
                ".files[] | {filename, status, previous_filename}",
            ]
        ).splitlines()
        try:
            files = [json.loads(line) for line in lines if line.strip()]
        except json.JSONDecodeError as error:
            raise self.fail("the recorded commit's file list was not valid JSON") from error
        if not files:
            raise self.fail("the recorded commit changes no files")
        if len(files) >= COMMIT_FILE_LIMIT:
            raise self.fail("the recorded commit's file list may be incomplete")
        for entry in files:
            paths = [entry.get("filename"), entry.get("previous_filename")]
            if entry.get("status") not in ALLOWED_FILE_STATUSES or any(
                path is not None and not str(path).startswith(BASELINE_PREFIX) for path in paths
            ):
                raise self.fail(
                    f"the recorded commit changes a path outside the baselines: {entry}"
                )

    def visual_after_reopen(self, number: int, recorded: str) -> str:
        before = {run["id"] for run in self.check_runs(recorded, VISUAL_CHECK)}
        self.call(["pr", "close", str(number), "--repo", REPOSITORY])
        self.state.update(stage="pull-request-closed")
        self.call(["pr", "reopen", str(number), "--repo", REPOSITORY])
        self.state.update(stage="pull-request-open")
        while True:
            fresh = [
                run for run in self.check_runs(recorded, VISUAL_CHECK) if run["id"] not in before
            ]
            if fresh and fresh[-1].get("status") == "completed":
                return str(fresh[-1].get("conclusion") or "unknown")
            self.wait(f"a new {VISUAL_CHECK} run on {recorded[:12]}")

    def run(self) -> dict[str, Any]:
        number, branch, marker = self.create()
        recorded = self.recorded_commit(number, marker)
        if recorded is None:
            self.call(["pr", "close", str(number), "--repo", REPOSITORY, "--delete-branch"])
            self.state.update(stage="closed-nothing-recorded")
            return {"status": "NO_BASELINE_CHANGED", "pull_request": number}
        self.state.update(recorded_commit=recorded)
        self.verify(branch, marker, recorded)
        conclusion = self.visual_after_reopen(number, recorded)
        pull = self.pull_request(number)
        if (
            pull.get("state") != "OPEN"
            or pull.get("head") != recorded
            or pull.get("commits") != [marker, recorded]
        ):
            raise self.fail("the PR changed after verification; nothing was reported as safe")
        result: dict[str, Any] = {
            "status": "VERIFIED" if conclusion == "success" else "VISUAL_NOT_GREEN",
            "pull_request": number,
            "branch": branch,
            "baseline_commit": recorded,
            "visual": conclusion,
        }
        if conclusion == "success":
            result["merge_command"] = (
                f"gh pr merge {number} --merge --match-head-commit {recorded} -R {REPOSITORY}"
            )
        return result


def recovery(state: dict[str, Any]) -> list[str]:
    """Commands that undo or finish what a stopped refresh left behind."""
    number, branch, stage = state.get("pull_request"), state.get("branch"), state.get("stage")
    if number is not None and stage == "pull-request-closed":
        return [
            f"gh pr reopen {number} --repo {REPOSITORY}",
            f"gh pr close {number} --repo {REPOSITORY} --delete-branch",
        ]
    if number is not None and stage == "pull-request-open":
        return [f"gh pr close {number} --repo {REPOSITORY} --delete-branch"]
    if branch is not None and number is None:
        return [f"gh api -X DELETE repos/{REPOSITORY}/git/refs/heads/{branch}"]
    return []


def refresh(
    gh: Gh = run_gh,
    *,
    timeout_minutes: int = 40,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    flow = Refresh(
        gh, timeout_minutes=timeout_minutes, now=now or datetime.now(UTC), sleep=sleep, clock=clock
    )
    try:
        return flow.run()
    except RefreshError:
        raise
    except Exception as error:  # noqa: BLE001 - any stop must still report what it left
        raise RefreshError(f"unexpected error: {error!r}", flow.state) from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout-minutes", type=int, default=40)
    arguments = parser.parse_args()
    try:
        result = refresh(timeout_minutes=arguments.timeout_minutes)
    except RefreshError as error:
        report = {
            "status": "STOPPED",
            "reason": str(error),
            "left_behind": error.state,
            "recovery": recovery(error.state),
        }
        print(json.dumps(report, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"VERIFIED", "NO_BASELINE_CHANGED"} else 1


if __name__ == "__main__":
    sys.exit(main())
