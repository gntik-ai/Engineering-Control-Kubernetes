"""NEW-243 phase 0: cross-runner ownership of ChangeSet worktrees.

Any provider's runner may write a worktree after any other (a retry or a correction whose
maker provider changed, a CI repair by the other provider). The writer restores the ACL
mask on its own files after its step, and the manager hands the worktree to a new writer by
rebuilding its checkout from Git; the checker stays read-only.
"""

from __future__ import annotations

import asyncio
import os
import pwd
import shutil
import stat
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest

from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.agents.instructions import InstructionBundle
from engineering_control.config import Settings
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import ModelClass, Project, Provider, RunStatus
from engineering_control.observability.agent import AgentTranscriptRecorder
from engineering_control.worktrees import ownership, ownership_repair
from engineering_control.worktrees.manager import (
    GitWorktreeManager,
    WorktreeAccess,
)
from engineering_control.worktrees.ownership import (
    Budget,
    normalise_owned_tree,
    normalised_mode,
    walk,
)

GIT = shutil.which("git") or "/usr/bin/git"
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "normalise-worktree-ownership.sh"


def git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(  # noqa: S603 - fixed executable and test-controlled arguments
        [GIT, "-c", "user.name=t", "-c", "user.email=t@invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return str(result.stdout).strip()


# --- the mode plan ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (stat.S_IFDIR | 0o700, 0o770),  # mkdtemp-style directory: mask --- -> rwx
        (stat.S_IFDIR | 0o755, 0o775),  # tar/pnpm directory: mask r-x -> rwx
        (stat.S_IFREG | 0o600, 0o660),  # mkstemp-style file
        (stat.S_IFREG | 0o644, 0o664),
        (stat.S_IFREG | 0o444, 0o464),  # read-only stays read-only for its owner
        (stat.S_IFREG | 0o755, 0o775),  # executable keeps execute in the mask
        (stat.S_IFREG | 0o640, 0o660),  # the runner units' UMask=0027 default
        (stat.S_IFREG | 0o666, 0o664),  # never world-writable
        (stat.S_IFDIR | 0o777, 0o775),
        (stat.S_IFDIR | 0o1777, 0o1775),  # sticky kept, other write removed
    ],
)
def test_normalised_mode_restores_the_mask_and_never_widens_other(mode: int, expected: int) -> None:
    result = normalised_mode(mode)
    assert result == expected
    assert not result & stat.S_IWOTH
    # "other" read/execute is never added.
    assert result & (stat.S_IROTH | stat.S_IXOTH) == stat.S_IMODE(mode) & (
        stat.S_IROTH | stat.S_IXOTH
    )


