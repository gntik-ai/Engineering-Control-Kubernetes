"""NEW-225: GitHub keeps a PR's test merge of an older base after the base moves.

gntik-ui #168 (PR #661): the test merge kept the old main as its first parent for over an
hour after main moved, so the NEW-219 changeset gate waited (MERGE_RESULT_STALE) until the
staging GitOps step blocked. With merge-forward the PR is now behind its base; without it,
the stale merge is judged when that provably gives the verdict of the merge into the
current base. These tests freeze the test merge at the old main of a real repository."""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any

import pytest
from test_new_160_gntik_ui_project import GREEN, REPOSITORY, SAFE, _plan, _pull_request
from test_new_219_changeset_merge_result import (
    BASE,
    PRIVATE,
    PUBLIC,
    _commit,
    _git,
    _GitGh,
    _merge,
    _repository,
    _write,
)
from test_staging_gitops import _config_root, _settings

from engineering_control.activities.gitops import GitOpsGitHubHandler
from engineering_control.adapters.command import CommandFailure
from engineering_control.models.reconciliation import MergeDeliveryPolicy

STALE = {"status": "MERGE_PENDING", "target": "source", "merge_state": "MERGE_RESULT_STALE"}
LINES = "".join(f"export const line{n} = {n}\n" for n in range(30))
EDIT = {"packages/ui/src/button.tsx": "export const Button = 2\n"}
CHANGESET = {".changeset/brave-ducks.md": "---\n'@gntik-ai/ui': patch\n---\nFix.\n"}


class _StaleGh(_GitGh):
    """_GitGh whose test merge stays the one built at main when the fake was made."""

    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.frozen = super()._test_merge()
        self.old_main = self._base()
        # GitHub may also keep listing the PR's files against the old base.
        self.files_at_old_base = False

    def _test_merge(self) -> str | None:
        return self.frozen

    def _answer(self, args: list[str]) -> str:
        if self.files_at_old_base and args[0] == "api" and args[1].endswith("/pulls/41/files"):
            return self._changes(f"{self.old_main}...{self.head}")
        if args[0] == "api" and "/compare/" in args[1] and ".files" not in args[-1]:
            base, head = args[1].split("/compare/", 1)[1].split("?", 1)[0].split("...")
            if "ahead_by" in args[-1]:
                ahead = _git(self.repo, "rev-list", "--count", f"{base}..{head}").stdout.strip()
                # GitHub reports one merge base; this fake reports the same one for equal sets.
                bases = _git(self.repo, "merge-base", "--all", base, head).stdout.split()
                return f"{ahead} {min(bases)}\n"
            if _git(self.repo, "merge-base", "--is-ancestor", base, head, check=False).returncode:
                return "diverged\n"
            return "identical\n" if base == head else "ahead\n"
        if args[0] == "api" and "/git/trees/" in args[1]:
            sha = args[1].split("/git/trees/", 1)[1].removesuffix("?recursive=1")
            listed = _git(self.repo, "ls-tree", "-r", "-t", sha).stdout.splitlines()
            return "".join(f"{line.split()[0]}\t{line.split(chr(9), 1)[1]}\n" for line in listed)
        if args[:3] == ["api", "-X", "GET"] and "/contents/" in args[3]:
            # GitHub's contents API follows a symlink to a file (git show does not).
            path = args[3].split("/contents/", 1)[1]
            ref = next(a.removeprefix("ref=") for a in args if a.startswith("ref="))
            entry = _git(self.repo, "ls-tree", ref, "--", path).stdout.split()
            if entry[:1] == ["120000"]:
                link = _git(self.repo, "show", f"{ref}:{path}").stdout
                target = posixpath.normpath(posixpath.join(posixpath.dirname(path), link))
                shown = _git(self.repo, "show", f"{ref}:{target}", check=False)
                if shown.returncode != 0:
                    raise CommandFailure("gh api failed: 404")
                return shown.stdout
        return super()._answer(args)


