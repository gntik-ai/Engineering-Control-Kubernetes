"""NEW-219: the gntik-ui changeset gate is judged on the PR's merge result.

The changed paths are the diff between the current base commit and GitHub's test merge of
the PR into it, read with the package manifests of the merge result (and of the base, for
paths that existed before the merge). These tests drive the real gate through a fake gh
that answers from real git repositories, so Git's own merge decides where edits land."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_new_160_gntik_ui_project import (
    CHANGED,
    GREEN,
    HEAD_SHA,
    MERGE_SHA,
    REPOSITORY,
    SAFE,
    _github,
    _MergeGh,
    _plan,
    _pull_request,
)
from test_staging_gitops import _config_root, _settings

from engineering_control.activities.gitops import GitOpsGitHubHandler
from engineering_control.adapters.command import CommandFailure

PRIVATE = {"name": "@gntik-ai/mcp", "private": True}
PUBLIC = {"name": "@gntik-ai/ui", "files": ["dist", "src"]}
_STATUS = {"A": "added", "D": "removed", "M": "modified", "T": "changed"}


GIT = shutil.which("git") or "/usr/bin/git"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed git binary on a test repository
        [GIT, "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


def _write(repo: Path, files: dict[str, str | dict[str, Any] | None]) -> None:
    for path, content in files.items():
        target = repo / path
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(content) if isinstance(content, dict) else content
        target.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


class _GitGh(_MergeGh):
    """_MergeGh's pull request, ready and merge handling, with the PR files, test merge,
    compare and file contents answered from a real repository (base ``main``, head
    ``pr``)."""

    def __init__(self, repo: Path) -> None:
        super().__init__([])
        self.repo = repo
        self.head = _git(repo, "rev-parse", "pr").stdout.strip()
        self.moved_base_after_gate = False
        # Files main changes just before the merge call (after the verdict): a raced base.
        self.race_on_merge: dict[str, str | dict[str, Any] | None] | None = None
        self.race_renames: list[tuple[str, str]] = []

    def _base(self) -> str:
        return _git(self.repo, "rev-parse", "main").stdout.strip()

    def _changes(self, *revisions: str) -> str:
        raw = _git(self.repo, "diff", "--name-status", "-M", *revisions).stdout
        lines = []
        for line in raw.splitlines():
            code, *paths = line.split("\t")
            if code[0] in "RC":
                status = "renamed" if code[0] == "R" else "copied"
                lines.append(f"{paths[1]}\t{status}\t{paths[0]}")
            else:
                lines.append(f"{paths[0]}\t{_STATUS[code[0]]}\t")
        return "\n".join(lines)

    def _test_merge(self) -> str | None:
        merged = _git(self.repo, "merge-tree", "--write-tree", "main", "pr", check=False)
        if merged.returncode != 0:
            return None
        tree = merged.stdout.splitlines()[0]
        return _git(
            self.repo, "commit-tree", tree, "-p", self._base(), "-p", self.head, "-m", "test merge"
        ).stdout.strip()

    def _answer(self, args: list[str]) -> str:
        if args[0] == "api" and args[1].endswith("/pulls/41/files"):
            return self._changes(f"{self._base()}...{self.head}")
        if args[0] == "api" and args[1] == f"repos/{REPOSITORY}/pulls/41":
            pull = self.pull_requests[(REPOSITORY, 41)]
            if pull["state"] == "MERGED":
                return json.dumps(
                    {
                        "mergeable": None,
                        "merged": True,
                        "merge_commit_sha": pull["mergeCommit"]["oid"],
                        "head_sha": self.head,
                    }
                )
            merge = self._test_merge()
            return json.dumps(
                {
                    "mergeable": merge is not None,
                    "merged": False,
                    "merge_commit_sha": merge,
                    "head_sha": self.head,
                }
            )
        if args[0] == "api" and "/git/commits/" in args[1]:
            sha = args[1].rsplit("/", 1)[1]
            return json.dumps(
                _git(self.repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()[1:]
            )
        if args[0] == "api" and args[1].startswith(f"repos/{REPOSITORY}/compare/"):
            base, merge = args[1].split("/compare/", 1)[1].split("?", 1)[0].split("...")
            changes = self._changes(base, merge)
            return "\n".join([str(len(changes.splitlines())), changes])
        if args[0] == "api" and args[1] == f"repos/{REPOSITORY}/commits/main":
            if self.moved_base_after_gate and any(
                call[:2] == ["pr", "ready"] for call in self.calls
            ):
                return json.dumps({"sha": "9" * 40, "tree": "f" * 40})
            return json.dumps({"sha": self._base(), "tree": "f" * 40})
        if args[:2] == ["pr", "merge"]:
            pull = self.pull_requests[(args[4], int(args[2]))]
            assert args[args.index("--match-head-commit") + 1] == pull["headRefOid"] == self.head
            if self.race_on_merge is not None or self.race_renames:
                for old, new in self.race_renames:
                    (self.repo / new).parent.mkdir(parents=True, exist_ok=True)
                    _git(self.repo, "mv", old, new)
                _write(self.repo, self.race_on_merge or {})
                _commit(self.repo, "main moves during the merge")
            _git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "pr")
            merge = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
            pull.update(state="MERGED", mergeCommit={"oid": merge})
            return ""
        if args[:3] == ["api", "-X", "GET"] and "/contents/" in args[3]:
            path = args[3].split("/contents/", 1)[1]
            ref = next(a.removeprefix("ref=") for a in args if a.startswith("ref="))
            shown = _git(self.repo, "show", f"{ref}:{path}", check=False)
            if shown.returncode != 0:
                raise CommandFailure("gh api failed: 404")
            return shown.stdout
        return super()._answer(args)


def _repository(
    tmp_path: Path,
    base: dict[str, str | dict[str, Any] | None],
    pr: dict[str, str | dict[str, Any] | None],
    *,
    main_after: dict[str, str | dict[str, Any] | None] | None = None,
    renames_on_main: list[tuple[str, str]] | None = None,
    renames_on_pr: list[tuple[str, str]] | None = None,
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _write(repo, base)
    _commit(repo, "base")
    _git(repo, "switch", "-q", "-c", "pr")
    for old, new in renames_on_pr or []:
        (repo / new).parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "mv", old, new)
    _write(repo, pr)
    _commit(repo, "pr")
    _git(repo, "switch", "-q", "main")
    for old, new in renames_on_main or []:
        (repo / new).parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "mv", old, new)
    if main_after or renames_on_main:
        _write(repo, main_after or {})
        _commit(repo, "main moves on")
    return repo


def _handler(tmp_path: Path, repo: Path) -> tuple[GitOpsGitHubHandler, _GitGh]:
    fake = _GitGh(repo)
    settings = _settings(
        tmp_path,
        project_config_root=_config_root(tmp_path, safe_repositories=SAFE),
        github_writes_enabled=True,
    )
    fake.pull_requests[(REPOSITORY, 41)] = {**_pull_request(GREEN), "headRefOid": fake.head}
    return GitOpsGitHubHandler(settings, runner=fake), fake  # type: ignore[arg-type]


async def _merge(handler: GitOpsGitHubHandler, fake: _GitGh) -> dict[str, Any]:
    return await handler.handle(
        "gitops.merge", {"plan": _plan(source_head_sha=fake.head), "target": "source"}
    )


BASE = {
    "packages/mcp/package.json": PRIVATE,
    "packages/mcp/src/tool.ts": "export const tool = 1\n",
    "packages/ui/package.json": PUBLIC,
    "packages/ui/src/button.tsx": "export const Button = 1\n",
}


async def test_a_private_only_change_merges_without_a_changeset(tmp_path: Path) -> None:
    # The PR edits the private package and its manifest (a script); private on both sides.
    repo = _repository(
        tmp_path,
        BASE,
        {
            "packages/mcp/src/tool.ts": "export const tool = 2\n",
            "packages/mcp/package.json": {**PRIVATE, "scripts": {"test": "node --test"}},
        },
    )
    handler, fake = _handler(tmp_path, repo)
    merged = await _merge(handler, fake)
    assert merged["status"] == "MERGED"


async def test_a_base_side_rename_into_a_published_package_needs_a_changeset(
    tmp_path: Path,
) -> None:
    """main moves the private tool into the published ui package while the PR edits it at
    its old path: Git applies the edit at the published destination."""
    lines = "".join(f"export const line{n} = {n}\n" for n in range(30))
    base = {**BASE, "packages/mcp/src/tool.ts": lines}
    repo = _repository(
        tmp_path,
        base,
        {"packages/mcp/src/tool.ts": lines.replace("line5 = 5", "line5 = 55")},
        renames_on_main=[("packages/mcp/src/tool.ts", "packages/ui/src/tool.ts")],
    )
    handler, fake = _handler(tmp_path, repo)
    refused = await _merge(handler, fake)
    assert refused == {"status": "BLOCKED", "blocker": "CHANGESET_MISSING"}
    assert not any(call[:2] == ["pr", "merge"] for call in fake.calls)


@pytest.mark.parametrize(
    "rename",
    [
        ("packages/mcp/src/tool.ts", "packages/ui/src/tool.ts"),  # into a published package
        ("packages/ui/src/button.tsx", "packages/mcp/src/button.tsx"),  # out of one
    ],
)
async def test_a_rename_across_the_publishable_boundary_needs_a_changeset(
    tmp_path: Path, rename: tuple[str, str]
) -> None:
    repo = _repository(tmp_path, BASE, {}, renames_on_pr=[rename])
    handler, fake = _handler(tmp_path, repo)
    refused = await _merge(handler, fake)
    assert refused == {"status": "BLOCKED", "blocker": "CHANGESET_MISSING"}


async def test_a_deleted_package_counts_by_its_base_manifest(tmp_path: Path) -> None:
    # Deleting the published package needs a changeset; deleting the private one does not.
    gone = {path: None for path in BASE if path.startswith("packages/ui/")}
    handler, fake = _handler(tmp_path / "ui", _repository(tmp_path / "ui", BASE, gone))
    assert (await _merge(handler, fake)) == {"status": "BLOCKED", "blocker": "CHANGESET_MISSING"}
    gone = {path: None for path in BASE if path.startswith("packages/mcp/")}
    handler, fake = _handler(tmp_path / "mcp", _repository(tmp_path / "mcp", BASE, gone))
    assert (await _merge(handler, fake))["status"] == "MERGED"


@pytest.mark.parametrize(
    "manifest",
    [
        ("packages/mcp/package.json", {**PRIVATE, "private": False}),  # becomes public
        ("packages/ui/package.json", {**PUBLIC, "private": True}),  # becomes private
    ],
)
async def test_a_package_that_changes_visibility_needs_a_changeset(
    tmp_path: Path, manifest: tuple[str, dict[str, Any]]
) -> None:
    path, value = manifest
    source = path.replace("package.json", "src/") + ("tool.ts" if "mcp" in path else "button.tsx")
    repo = _repository(tmp_path, BASE, {path: value, source: "export const changed = 1\n"})
    handler, fake = _handler(tmp_path, repo)
    refused = await _merge(handler, fake)
    assert refused == {"status": "BLOCKED", "blocker": "CHANGESET_MISSING"}


async def test_a_published_change_with_a_changeset_in_the_merge_passes(tmp_path: Path) -> None:
    repo = _repository(
        tmp_path,
        BASE,
        {
            "packages/ui/src/button.tsx": "export const Button = 2\n",
            ".changeset/brave-ducks.md": "---\n'@gntik-ai/ui': patch\n---\nFix.\n",
        },
    )
    handler, fake = _handler(tmp_path, repo)
    assert (await _merge(handler, fake))["status"] == "MERGED"


async def test_a_conflicting_merge_defers_to_the_conflict_handling(tmp_path: Path) -> None:
    repo = _repository(
        tmp_path,
        BASE,
        {"packages/ui/src/button.tsx": "export const Button = 'pr'\n"},
        main_after={"packages/ui/src/button.tsx": "export const Button = 'main'\n"},
    )
    handler, fake = _handler(tmp_path, repo)
    refused = await _merge(handler, fake)
    assert refused == {"status": "BLOCKED", "blocker": "SOURCE_PR_MERGE_CONFLICT"}
    assert not any(call[:2] == ["pr", "merge"] for call in fake.calls)


async def test_a_base_that_moves_after_the_gate_is_judged_again(tmp_path: Path) -> None:
    repo = _repository(tmp_path, BASE, {"packages/mcp/src/tool.ts": "export const tool = 2\n"})
    handler, fake = _handler(tmp_path, repo)
    fake.moved_base_after_gate = True
    pending = await _merge(handler, fake)
    assert pending == {"status": "MERGE_PENDING", "target": "source", "merge_state": "BASE_MOVED"}
    assert not any(call[:2] == ["pr", "merge"] for call in fake.calls)


@pytest.mark.parametrize(
    ("preview", "parents", "expected"),
    [
        ({"mergeable": None}, None, {"merge_state": "MERGE_RESULT_PENDING"}),
        ({"merge_commit_sha": None}, None, {"merge_state": "MERGE_RESULT_PENDING"}),
        ({"head_sha": "1" * 40}, None, {"merge_state": "MERGE_RESULT_STALE"}),
        # A test merge computed against an older base, or not of the bound head.
        ({}, ["2" * 40, HEAD_SHA], {"merge_state": "MERGE_RESULT_STALE"}),
        ({}, [MERGE_SHA, "3" * 40], {"merge_state": "MERGE_RESULT_STALE"}),
    ],
)
async def test_a_merge_result_github_has_not_computed_is_judged_later(
    tmp_path: Path,
    preview: dict[str, Any],
    parents: list[str] | None,
    expected: dict[str, str],
) -> None:
    handler, fake = _github(tmp_path, GREEN, CHANGED)
    fake.preview.update(preview)
    if parents is not None:
        fake.merge_parents = parents
    pending = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert pending == {"status": "MERGE_PENDING", "target": "source", **expected}
    assert fake.mutations == []


async def test_a_retried_merge_of_a_merged_pull_request_is_not_judged_again(
    tmp_path: Path,
) -> None:
    handler, fake = _github(tmp_path, GREEN, ["packages/ui/src/button.tsx"])
    fake.preview.update(merged=True, mergeable=None)
    fake.pull_requests[(REPOSITORY, 41)].update(state="MERGED", mergeCommit={"oid": MERGE_SHA})
    merged = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert merged["status"] == "MERGED"
    assert not any(call[:2] == ["pr", "merge"] for call in fake.calls)


async def test_a_manifest_with_a_repeated_key_keeps_its_package_counted(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path, GREEN, ["packages/mcp/src/tool.ts"])
    fake.head_files = {
        "packages/mcp/package.json": '{"private": false, "name": "@gntik-ai/mcp", "private": true}'
    }
    refused = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert refused == {"status": "BLOCKED", "blocker": "CHANGESET_MISSING"}


async def test_a_merge_diff_at_the_compare_cap_is_refused(tmp_path: Path) -> None:
    # GitHub's compare lists at most 300 files: a listing that reaches it may be truncated.
    changed = [f"packages/mcp/src/file{n}.ts" for n in range(300)]
    handler, fake = _github(tmp_path, GREEN, changed)
    refused = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert refused == {"status": "BLOCKED", "blocker": "MERGE_DELIVERY_FILE_LISTING_TRUNCATED"}
    assert fake.mutations == []


async def test_a_base_that_moves_during_the_merge_call_is_judged_on_the_actual_merge(
    tmp_path: Path,
) -> None:
    """GitHub's merge binds only the head: main may still move between the last base read
    and the merge call. The actual merge commit is judged again, and a raced move of the
    PR's private edit into a published package stops for the operator."""
    lines = "".join(f"export const line{n} = {n}\n" for n in range(30))
    base = {**BASE, "packages/mcp/src/tool.ts": lines}
    edit = {"packages/mcp/src/tool.ts": lines.replace("line5 = 5", "line5 = 55")}
    repo = _repository(tmp_path / "raced", base, edit)
    handler, fake = _handler(tmp_path / "raced", repo)
    fake.race_renames = [("packages/mcp/src/tool.ts", "packages/ui/src/tool.ts")]
    stopped = await _merge(handler, fake)
    assert stopped["status"] == "BLOCKED"
    assert stopped["blocker"] == "CHANGESET_MISSING_AFTER_MERGE"
    assert stopped["merge_sha"] == _git(repo, "rev-parse", "main").stdout.strip()
    # A retry finds the PR merged and goes on: the operator added the changeset on main.
    assert (await _merge(handler, fake))["status"] == "MERGED"
    # A race that leaves the PR's change private is no reason to stop.
    repo = _repository(tmp_path / "harmless", base, edit)
    handler, fake = _handler(tmp_path / "harmless", repo)
    fake.race_on_merge = {"packages/mcp/README.md": "notes\n"}
    assert (await _merge(handler, fake))["status"] == "MERGED"