def test_normalise_owned_tree_repairs_only_owned_entries_and_skips_symlinks(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "worktree"
    (tree / "dist" / "private").mkdir(parents=True)
    (tree / "dist" / "bundle.js").write_text("x")
    (tree / "dist" / "bundle.js").chmod(0o600)
    (tree / "dist" / "private").chmod(0o700)
    (tree / "dist").chmod(0o755)
    outside = tmp_path / "outside"
    outside.write_text("y")
    outside.chmod(0o600)
    (tree / "link").symlink_to(outside)

    report = normalise_owned_tree(tree)

    assert stat.S_IMODE((tree / "dist" / "bundle.js").stat().st_mode) == 0o660
    assert stat.S_IMODE((tree / "dist" / "private").stat().st_mode) == 0o770
    assert stat.S_IMODE((tree / "dist").stat().st_mode) == 0o775
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600  # never through the symlink
    assert report.failed == 0 and report.changed >= 3
    # Idempotent.
    assert normalise_owned_tree(tree).changed == 0

    (tree / "dist" / "bundle.js").chmod(0o600)
    foreign = normalise_owned_tree(tree, owner=os.geteuid() + 12345)
    assert foreign.changed == 0  # another runner's files are not this runner's to change
    assert stat.S_IMODE((tree / "dist" / "bundle.js").stat().st_mode) == 0o600


# --- the grant: the checker reads, only the maker writes ---------------------------------

CLAUDE_MAKER = WorktreeAccess("claude-runner", "codex-runner")


def test_checker_stays_read_only_in_the_grant() -> None:
    # The hand-off changes who writes, never that the checker only reads: _grant_access
    # gives the maker rwX and the checker r-X, and both must differ (R3 cross-provider).
    with pytest.raises(ValueError, match="must differ"):
        WorktreeAccess("claude-runner", "claude-runner")


# --- the root-only repair ---------------------------------------------------------------

IDENTITY = "11111111-2222-3333-4444-555555555555"


def _me() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


def _platform(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "srv"
    worktree = root / "worktrees" / "falcone" / IDENTITY
    (worktree / "dist").mkdir(parents=True)
    (worktree / "dist" / "bundle.js").write_text("x")
    (worktree / "dist" / "bundle.js").chmod(0o600)
    (worktree / "dist").chmod(0o700)
    (root / "worktrees" / "falcone" / "not-a-changeset").mkdir()
    metadata = root / "cache" / "scan" / "worktree-git" / "falcone" / f"{IDENTITY}.git"
    for sub in ("objects/info", "objects/ab", "refs/heads/agent", "logs/refs/heads/agent"):
        (metadata / sub).mkdir(parents=True)
    (metadata / "objects" / "ab" / "cdef").write_text("o")
    (metadata / "objects" / "ab" / "cdef").chmod(0o400)
    (metadata / "objects" / "info").chmod(0o700)
    (metadata / "logs" / "refs" / "heads" / "agent" / "x").write_text("log")
    (metadata / "logs" / "refs" / "heads" / "agent" / "x").chmod(0o600)
    metadata.chmod(0o750)
    return root, worktree, metadata


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_repair_is_a_bounded_dry_run_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, worktree, metadata = _platform(tmp_path)
    (root / "worktrees" / "falcone" / "aaaaaaaa-2222-3333-4444-555555555555").symlink_to(tmp_path)

    # A refused (symlinked) ChangeSet entry makes the run non-zero; the rest is listed.
    assert ownership_repair.main(["--platform-root", str(root), "--manager", _me()]) == 3

    out = capsys.readouterr().out
    assert "dry run: 1 worktree(s), 1 metadata repositories" in out
    assert "not-a-changeset" not in out
    assert "refused worktrees/falcone/aaaaaaaa-2222-3333-4444-555555555555" in out
    assert f"would set mode 0770 on worktrees/falcone/{IDENTITY}/dist" in out
    assert "objects/info" not in out  # sealed: never touched
    assert _mode(worktree / "dist") == 0o700  # nothing changed


def test_repair_applies_modes_but_never_the_sealed_parts(tmp_path: Path) -> None:
    root, worktree, metadata = _platform(tmp_path)
    report = ownership_repair.repair(root, _me(), apply=True)
    assert report.worktrees == 1 and report.metadata == 1 and not report.refused
    assert _mode(worktree / "dist") == 0o770
    assert _mode(worktree / "dist" / "bundle.js") == 0o660
    assert _mode(metadata / "objects" / "ab" / "cdef") == 0o440  # objects stay unwritable
    assert _mode(metadata / "logs" / "refs" / "heads" / "agent" / "x") == 0o660
    assert _mode(metadata / "objects" / "info") == 0o700
    assert _mode(metadata) == 0o750
    assert ownership_repair.repair(root, _me(), apply=True).modes == 0  # idempotent


def test_repair_refuses_to_apply_without_root(capsys: pytest.CaptureFixture[str]) -> None:
    if os.geteuid() == 0:
        pytest.skip("runs as root")
    assert ownership_repair.main(["--apply"]) == 1
    assert "must run as root" in capsys.readouterr().err


def test_repair_wrapper_runs_the_module_isolated(tmp_path: Path) -> None:
    echo = tmp_path / "python"
    echo.write_text('#!/bin/sh\necho "$@"\n')
    echo.chmod(0o755)
    result = subprocess.run(  # noqa: S603 - the script under test
        ["/usr/bin/bash", str(SCRIPT), "--apply"],
        env={**os.environ, "ENGINEERING_PYTHON": str(echo)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "-I -m engineering_control.worktrees.ownership_repair --apply"


def test_repair_never_follows_an_intermediate_symlink(tmp_path: Path) -> None:
    """A maker-controlled ``logs/refs`` (or ``logs``) pointing at a mirror's refs: root must
    neither chmod nor chown anything there."""
    root, _worktree, metadata = _platform(tmp_path)
    mirror_refs = tmp_path / "mirrors" / "falcone.git" / "refs"
    (mirror_refs / "heads").mkdir(parents=True)
    (mirror_refs / "heads" / "main").write_text("sha")
    (mirror_refs / "heads" / "main").chmod(0o600)
    (mirror_refs / "heads").chmod(0o700)
    shutil.rmtree(metadata / "logs" / "refs")
    (metadata / "logs" / "refs").symlink_to(mirror_refs)
    (metadata / "refs").rename(metadata / "refs-real")
    (metadata / "refs").symlink_to(mirror_refs)

    report = ownership_repair.repair(root, _me(), apply=True)

    assert _mode(mirror_refs / "heads" / "main") == 0o600
    assert _mode(mirror_refs / "heads") == 0o700
    assert any(line.endswith("/refs: not a real directory") for line in report.refused)


def test_repair_refuses_a_symlinked_ancestor(tmp_path: Path) -> None:
    real, worktree, _metadata = _platform(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(OSError):
        ownership_repair.repair(link, _me(), apply=True)
    assert ownership_repair.main(["--platform-root", str(link), "--manager", _me()]) == 1
    assert _mode(worktree / "dist") == 0o700


# --- the writer's post-step normalisation (provider activity) ---------------------------


class BuildingAdapter:
    """Builds like a maker whose tools ask for private modes (mkdtemp, 0600 outputs)."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.events: list[dict[str, Any]] = []

    async def authenticated(self) -> bool:
        return True

    async def execute(self, request: AgentRequest, on_event: Any = None, **_: Any) -> Any:
        dist = Path(request.working_directory) / "dist"
        dist.mkdir(mode=0o700)
        (dist / "bundle.js").write_text("built")
        (dist / "bundle.js").chmod(0o600)
        now = datetime.now(UTC)
        return AgentResult(
            run_id=request.run_id,
            provider=request.provider,
            role=request.role,
            status=RunStatus.SUCCESS,
            summary="ok",
            structured_result={"summary": "ok"},
            started_at=now,
            finished_at=now,
        )


class PassThroughPack:
    def enrich(self, request: AgentRequest, _step: str) -> tuple[AgentRequest, InstructionBundle]:
        return request, InstructionBundle(
            prompt_prefix="", sha256="a" * 64, revision=None, sources=("x",), allowed_tools=()
        )


def _provider_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> tuple[ProviderActivityHandler, Path]:
    monkeypatch.setattr(
        "engineering_control.activities.provider.activity.heartbeat", lambda _: None
    )
    worktree = tmp_path / "worktrees" / "gntik-ui" / name
    worktree.mkdir(parents=True)
    handler = ProviderActivityHandler(
        Settings(
            worktree_root=tmp_path / "worktrees",
            worktree_metadata_root=tmp_path / "metadata",
            mirror_root=tmp_path / "mirrors",
            run_root=tmp_path / "runs",
            temporary_root=tmp_path / "tmp",
        ),
        "claude",
        BuildingAdapter(Provider.CLAUDE),
        PassThroughPack(),
    )
    return handler, worktree


def _agent_request(worktree: Path, *, writable: bool, role: str) -> dict[str, Any]:
    return AgentRequest(
        project=Project.GNTIK_UI,
        role=role,
        provider=Provider.CLAUDE,
        model_class=ModelClass.WORKER,
        prompt="perform a bounded synthetic task",
        working_directory=str(worktree),
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        },
        writable=writable,
        allowed_tools=("Read", "Glob", "Grep", "Bash"),
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_maker_restores_the_mask_on_what_it_built_in_its_changeset_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, worktree = _provider_handler(tmp_path, monkeypatch, str(uuid4()))
    result = await handler.handle(
        "delivery.invoke_maker",
        {
            "maker_request": _agent_request(worktree, writable=True, role="implementer"),
            "toolchain_preparation": {"status": "TOOLCHAIN_READY"},
        },
    )
    assert result["status"] == "SUCCESS"
    assert stat.S_IMODE((worktree / "dist").stat().st_mode) == 0o770
    assert stat.S_IMODE((worktree / "dist" / "bundle.js").stat().st_mode) == 0o660


@pytest.mark.asyncio
@pytest.mark.parametrize("changeset", [True, False])
async def test_checkers_and_non_changeset_directories_are_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changeset: bool
) -> None:
    name = str(uuid4()) if changeset else "source"
    handler, worktree = _provider_handler(tmp_path, monkeypatch, name)
    step, key, writable = (
        ("delivery.invoke_checker", "checker_request", False)
        if changeset
        else ("delivery.invoke_maker", "maker_request", True)
    )
    role = "code-reviewer" if changeset else "implementer"
    await handler.handle(
        step,
        {
            key: _agent_request(worktree, writable=writable, role=role),
            "toolchain_preparation": {"status": "TOOLCHAIN_READY"},
        },
    )
    assert stat.S_IMODE((worktree / "dist").stat().st_mode) == 0o700
    assert stat.S_IMODE((worktree / "dist" / "bundle.js").stat().st_mode) == 0o600


# --- review regressions: races, mounts, budgets, checker -----------------------------------


def test_an_entry_swapped_for_a_symlink_after_listing_is_never_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "worktree"
    tree.mkdir()
    tree.chmod(0o770)  # already normalised, whatever umask the suite runs under
    victim = tree / "victim"
    victim.write_text("x")
    victim.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_text("secret")
    outside.chmod(0o600)
    real_open = os.open

    def swapping_open(
        path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == "victim" and dir_fd is not None and os.path.lexists(victim):
            victim.unlink()
            victim.symlink_to(outside)  # the swap lands between listing and opening
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)
    report = normalise_owned_tree(tree)
    monkeypatch.undo()
    assert report.raced == 1 and report.changed == 0 and not report.complete
    assert _mode(outside) == 0o600


def test_an_entry_replaced_by_another_inode_after_listing_is_never_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "worktree"
    tree.mkdir()
    victim = tree / "victim"
    victim.write_text("x")
    victim.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_text("secret")
    outside.chmod(0o600)
    real_open = os.open

    def replacing_open(
        path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if (
            path == "victim"
            and dir_fd is not None
            and victim.stat().st_ino != outside.stat().st_ino
        ):
            victim.unlink()
            os.link(outside, victim)  # same name, another (outside) inode
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replacing_open)
    report = normalise_owned_tree(tree)
    monkeypatch.undo()
    assert report.raced == 1
    assert _mode(outside) == 0o600


def _on_other_device(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    real_stat = os.stat

    def stat_with_mount(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)
        if path == name and kwargs.get("dir_fd") is not None:
            fields = list(result)
            fields[2] = result.st_dev + 1  # st_dev: as if a filesystem were mounted here
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(os, "stat", stat_with_mount)


def test_entries_on_another_filesystem_are_neither_changed_nor_entered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "worktree"
    (tree / "mnt").mkdir(parents=True)
    tree.chmod(0o770)  # already normalised, whatever umask the suite runs under
    (tree / "mnt" / "inner").write_text("x")
    (tree / "mnt" / "inner").chmod(0o600)
    (tree / "mnt").chmod(0o700)
    _on_other_device(monkeypatch, "mnt")
    report = normalise_owned_tree(tree)
    monkeypatch.undo()
    assert report.changed == 0
    assert _mode(tree / "mnt") == 0o700 and _mode(tree / "mnt" / "inner") == 0o600
    # A skipped subtree is not a complete walk.
    assert not report.complete and report.reason == "foreign_device"
    assert report.foreign_device == 1


def test_repair_exits_non_zero_when_a_mount_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, worktree, _metadata = _platform(tmp_path)
    _on_other_device(monkeypatch, "dist")
    code = ownership_repair.main(["--platform-root", str(root), "--manager", _me()])
    monkeypatch.undo()
    captured = capsys.readouterr()
    assert code == 3
    assert f"incomplete worktrees/falcone/{IDENTITY}: foreign_device" in captured.out
    assert _mode(worktree / "dist") == 0o700


@pytest.mark.parametrize(
    ("budget", "reason"),
    [
        (Budget(max_entries=1), "entries"),
        (Budget(deadline=0.0), "deadline"),
        (Budget(stop=threading.Event()), "stopped"),
    ],
)
def test_a_walk_stops_at_its_budget(tmp_path: Path, budget: Budget, reason: str) -> None:
    if budget.stop is not None:
        budget.stop.set()
    tree = tmp_path / "worktree"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "a" / "b" / "c").write_text("x")
    report = normalise_owned_tree(tree, budget=budget)
    assert not report.complete and report.reason == reason


def _posix_acl_permits(
    *,
    user: str,
    owner: str,
    named: dict[str, str],
    mask: str,
    other: str,
    want: str,
) -> bool:
    """POSIX.1e access check for one user (no group membership: runners are alone in
    their private groups)."""
    if user == owner:
        granted = "rwx"  # owner class: the file mode's user bits (rw for these files)
    elif user in named:
        granted = "".join(
            bit if bit == maskbit else "-" for bit, maskbit in zip(named[user], mask, strict=True)
        )
    else:
        granted = other
    return want in granted


def test_the_checker_runner_cannot_write_the_worktree(
    tmp_path: Path,
) -> None:
    """Distinct users, simulated: the ACL _grant_access writes for a pair, evaluated for
    each identity on files the manager owns (the checkout Git created)."""
    worktree_root = tmp_path / "worktrees"
    target = worktree_root / "falcone" / str(uuid4())
    target.mkdir(parents=True)
    metadata = tmp_path / "metadata" / "falcone" / f"{target.name}.git"
    metadata.mkdir(parents=True)
    canonical = tmp_path / "mirrors" / "falcone.git"
    canonical.mkdir(parents=True)
    manager = GitWorktreeManager(
        mirror_root=tmp_path / "mirrors",
        worktree_root=worktree_root,
        lease_root=tmp_path / "leases",
        metadata_root=tmp_path / "metadata",
    )
    commands: list[list[str]] = []

    def record(args: list[str], cwd: Path, **_: object) -> str:
        commands.append(args)
        return ""

    manager._run = record  # type: ignore[method-assign]
    manager._grant_access(target, metadata, canonical, CLAUDE_MAKER)
    (acl,) = [
        c[-2]
        for c in commands
        if c[:3] == ["setfacl", "--recursive", "--modify"] and c[-1] == str(target)
    ]
    named = {
        name: perms.replace("X", "x")
        for name, perms in (entry.split(":")[1:] for entry in acl.split(",") if entry[0] == "u")
    }
    mask = next(entry.split(":")[2] for entry in acl.split(",") if entry.startswith("m:"))
    manager_name = pwd.getpwuid(os.geteuid()).pw_name
    files = {"owner": manager_name, "named": named, "mask": mask.replace("X", "x"), "other": "---"}
    assert _posix_acl_permits(user="claude-runner", want="w", **files)  # type: ignore[arg-type]
    assert not _posix_acl_permits(user="codex-runner", want="w", **files)  # type: ignore[arg-type]
    assert _posix_acl_permits(user="codex-runner", want="r", **files)  # type: ignore[arg-type]
    assert not _posix_acl_permits(user="nobody", want="r", **files)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_post_step_normalisation_is_bounded_and_never_blocks_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, worktree = _provider_handler(tmp_path, monkeypatch, str(uuid4()))
    handler._normalise_seconds = 0.05
    handler._normalise_grace_seconds = 0.05
    stopped: list[bool] = []

    def hanging(path: Path, stop: Any) -> None:
        stopped.append(stop.wait(5))  # a walk stuck until it is told to stop

    monkeypatch.setattr(handler, "_normalise_changeset_worktree", hanging)
    events: list[dict[str, Any]] = []

    async def collect(event: dict[str, Any]) -> None:
        events.append(event)

    request = AgentRequest.model_validate(
        _agent_request(worktree, writable=True, role="implementer")
    )
    started = time.monotonic()
    await handler._normalise_worktree_ownership(request, collect)
    assert time.monotonic() - started < 2
    assert events[-1]["status"] == "INCOMPLETE" and events[-1]["reason"] == "timeout"
    for _ in range(100):
        if stopped:
            break
        await asyncio.sleep(0.01)
    assert stopped == [True]  # the stuck walk was told to stop


@pytest.mark.asyncio
async def test_a_cancelled_maker_is_not_held_by_the_normalisation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, worktree = _provider_handler(tmp_path, monkeypatch, str(uuid4()))

    async def cancelled(*_: Any, **__: Any) -> Any:
        raise asyncio.CancelledError

    detached: list[str] = []
    monkeypatch.setattr(handler.adapter, "execute", cancelled)
    monkeypatch.setattr(
        handler, "_normalise_detached", lambda request: detached.append(request.working_directory)
    )

    async def awaited(*_: Any) -> None:
        raise AssertionError("cancellation cleanup must not await the normalisation")

    monkeypatch.setattr(handler, "_normalise_worktree_ownership", awaited)
    request = AgentRequest.model_validate(
        _agent_request(worktree, writable=True, role="implementer")
    )
    with pytest.raises(asyncio.CancelledError):
        await handler._execute(request, lambda event: asyncio.sleep(0), None)
    assert detached == [str(worktree)]


# --- incremental enumeration and incomplete walks ----------------------------------------


def test_enumeration_stops_at_the_budget_in_a_huge_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "worktree"
    tree.mkdir()
    consumed = 0

    class Endless:
        def __iter__(self) -> Endless:
            return self

        def __next__(self) -> Any:
            nonlocal consumed
            consumed += 1
            return type("Entry", (), {"name": f"f{consumed}"})()

        def close(self) -> None:
            return None

    monkeypatch.setattr(os, "scandir", lambda descriptor: Endless())
    descriptor = os.open(tree, os.O_PATH | os.O_DIRECTORY)
    try:
        report = walk(descriptor, Budget(max_entries=50), lambda *_: None)
    finally:
        os.close(descriptor)
        monkeypatch.undo()
    assert not report.complete and report.reason == "entries"
    assert consumed <= 50


def test_an_unlistable_root_is_incomplete(tmp_path: Path) -> None:
    tree = tmp_path / "worktree"
    (tree / "inner").mkdir(parents=True)
    tree.chmod(0o300)  # owner may search and change it, but not list it
    try:
        report = normalise_owned_tree(tree)
    finally:
        tree.chmod(0o755)
    assert not report.complete and report.unlisted == 1 and report.reason == "unlisted"


def test_repair_exits_non_zero_when_a_walk_is_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, worktree, _metadata = _platform(tmp_path)
    worktree.chmod(0o300)
    try:
        code = ownership_repair.main(["--platform-root", str(root), "--manager", _me()])
    finally:
        worktree.chmod(0o755)
    captured = capsys.readouterr()
    assert code == 3
    assert f"incomplete worktrees/falcone/{IDENTITY}: unlisted" in captured.out
    assert "NOT COMPLETE" in captured.err


# --- final review: mount identity, bounded publication, transcript --------------------------


def _bind_mounted(monkeypatch: pytest.MonkeyPatch, name: str, value: int | None = 2) -> None:
    """As if ``name`` were a bind mount of the same filesystem: same st_dev, another mount
    id (``value``; None: the mount id cannot be read)."""
    real = ownership.mount_id

    def fake(descriptor: int) -> int | None:
        if os.readlink(f"/proc/self/fd/{descriptor}").endswith(f"/{name}"):
            return value
        return real(descriptor)

    monkeypatch.setattr(ownership, "mount_id", fake)
    monkeypatch.setattr(ownership_repair, "mount_id", fake)


def test_mount_id_is_read_from_a_path_descriptor(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path, os.O_PATH | os.O_DIRECTORY)
    try:
        assert isinstance(ownership.mount_id(descriptor), int)
    finally:
        os.close(descriptor)


def test_a_same_device_bind_mount_is_neither_changed_nor_entered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "worktree"
    (tree / "bind").mkdir(parents=True)
    (tree / "bind" / "inner").write_text("x")
    (tree / "bind" / "inner").chmod(0o600)
    (tree / "bind").chmod(0o700)
    (tree / "own").write_text("y")
    (tree / "own").chmod(0o600)
    _bind_mounted(monkeypatch, "bind")
    report = normalise_owned_tree(tree)
    monkeypatch.undo()
    assert _mode(tree / "bind") == 0o700 and _mode(tree / "bind" / "inner") == 0o600
    assert _mode(tree / "own") == 0o660  # the rest is still normalised
    assert not report.complete and report.reason == "mount_point" and report.mount_points == 1


def test_unreadable_mount_information_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "worktree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub").chmod(0o700)
    _bind_mounted(monkeypatch, "sub", None)
    entry = normalise_owned_tree(tree)
    monkeypatch.setattr(ownership, "mount_id", lambda descriptor: None)
    root = normalise_owned_tree(tree)
    monkeypatch.undo()
    assert not entry.complete and entry.reason == "mount_info_unavailable"
    assert not root.complete and root.reason == "mount_info_unavailable" and root.scanned == 0
    assert _mode(tree / "sub") == 0o700


def test_repair_exits_non_zero_at_a_bind_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, worktree, _metadata = _platform(tmp_path)
    _bind_mounted(monkeypatch, "dist")
    code = ownership_repair.main(["--platform-root", str(root), "--manager", _me()])
    _bind_mounted(monkeypatch, IDENTITY)  # the ChangeSet directory itself a mount
    second = ownership_repair.main(["--platform-root", str(root), "--manager", _me()])
    monkeypatch.undo()
    out = capsys.readouterr().out
    assert code == 3 and second == 3
    assert f"incomplete worktrees/falcone/{IDENTITY}: mount_point" in out
    assert f"refused worktrees/falcone/{IDENTITY}: another filesystem or mount" in out
    assert _mode(worktree / "dist") == 0o700


@pytest.mark.asyncio
async def test_a_stalled_event_callback_does_not_hold_the_provider_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, worktree = _provider_handler(tmp_path, monkeypatch, str(uuid4()))
    handler._normalise_seconds = 0.05
    handler._normalise_grace_seconds = 0.05
    handler._normalise_publish_seconds = 5.0  # capped by the shared deadline
    stalled: list[str] = []

    async def stall(event: dict[str, Any]) -> None:
        stalled.append(event["status"])
        await asyncio.sleep(30)

    request = AgentRequest.model_validate(
        _agent_request(worktree, writable=True, role="implementer")
    )
    started = time.monotonic()
    await handler._normalise_worktree_ownership(request, stall)
    assert time.monotonic() - started < 1.0
    assert stalled == ["NORMALISED"]


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.asyncio
async def test_the_recorder_keeps_the_ownership_event_for_both_providers(
    provider: Literal["codex", "claude"],
) -> None:
    recorder = AgentTranscriptRecorder(
        Settings(),
        uuid4(),
        provider,
        client=object(),  # type: ignore[arg-type]
    )
    await recorder.event(
        {
            "type": "hermes.worktree_ownership",
            "status": "INCOMPLETE",
            "scanned": 12,
            "changed": 3,
            "failed": 0,
            "mount_points": 1,
            "reason": "mount_point",
        }
    )
    await recorder.event(
        {"type": "hermes.worktree_ownership", "status": "FAILED", "detail": "OSError: x"}
    )
    lines = [(chunk.stream, chunk.redacted_text) for chunk in recorder.buffer]
    assert lines == [
        (
            "system",
            "Worktree ownership normalisation: INCOMPLETE (scanned 12, changed 3, failed 0, "
            "mount_points 1); reason mount_point",
        ),
        ("system", "Worktree ownership normalisation: FAILED; OSError: x"),
    ]


@pytest.mark.asyncio
async def test_a_cancelled_flush_marks_the_transcript_incomplete() -> None:
    """Codex review of 5cc233e: a bounded publication can cancel a flush after the batch
    left the buffer; the transcript must then report itself incomplete."""

    class StalledClient:
        async def logs(self, *_args: Any, **_kwargs: Any) -> bool:
            await asyncio.sleep(30)
            return True

    recorder = AgentTranscriptRecorder(
        Settings(),
        uuid4(),
        "codex",
        client=StalledClient(),  # type: ignore[arg-type]
    )
    await recorder.event({"type": "hermes.worktree_ownership", "status": "NORMALISED"})
    assert recorder.buffer and recorder.transcript_incomplete is False
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(recorder.flush(), timeout=0.05)
    assert recorder.buffer == []
    assert recorder.transcript_incomplete is True