def _stale(
    tmp_path: Path, repo: Path, *, merge_forward: bool = False
) -> tuple[GitOpsGitHubHandler, _StaleGh]:
    projects = _config_root(tmp_path, safe_repositories=SAFE)
    if merge_forward:
        path = projects / "gntik-ui.yaml"
        text = path.read_text(encoding="utf-8")
        marker = "  merge_delivery:\n    enabled: true\n"
        assert marker in text
        path.write_text(text.replace(marker, marker + "    merge_forward: true\n"), "utf-8")
    settings = _settings(tmp_path, project_config_root=projects, github_writes_enabled=True)
    fake = _StaleGh(repo)
    fake.pull_requests[(REPOSITORY, 41)] = {**_pull_request(GREEN), "headRefOid": fake.head}
    return GitOpsGitHubHandler(settings, runner=fake), fake  # type: ignore[arg-type]


def _main_moves(
    repo: Path,
    files: dict[str, str | dict[str, Any] | None],
    renames: list[tuple[str, str]] | None = None,
) -> str:
    for old, new in renames or []:
        (repo / new).parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "mv", old, new)
    _write(repo, files)
    return _commit(repo, "main moves after the test merge")


def _linked(
    tmp_path: Path,
    base: dict[str, str | dict[str, Any] | None],
    links: dict[str, str],
    pr: dict[str, str | dict[str, Any] | None],
    pr_links: dict[str, str],
) -> Path:
    """_repository with symlinks (path -> link text) at the base and in the PR."""
    repo = _repository(tmp_path, {}, {})  # empty commits on main and pr
    for files, symlinks, branch in ((base, links, "main"), (pr, pr_links, "pr")):
        _git(repo, "switch", "-q", branch)
        if branch == "pr":
            _git(repo, "reset", "-q", "--hard", "main")
        _write(repo, files)
        for path, target in symlinks.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).symlink_to(target)
        _commit(repo, branch)
    _git(repo, "switch", "-q", "main")
    return repo


def _merged(fake: _StaleGh) -> bool:
    return any(call[:2] == ["pr", "merge"] for call in fake.calls)


async def test_a_stale_test_merge_with_merge_forward_is_behind_its_base(tmp_path: Path) -> None:
    repo = _repository(tmp_path, BASE, EDIT)
    handler, fake = _stale(tmp_path, repo, merge_forward=True)
    _main_moves(repo, {"apps/docs/notes.md": "notes\n"})
    assert (await _merge(handler, fake)) == {
        "status": "BLOCKED",
        "blocker": "SOURCE_PR_BEHIND_BASE",
    }
    assert not _merged(fake)


@pytest.mark.parametrize("files_at_old_base", [False, True])
async def test_a_provable_stale_test_merge_is_judged_for_the_current_base(
    tmp_path: Path, files_at_old_base: bool
) -> None:
    # Another slot's merge lands on main: a private package's file and its changeset.
    repo = _repository(tmp_path, BASE, {**EDIT, **CHANGESET})
    handler, fake = _stale(tmp_path, repo)
    fake.files_at_old_base = files_at_old_base
    current = _main_moves(
        repo,
        {
            "packages/mcp/src/tool.ts": "export const tool = 2\n",
            ".changeset/other-slot.md": "---\n'@gntik-ai/mcp': patch\n---\nOther.\n",
        },
    )
    plan, gate = handler._gate({"plan": _plan(source_head_sha=fake.head)})
    assert isinstance(gate, MergeDeliveryPolicy)
    assert handler._merge_only_files_gate(plan, gate) == (None, current)
    merged = await _merge(handler, fake)
    assert merged["status"] == "MERGED"
    assert _git(repo, "rev-list", "--parents", "-n", "1", "main").stdout.split()[1:] == [
        current,
        fake.head,
    ]