async def test_a_dangling_symlink_or_an_empty_changeset_never_counts(tmp_path: Path) -> None:
    edit = {"packages/ui/src/button.tsx": "export const Button = 2\n"}
    repo = _repository(tmp_path / "link", BASE, edit)
    _git(repo, "switch", "-q", "pr")
    (repo / ".changeset").mkdir()
    (repo / ".changeset" / "release.md").symlink_to("missing.md")
    _commit(repo, "a dangling changeset link")
    _git(repo, "switch", "-q", "main")
    handler, fake = _handler(tmp_path / "link", repo)
    refused = await _merge(handler, fake)
    assert refused == {"status": "BLOCKED", "blocker": "CHANGESET_MISSING"}
    empty = {**edit, ".changeset/brave-ducks.md": "---\n---\nNothing released.\n"}
    repo = _repository(tmp_path / "empty", BASE, empty)
    handler, fake = _handler(tmp_path / "empty", repo)
    refused = await _merge(handler, fake)
    assert refused == {"status": "BLOCKED", "blocker": "CHANGESET_MISSING"}


@pytest.mark.parametrize(
    "listing",
    [
        "INVALID",  # files missing or null: never an empty diff
        "2\npackages/ui/src/button.tsx\tmodified\t",  # an entry lost
        "1\npackages/mcp/src/tool.ts\trenamed\t",  # a rename without its source
    ],
)
async def test_a_malformed_merge_diff_is_refused(tmp_path: Path, listing: str) -> None:
    handler, fake = _github(tmp_path, GREEN, CHANGED)
    original = fake._answer

    def answer(args: list[str]) -> str:
        if args[0] == "api" and "/compare/" in args[1]:
            return listing
        return original(args)

    fake._answer = answer  # type: ignore[method-assign]
    refused = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert refused == {"status": "BLOCKED", "blocker": "MERGE_DELIVERY_FILE_LISTING_TRUNCATED"}
    assert fake.mutations == []