async def test_a_version_packages_base_change_is_provable(tmp_path: Path) -> None:
    """A Version Packages merge bumps only the version of the package the PR edits, writes
    its changelog and consumes the pending changesets: the publish decisions are unchanged,
    so the PR is judged, with or without its own changeset."""
    base = {
        **BASE,
        ".changeset/config.json": "{}",  # .changeset/ stays a directory, as in gntik-ui
        ".changeset/older-fix.md": "---\n'@gntik-ai/ui': patch\n---\nOld.\n",
    }
    version_packages = {
        "packages/ui/package.json": {**PUBLIC, "version": "1.0.1"},
        "packages/ui/CHANGELOG.md": "# @gntik-ai/ui\n\n## 1.0.1\n",
        ".changeset/older-fix.md": None,
    }
    repo = _repository(tmp_path / "with", base, {**EDIT, **CHANGESET})
    handler, fake = _stale(tmp_path / "with", repo)
    _main_moves(repo, version_packages)
    assert (await _merge(handler, fake))["status"] == "MERGED"
    repo = _repository(tmp_path / "without", base, EDIT)
    handler, fake = _stale(tmp_path / "without", repo)
    _main_moves(repo, version_packages)
    assert (await _merge(handler, fake)) == {"status": "BLOCKED", "blocker": "CHANGESET_MISSING"}
    assert not _merged(fake)


@pytest.mark.parametrize(
    ("pr", "main", "renames"),
    [
        # A rename in the base-side delta (directory renames could move PR paths).
        (EDIT, {}, [("packages/mcp/src/tool.ts", "packages/mcp/lib/tool.ts")]),
        # The base-side delta changes a path the PR changes (no conflict).
        (
            {"packages/mcp/src/tool.ts": LINES.replace("line5 = 5", "line5 = 55")},
            {"packages/mcp/src/tool.ts": LINES.replace("line25 = 25", "line25 = 2525")},
            [],
        ),
        # The PR's private package becomes public on main.
        (
            {"packages/mcp/src/tool.ts": "export const tool = 2\n"},
            {"packages/mcp/package.json": {**PRIVATE, "private": False}},
            [],
        ),
        # The package.json of the PR's package becomes unreadable.
        (EDIT, {"packages/ui/package.json": '{"name": "@gntik-ai/ui", "name": "x"}'}, []),
        # Workspace configuration the delta changes.
        (EDIT, {".changeset/config.json": "{}"}, []),
        (EDIT, {"package.json": {"name": "root", "private": True}}, []),
        (EDIT, {"pnpm-workspace.yaml": "packages:\n  - packages/*\n"}, []),
        # More files than GitHub's compare lists: a truncated delta.
        (EDIT, {f"apps/docs/page{n}.md": f"page {n}\n" for n in range(300)}, []),
    ],
)
async def test_a_stale_test_merge_that_is_not_provable_keeps_waiting(
    tmp_path: Path,
    pr: dict[str, str | dict[str, Any] | None],
    main: dict[str, str | dict[str, Any] | None],
    renames: list[tuple[str, str]],
) -> None:
    base = {**BASE, "packages/mcp/src/tool.ts": LINES}
    repo = _repository(tmp_path, base, {**pr, **CHANGESET})
    handler, fake = _stale(tmp_path, repo)
    _main_moves(repo, main, renames)
    assert (await _merge(handler, fake)) == STALE
    assert not _merged(fake)


async def test_a_rewritten_base_is_not_provable(tmp_path: Path) -> None:
    repo = _repository(tmp_path, BASE, EDIT, main_after={"apps/docs/notes.md": "one\n"})
    handler, fake = _stale(tmp_path, repo)
    _git(repo, "reset", "-q", "--hard", "HEAD~1")  # the old main is no longer an ancestor
    _main_moves(repo, {"apps/docs/notes.md": "two\n"})
    assert (await _merge(handler, fake)) == STALE


async def test_a_commit_the_head_shares_landing_on_main_is_not_provable(tmp_path: Path) -> None:
    # The PR merged a side branch that main merges later: the merge base differs.
    repo = _repository(tmp_path, BASE, EDIT)
    _git(repo, "switch", "-q", "-c", "side")
    _write(repo, {"apps/docs/side.md": "side\n"})
    _commit(repo, "side")
    _git(repo, "switch", "-q", "pr")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "side")
    _git(repo, "switch", "-q", "main")
    handler, fake = _stale(tmp_path, repo)
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "side")
    assert (await _merge(handler, fake)) == STALE


async def test_a_reverted_shared_change_landing_on_main_is_not_provable(tmp_path: Path) -> None:
    """The PR merged a side branch's change to a published file and reverted it; main
    merges that branch later. The merge into the current base reverts the published file,
    which the stale merge (and a file listing against the old base) never shows."""
    base = {**BASE, "packages/ui/src/shared.ts": "export const shared = 0\n"}
    repo = _repository(tmp_path, base, {"packages/mcp/src/tool.ts": "export const tool = 2\n"})
    _git(repo, "switch", "-q", "-c", "side", "main")
    _write(repo, {"packages/ui/src/shared.ts": "export const shared = 1\n"})
    _commit(repo, "side")
    _git(repo, "switch", "-q", "pr")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "side")
    _write(repo, {"packages/ui/src/shared.ts": "export const shared = 0\n"})
    _commit(repo, "revert the shared change")
    _git(repo, "switch", "-q", "main")
    handler, fake = _stale(tmp_path, repo)
    fake.files_at_old_base = True
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "side")
    assert (await _merge(handler, fake)) == STALE


async def test_a_criss_cross_merge_base_is_not_provable(tmp_path: Path) -> None:
    """Codex review (round 3): with several merge bases ort merges from a virtual one, so
    the trees of the single base GitHub reports prove nothing."""
    repo = _repository(tmp_path, BASE, {})
    for branch, files in (
        ("left", {"apps/docs/l.md": "l\n"}),
        ("right", {"apps/docs/r.md": "r\n"}),
    ):
        _git(repo, "switch", "-q", "-c", branch, "main")
        _write(repo, files)
        _commit(repo, branch)
    for branch, other in (("main", "left"), ("pr", "right")):
        _git(repo, "switch", "-q", branch)
        _git(repo, "reset", "-q", "--hard", "right" if branch == "main" else "left")
        _git(repo, "merge", "-q", "--no-ff", "--no-edit", other)
    _git(repo, "switch", "-q", "pr")
    _write(repo, {"packages/mcp/src/tool.ts": "export const tool = 2\n"})
    _commit(repo, "pr")
    _git(repo, "switch", "-q", "main")
    assert len(_git(repo, "merge-base", "--all", "main", "pr").stdout.split()) == 2
    handler, fake = _stale(tmp_path, repo)
    _main_moves(repo, {"apps/docs/notes.md": "notes\n"})
    assert (await _merge(handler, fake)) == STALE


async def test_a_stale_merge_that_follows_a_base_side_rename_is_not_provable(
    tmp_path: Path,
) -> None:
    # main renamed the file the PR edits before the test merge: the edit landed elsewhere.
    base = {**BASE, "packages/mcp/src/tool.ts": LINES}
    repo = _repository(
        tmp_path,
        base,
        {"packages/mcp/src/tool.ts": LINES.replace("line5 = 5", "line5 = 55")},
        renames_on_main=[("packages/mcp/src/tool.ts", "packages/mcp/lib/tool.ts")],
    )
    handler, fake = _stale(tmp_path, repo)
    _main_moves(repo, {"apps/docs/notes.md": "notes\n"})
    assert (await _merge(handler, fake)) == STALE


async def test_a_linked_manifest_whose_target_moves_is_not_provable(tmp_path: Path) -> None:
    """GitHub's contents read follows the private package's linked package.json to a file
    main then makes public: the merge into the current base needs a changeset."""
    base = {path: v for path, v in BASE.items() if path != "packages/mcp/package.json"}
    base["shared/mcp.json"] = PRIVATE
    repo = _linked(
        tmp_path,
        base,
        {"packages/mcp/package.json": "../../shared/mcp.json"},
        {"packages/mcp/src/tool.ts": "export const tool = 2\n"},
        {},
    )
    handler, fake = _stale(tmp_path, repo)
    _main_moves(repo, {"shared/mcp.json": {**PRIVATE, "private": False}})
    assert (await _merge(handler, fake)) == STALE
    assert not _merged(fake)


async def test_a_linked_changeset_whose_target_moves_is_not_provable(tmp_path: Path) -> None:
    release = "---\n'@gntik-ai/ui': patch\n---\nFix.\n"
    repo = _linked(
        tmp_path,
        {**BASE, "apps/docs/release.md": release},
        {},
        EDIT,
        {".changeset/brave-ducks.md": "../apps/docs/release.md"},
    )
    handler, fake = _stale(tmp_path, repo)
    _main_moves(repo, {"apps/docs/release.md": "No release.\n"})
    assert (await _merge(handler, fake)) == STALE
    assert not _merged(fake)


async def test_a_base_side_rename_git_pairs_differently_is_not_provable(
    tmp_path: Path,
) -> None:
    """Codex review (round 2): main replaced the file the PR edits with a near copy that
    already holds the edit, so the stale merge changes nothing. The current base adds an
    exact copy of the original inside a published package; Git pairs that rename instead and
    applies the PR's edit there, which needs a changeset."""
    edited = LINES.replace("line5 = 5", "line5 = 55")
    repo = _repository(
        tmp_path,
        {**BASE, "apps/docs/a.txt": LINES},
        {"apps/docs/a.txt": edited},
        main_after={
            "apps/docs/a.txt": None,
            "apps/docs/b.txt": edited,
            "packages/ui/src/c.ts": "export const c = 1\n",
        },
    )
    handler, fake = _stale(tmp_path, repo)
    _main_moves(repo, {"packages/ui/src/c.ts": LINES})
    assert (await _merge(handler, fake)) == STALE
    assert not _merged(fake)


async def test_a_pr_file_in_a_directory_main_renamed_away_is_not_provable(
    tmp_path: Path,
) -> None:
    # main removed old/ and the current base adds its files elsewhere: a directory rename.
    old = {f"packages/mcp/src/old/{name}.ts": LINES.replace("0", name) for name in ("x", "y")}
    repo = _repository(
        tmp_path,
        {**BASE, **old},
        {"packages/mcp/src/old/new.ts": "export const added = 1\n"},
        main_after=dict.fromkeys(old),
    )
    handler, fake = _stale(tmp_path, repo)
    _main_moves(repo, {path.replace("mcp/src/old", "ui/src/old"): v for path, v in old.items()})
    assert (await _merge(handler, fake)) == STALE


async def test_a_base_file_in_a_directory_the_pr_renamed_away_is_not_provable(
    tmp_path: Path,
) -> None:
    # The PR moves old/ into the published package; main then adds a file to old/.
    old = {f"packages/mcp/src/old/{name}.ts": LINES.replace("0", name) for name in ("x", "y")}
    repo = _repository(
        tmp_path,
        {**BASE, **old},
        CHANGESET,
        renames_on_pr=[(path, path.replace("mcp/src/old", "ui/src/old")) for path in old],
    )
    handler, fake = _stale(tmp_path, repo)
    _main_moves(repo, {"packages/mcp/src/old/z.ts": "export const z = 1\n"})
    assert (await _merge(handler, fake)) == STALE


async def test_a_fresh_test_merge_reads_no_stale_proof(tmp_path: Path) -> None:
    repo = _repository(tmp_path, BASE, EDIT)
    handler, fake = _stale(tmp_path, repo)
    assert (await _merge(handler, fake)) == {"status": "BLOCKED", "blocker": "CHANGESET_MISSING"}
    reads = [" ".join(call) for call in fake.calls]
    assert not any("/git/trees/" in read or "ahead_by" in read for read in reads)
