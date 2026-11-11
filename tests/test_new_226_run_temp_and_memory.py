"""NEW-226: per-run TMPDIR, the private /tmp sweep, run memory bounds and memory events.

On 2026-10-19 files finished runs left in the agent workers' PrivateTmp tmpfs held 2.6 GB
of RAM, memory pressure "full" sat at 75-90%, and two makers each burned two 2-hour
attempts while reporting only a timeout.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from engineering_control.adapters import process as process_module
from engineering_control.adapters import run_temp
from engineering_control.adapters.claude import client as claude_module
from engineering_control.adapters.claude.client import ClaudeCLIAdapter
from engineering_control.adapters.codex import client as codex_module
from engineering_control.adapters.codex.client import CodexCLIAdapter
from engineering_control.adapters.command import CommandResult
from engineering_control.adapters.process import ProcessResult, run_memory_limit
from engineering_control.adapters.scanning.tooling import DeterministicScanner
from engineering_control.config import Settings
from engineering_control.models.core import AgentRequest
from engineering_control.models.enums import ModelClass, Project, Provider, RunStatus
from engineering_control.observability import cgroup_memory
from engineering_control.observability.agent import TranscriptNormalizer
from engineering_control.workflows import tmp_sweep
from engineering_control.workflows.role_worker import worker_slots
from engineering_control.workflows.worker_registry import role_worker_spec

GIB = 1 << 30


def _request(provider: Provider, worktree: Path) -> AgentRequest:
    return AgentRequest(
        project=Project.FALCONE,
        role="implementer",
        provider=provider,
        model_class=ModelClass.WORKER,
        prompt="perform a bounded synthetic task",
        working_directory=str(worktree),
        output_schema={"type": "object"},
        skip_git_repository_check=True,
    )


# -- 1. per-run TMPDIR -------------------------------------------------------------------


def test_a_run_directory_is_private_and_removed(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    with (
        run_temp.run_temporary_directory(root) as first,
        run_temp.run_temporary_directory(root) as second,
    ):
        parent = root / run_temp.RUN_TEMP_PARENT
        assert first.parent == parent == second.parent
        assert first != second
        assert stat.S_IMODE(first.stat().st_mode) == 0o700
        # The runner may enter the parent but not list it: no run finds another's name.
        assert stat.S_IMODE(parent.stat().st_mode) == 0o300
        if os.geteuid() != 0:
            with pytest.raises(PermissionError):
                os.listdir(parent)
        assert len(first.name) == 16
        assert run_temp.live_run_temporaries() >= {first, second}
        (first / "vitest-output").mkdir()
        (first / "vitest-output" / "log").write_text("x")
    run_temp.wait_for_removals()
    assert not first.exists()
    assert not second.exists()
    assert first not in run_temp.live_run_temporaries()


def test_a_run_directory_is_removed_when_the_run_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), run_temp.run_temporary_directory(tmp_path) as path:
        (path / "partial").write_text("x")
        raise RuntimeError("provider failed")
    run_temp.wait_for_removals()
    assert not path.exists()


@pytest.mark.asyncio
async def test_a_run_directory_is_removed_when_the_activity_is_cancelled(
    tmp_path: Path,
) -> None:
    created: list[Path] = []
    started = asyncio.Event()

    async def run() -> None:
        with run_temp.run_temporary_directory(tmp_path) as path:
            created.append(path)
            started.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run_temp.wait_for_removals()
    assert not created[0].exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_read_only_and_unreadable_directories_are_still_removed(tmp_path: Path) -> None:
    path = tmp_path / "run"
    (path / "go-mod" / "pkg").mkdir(parents=True)
    (path / "go-mod" / "pkg" / "file.go").write_text("x")
    (path / "sealed").mkdir()
    (path / "sealed" / "inner").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (path / "link").symlink_to(outside, target_is_directory=True)
    os.chmod(path / "go-mod" / "pkg", 0o500)
    os.chmod(path / "go-mod", 0o500)
    os.chmod(path / "sealed", 0o000)
    os.chmod(outside, 0o500)
    assert run_temp.remove_tree(path)
    assert not path.exists()
    # A symlink is removed, never followed: neither its target nor the target's mode.
    assert (outside / "keep").exists()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o500
    os.chmod(outside, 0o700)


def test_the_worker_start_clears_left_run_directories(tmp_path: Path) -> None:
    with run_temp.run_temporary_directory(tmp_path) as live:
        parent = live.parent
        os.chmod(parent, 0o700)
        (parent / "left-by-a-killed-worker").mkdir()
        (parent / "left-by-a-killed-worker" / "f").write_text("x")
        os.chmod(parent, 0o300)
        assert run_temp.clear_run_temporaries(tmp_path) == 1
        assert live.exists()  # a live run directory is never touched
        assert stat.S_IMODE(parent.stat().st_mode) == 0o300
    assert run_temp.clear_run_temporaries(tmp_path / "absent") == 0


def test_a_failed_removal_is_retried_by_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[Path] = []
    real = run_temp.remove_tree

    def first_fails(path: Path) -> bool:
        attempts.append(path)
        return False if len(attempts) == 1 else real(path)

    monkeypatch.setattr(run_temp, "remove_tree", first_fails)
    with run_temp.run_temporary_directory(tmp_path) as path:
        pass
    run_temp.wait_for_removals()
    assert path.exists()
    assert run_temp.retry_unremoved() == 1
    assert not path.exists()
    assert run_temp.retry_unremoved() == 0


def test_the_run_environment_sets_every_temp_variable(tmp_path: Path) -> None:
    assert run_temp.run_temp_environment(tmp_path) == {
        "TMPDIR": str(tmp_path),
        "TMP": str(tmp_path),
        "TEMP": str(tmp_path),
    }


@pytest.mark.asyncio
async def test_codex_runs_get_their_own_tmpdir_and_memory_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    seen: dict[str, Any] = {}

    async def fake_run(command: list[str], **kwargs: Any) -> ProcessResult:
        seen.update(kwargs)
        directory = Path(kwargs["environment"]["TMPDIR"])
        seen["existed"] = directory.is_dir()  # noqa: ASYNC240 - synchronous test double
        Path(command[command.index("--output-last-message") + 1]).write_text("{}")  # noqa: ASYNC240
        return ProcessResult(exit_code=0, events=(), stderr="")

    monkeypatch.setattr(codex_module, "run_jsonl_process", fake_run)
    adapter = CodexCLIAdapter(temp_root=str(tmp_path / "codex"), run_memory_max_bytes=4 * GIB)
    result = await adapter.execute(_request(Provider.CODEX, worktree))

    assert result.status is RunStatus.SUCCESS
    directory = Path(seen["environment"]["TMPDIR"])
    assert seen["existed"] is True
    assert directory.parent == tmp_path / "codex" / run_temp.RUN_TEMP_PARENT
    assert seen["environment"]["TMP"] == seen["environment"]["TEMP"] == str(directory)
    assert seen["run_memory_max_bytes"] == 4 * GIB
    run_temp.wait_for_removals()
    assert not directory.exists()  # noqa: ASYNC240 - synchronous test assertion


@pytest.mark.asyncio
async def test_claude_runs_get_their_own_tmpdir_removed_even_on_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    seen: list[Path] = []

    async def cancelled_run(command: list[str], **kwargs: Any) -> ProcessResult:
        seen.append(Path(kwargs["environment"]["TMPDIR"]))
        raise asyncio.CancelledError

    monkeypatch.setattr(claude_module, "run_jsonl_process", cancelled_run)
    adapter = ClaudeCLIAdapter(temp_root=str(tmp_path / "claude"))
    with pytest.raises(asyncio.CancelledError):
        await adapter.execute(_request(Provider.CLAUDE, worktree))
    run_temp.wait_for_removals()
    assert seen and not seen[0].exists()


@pytest.mark.asyncio
async def test_a_bare_adapter_keeps_the_previous_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_run(command: list[str], **kwargs: Any) -> ProcessResult:
        seen.update(kwargs)
        return ProcessResult(exit_code=0, events=(), stderr="")

    monkeypatch.setattr(claude_module, "run_jsonl_process", fake_run)
    await ClaudeCLIAdapter().execute(_request(Provider.CLAUDE, tmp_path))
    assert seen["environment"] is None
    assert seen["run_memory_max_bytes"] is None


def test_the_provider_workers_build_adapters_with_a_temp_root(tmp_path: Path) -> None:
    from engineering_control.activities.provider import ProviderActivityHandler

    settings = Settings(temporary_root=tmp_path, agent_run_memory_max_mib=4096)
    for kind in ("codex", "claude"):
        adapter = ProviderActivityHandler(settings, kind).adapter
        assert adapter.temp_root == str(tmp_path / kind)  # type: ignore[attr-defined]
        assert adapter.run_memory_max_bytes == 4 * GIB  # type: ignore[attr-defined]


# -- 2. the private /tmp sweep -----------------------------------------------------------


def _tree(root: Path) -> None:
    (root / "vitest-1234" / "deep").mkdir(parents=True)
    (root / "vitest-1234" / "deep" / "out.log").write_text("x")
    (root / "trivy").mkdir()
    (root / "node-compile-cache").mkdir()
    (root / ".git").mkdir()
    (root / "loose.txt").write_text("x")


def test_the_sweep_selects_only_idle_entries(tmp_path: Path) -> None:
    _tree(tmp_path)
    now = time.time()
    hour_later = now + 3600
    assert tmp_sweep.stale_entries(tmp_path, now=hour_later, idle_seconds=5400) == []
    selected = tmp_sweep.stale_entries(tmp_path, now=now + 5500, idle_seconds=5400)
    # node-compile-cache and Codex's mount targets always stay; the order is by name.
    assert selected == [tmp_path / "loose.txt", tmp_path / "trivy", tmp_path / "vitest-1234"]


def test_an_old_mtime_does_not_make_an_entry_idle(tmp_path: Path) -> None:
    # pnpm and tar restore 1985 mtimes; the change time is the time of the restore.
    (tmp_path / "extracted").mkdir()
    (tmp_path / "extracted" / "package.json").write_text("{}")
    os.utime(tmp_path / "extracted" / "package.json", (500_000_000, 500_000_000))
    os.utime(tmp_path / "extracted", (500_000_000, 500_000_000))
    assert tmp_sweep.stale_entries(tmp_path, now=time.time() + 60, idle_seconds=5400) == []


def test_the_sweep_keeps_what_a_running_step_wrote(tmp_path: Path) -> None:
    before = time.time() - 1
    _tree(tmp_path)
    later = time.time() + 7200
    # A step that started before the entries were written still runs: all are kept.
    assert (
        tmp_sweep.stale_entries(tmp_path, now=later, idle_seconds=5400, running_since=before) == []
    )
    # A step that started after them did not write them.
    after = time.time() + 1
    assert (
        len(tmp_sweep.stale_entries(tmp_path, now=later, idle_seconds=5400, running_since=after))
        == 3
    )


def test_the_sweep_never_touches_a_live_run_directory(tmp_path: Path) -> None:
    _tree(tmp_path)
    later = time.time() + 7200
    live = tmp_path / "vitest-1234" / "deep"
    selected = tmp_sweep.stale_entries(
        tmp_path, now=later, idle_seconds=5400, protected={live, tmp_path / "trivy"}
    )
    assert selected == [tmp_path / "loose.txt"]


def test_an_entry_too_large_to_walk_is_kept(tmp_path: Path) -> None:
    _tree(tmp_path)
    selected = tmp_sweep.stale_entries(
        tmp_path, now=time.time() + 7200, idle_seconds=5400, max_entries=2
    )
    assert tmp_path / "vitest-1234" not in selected
    assert tmp_path / "loose.txt" in selected


def test_the_sweep_removes_what_it_selects(tmp_path: Path) -> None:
    _tree(tmp_path)
    # A negative idle time stands for "much later": every entry is older than the cutoff.
    result = tmp_sweep.sweep(tmp_path, idle_seconds=-60, protected=())
    assert result == tmp_sweep.SweepResult(removed=3, failed=0)
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [".git", "node-compile-cache"]


def test_the_sweep_removes_nothing_while_a_step_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path)
    with tmp_sweep.running_step():
        assert tmp_sweep.sweep(tmp_path, idle_seconds=-60, protected=()).removed == 0
    # A step that starts during the pass stops it before the next removal.
    real = tmp_sweep.remove_tree

    def start_a_step(path: Path) -> bool:
        tmp_sweep._STARTED[0] += 1  # noqa: SLF001
        return real(path)

    monkeypatch.setattr(tmp_sweep, "remove_tree", start_a_step)
    assert tmp_sweep.sweep(tmp_path, idle_seconds=-60, protected=()).removed == 1
    assert (tmp_path / "vitest-1234" / "deep" / "out.log").exists()
    # A step that starts during an entry's revalidation walk stops it too.
    monkeypatch.setattr(tmp_sweep, "remove_tree", real)
    walk = tmp_sweep._newest_change  # noqa: SLF001

    def start_during_walk(*args: Any) -> float | None:
        tmp_sweep._STARTED[0] += 1  # noqa: SLF001
        return walk(*args)

    selected = tmp_sweep.stale_entries(tmp_path, now=time.time() + 60, idle_seconds=-60)
    assert selected
    monkeypatch.setattr(tmp_sweep, "stale_entries", lambda *_, **__: selected)
    monkeypatch.setattr(tmp_sweep, "_newest_change", start_during_walk)
    assert tmp_sweep.sweep(tmp_path, idle_seconds=-60, protected=()).removed == 0
    assert all(entry.exists() for entry in selected)


def test_a_busy_worker_removes_only_what_finished_steps_wrote(tmp_path: Path) -> None:
    root = tmp_path / "tmp"
    (root / "rv121" / "target").mkdir(parents=True)
    (root / "rv121" / "target" / "out").write_text("x")
    (root / "loose.txt").write_text("x")
    time.sleep(0.05)
    with tmp_sweep.running_step():
        time.sleep(0.05)
        (root / "written-by-a-running-step").mkdir()
        # Without a busy idle time the pass still removes nothing while a step runs.
        assert tmp_sweep.sweep(root, idle_seconds=-60, protected=()).removed == 0
        # Busy: idle for the busy time is not enough, nobody may have written it since
        # the oldest running step started (a negative idle time stands for "much later").
        result = tmp_sweep.sweep(root, idle_seconds=5400, protected=(), busy_idle_seconds=-60)
        assert result == tmp_sweep.SweepResult(removed=1, failed=0)
        # A file waits for an idle worker: its old mtime could be a restored one.
        assert sorted(entry.name for entry in root.iterdir()) == [
            "loose.txt",
            "written-by-a-running-step",
        ]
    # And the busy idle time still applies to what finished steps wrote.
    (root / "fresh").mkdir()
    time.sleep(0.05)
    with tmp_sweep.running_step():
        kept = tmp_sweep.sweep(root, idle_seconds=5400, protected=(), busy_idle_seconds=600)
    assert kept.removed == 0
    assert (root / "fresh").exists()


def test_a_step_starting_during_a_busy_pass_stops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tmp"
    root.mkdir()
    for name in ("a", "b", "c"):
        (root / name).mkdir()
    time.sleep(0.05)
    real = tmp_sweep.remove_tree

    def start_a_step(path: Path) -> bool:
        tmp_sweep._STARTED[0] += 1  # noqa: SLF001
        return real(path)

    monkeypatch.setattr(tmp_sweep, "remove_tree", start_a_step)
    with tmp_sweep.running_step():
        result = tmp_sweep.sweep(root, idle_seconds=5400, protected=(), busy_idle_seconds=-60)
    assert result.removed == 1
    assert len(list(root.iterdir())) == 2


def test_a_write_during_the_busy_revalidation_puts_the_entry_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tmp"
    (root / "reused" / "target").mkdir(parents=True)
    (root / "reused" / "target" / "a").write_text("old")
    (root / "reused" / "target" / "b").write_text("old")
    time.sleep(0.05)
    walk = tmp_sweep._newest_change  # noqa: SLF001
    calls: list[Path] = []

    def write_after_the_walk(path: Path, *args: Any, **kwargs: Any) -> float | None:
        newest = walk(path, *args, **kwargs)
        calls.append(path)
        if len(calls) == 2:  # the pre-rename revalidation: a live step writes "a" after it
            (path / "target" / "a").write_text("written by a live step")
        return newest

    monkeypatch.setattr(tmp_sweep, "_newest_change", write_after_the_walk)
    with tmp_sweep.running_step():
        time.sleep(0.05)
        result = tmp_sweep.sweep(root, idle_seconds=5400, protected=(), busy_idle_seconds=-60)
    assert result.removed == 0
    assert (root / "reused" / "target" / "a").read_text() == "written by a live step"
    assert sorted(entry.name for entry in root.iterdir()) == ["reused"]
    # Once that step has ended and nothing writes in it, it is removed, renamed out of
    # reach first: no quarantine name is left behind.
    monkeypatch.setattr(tmp_sweep, "_newest_change", walk)
    time.sleep(0.05)
    with tmp_sweep.running_step():
        result = tmp_sweep.sweep(root, idle_seconds=5400, protected=(), busy_idle_seconds=-60)
    assert result == tmp_sweep.SweepResult(removed=1, failed=0)
    assert list(root.iterdir()) == []


def test_a_put_back_entry_never_replaces_a_new_one_of_that_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "tmp"
    (root / "cache").mkdir(parents=True)
    (root / "cache" / "old").write_text("old")
    time.sleep(0.05)
    walk = tmp_sweep._newest_change  # noqa: SLF001

    def recreate_during_the_final_walk(path: Path, *args: Any, **kwargs: Any) -> float | None:
        if kwargs.get("renamed"):
            (path / "old").write_text("written before the rename")  # forces a put-back
            (root / "cache").mkdir()  # a live step makes a new "cache" meanwhile
            (root / "cache" / "new").write_text("new")
        return walk(path, *args, **kwargs)

    monkeypatch.setattr(tmp_sweep, "_newest_change", recreate_during_the_final_walk)
    # A migration test earlier in the same process may have run alembic's fileConfig, which
    # disables every logger that already existed; the warning asserted below needs this one.
    monkeypatch.setattr(tmp_sweep.LOGGER, "disabled", False)
    with tmp_sweep.running_step():
        time.sleep(0.05)
        result = tmp_sweep.sweep(root, idle_seconds=5400, protected=(), busy_idle_seconds=-60)
    assert result.removed == 0
    assert (root / "cache" / "new").read_text() == "new"  # not replaced
    kept = [entry for entry in root.iterdir() if entry.name.startswith(".hermes-sweep-")]
    assert len(kept) == 1
    assert (kept[0] / "old").read_text() == "written before the rename"  # not lost
    assert "private /tmp sweep kept cache as .hermes-sweep-" in caplog.text


def test_an_entry_replaced_before_the_rename_goes_back_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tmp"
    (root / "out").mkdir(parents=True)
    time.sleep(0.05)
    real = tmp_sweep._rename_noreplace  # noqa: SLF001
    old = time.time() - 86400

    def replace_then_rename(source: Path, target: Path) -> bool:
        if source.name == "out" and source.is_dir():
            # A live step removes the old empty directory and copies a file with an old
            # mtime to that name (cp -p) just before the sweep renames it.
            source.rmdir()
            source.write_text("new content")
            os.utime(source, (old, old))
        return real(source, target)

    monkeypatch.setattr(tmp_sweep, "_rename_noreplace", replace_then_rename)
    with tmp_sweep.running_step():
        time.sleep(0.05)
        result = tmp_sweep.sweep(root, idle_seconds=5400, protected=(), busy_idle_seconds=-60)
    assert result.removed == 0
    assert (root / "out").read_text() == "new content"
    assert [entry.name for entry in root.iterdir()] == ["out"]


def test_rename_noreplace_refuses_an_existing_target(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("a")
    (tmp_path / "b").write_text("b")
    assert tmp_sweep._rename_noreplace(tmp_path / "a", tmp_path / "b") is False  # noqa: SLF001
    assert (tmp_path / "b").read_text() == "b"
    assert tmp_sweep._rename_noreplace(tmp_path / "a", tmp_path / "c") is True  # noqa: SLF001
    assert (tmp_path / "c").read_text() == "a"


def test_a_busy_pass_keeps_live_run_directories(tmp_path: Path) -> None:
    root = tmp_path / "tmp"
    (root / "keep" / "run").mkdir(parents=True)
    time.sleep(0.05)
    with tmp_sweep.running_step():
        result = tmp_sweep.sweep(
            root, idle_seconds=5400, protected={root / "keep" / "run"}, busy_idle_seconds=-60
        )
    assert result.removed == 0
    assert (root / "keep" / "run").exists()


def test_the_role_worker_sweeps_busy_with_the_configured_idle_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engineering_control.workflows import role_worker

    seen: dict[str, Any] = {}
    monkeypatch.setattr(tmp_sweep, "is_private_tmp", lambda: True)
    monkeypatch.setattr(role_worker, "retry_unremoved", lambda: None)

    def fake_sweep(root: Path, **kwargs: Any) -> tmp_sweep.SweepResult:
        seen.update(kwargs, root=root)
        return tmp_sweep.SweepResult(0, 0)

    monkeypatch.setattr(tmp_sweep, "sweep", fake_sweep)
    role_worker.sweep_temporaries(
        Settings(private_tmp_sweep_busy_idle_minutes=12), protected=frozenset()
    )
    assert seen["busy_idle_seconds"] == 720
    assert seen["idle_seconds"] == 5400
    assert Settings().private_tmp_sweep_interval_seconds == 300


def test_running_steps_are_tracked_until_they_end() -> None:
    assert tmp_sweep.oldest_running_step() is None
    with tmp_sweep.running_step():
        first = tmp_sweep.oldest_running_step()
        with tmp_sweep.running_step():
            assert tmp_sweep.oldest_running_step() == first
    assert tmp_sweep.oldest_running_step() is None


def test_the_sweep_runs_only_on_a_systemd_private_tmp(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    shared = "601 516 0:34 / /tmp rw,nosuid,nodev shared:380 - tmpfs tmpfs rw\n"
    private = (
        "650 601 0:34 /systemd-private-352a7e4e7a3d44ccaa43868322162d8d-"
        "engineering-worker@codex.service-z34gpC/tmp /tmp rw,nosuid,nodev - tmpfs tmpfs rw\n"
    )
    mountinfo.write_text(shared)
    assert tmp_sweep.is_private_tmp(mountinfo) is False
    mountinfo.write_text(shared + private)
    assert tmp_sweep.is_private_tmp(mountinfo) is True
    assert tmp_sweep.is_private_tmp(tmp_path / "absent") is False


# -- 3. the scan worker's Trivy cache ----------------------------------------------------


class _TrivyRunner:
    def __init__(self) -> None:
        self.arguments: list[str] = []

    def run(self, executable: str, arguments: list[str], **_: Any) -> CommandResult:
        self.arguments = list(arguments)
        Path(arguments[arguments.index("--output") + 1]).write_text('{"Results": []}')
        return CommandResult(0, "", "")


def test_trivy_keeps_its_cache_on_disk(tmp_path: Path) -> None:
    runner = _TrivyRunner()
    cache = tmp_path / "cache" / "scan" / "trivy"
    DeterministicScanner(runner, trivy_cache_root=cache).vulnerability_scan(  # type: ignore[arg-type]
        tmp_path, tmp_path / "evidence"
    )
    position = runner.arguments.index("--cache-dir")
    assert runner.arguments[position + 1] == str(cache)
    assert runner.arguments[runner.arguments.index("--cache-backend") + 1] == "memory"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert Settings().trivy_cache_root == Path("/srv/engineering/cache/scan/trivy")


# -- 4. worker slots ---------------------------------------------------------------------


def test_provider_worker_slots_are_set_per_deployment() -> None:
    default = Settings()
    assert worker_slots(default, role_worker_spec("codex")) == 2
    assert worker_slots(default, role_worker_spec("claude")) == 2
    assert worker_slots(default, role_worker_spec("scan")) == 4
    one = Settings(codex_worker_slots=1)
    assert worker_slots(one, role_worker_spec("codex")) == 1
    assert worker_slots(one, role_worker_spec("claude")) == 2
    with pytest.raises(ValidationError):
        Settings(codex_worker_slots=3)  # the drop-ins are sized for 2


# -- the run cgroup's memory bound -------------------------------------------------------


def _worker(tmp_path: Path, high: str, maximum: str, control: str = "") -> Path:
    worker = tmp_path / "worker.service"
    (worker / "agent-run-x").mkdir(parents=True)
    (worker / "memory.high").write_text(f"{high}\n")
    (worker / "memory.max").write_text(f"{maximum}\n")
    (worker / "cgroup.subtree_control").write_text(control)
    return worker


def test_a_run_may_use_the_worker_memory_high_less_a_reserve(tmp_path: Path) -> None:
    worker = _worker(tmp_path, str(7 * GIB), str(8 * GIB))
    assert run_memory_limit(worker, None) == 6 * GIB
    (worker / "memory.high").write_text("max\n")
    assert run_memory_limit(worker, None) == 7 * GIB
    (worker / "memory.max").write_text("max\n")
    assert run_memory_limit(worker, None) is None
    assert run_memory_limit(worker, 4 * GIB) == 4 * GIB


def test_the_run_cgroup_gets_the_memory_controller_and_its_limit(tmp_path: Path) -> None:
    worker = _worker(tmp_path, str(7 * GIB), str(8 * GIB))
    run = process_module._RunCgroup(worker / "agent-run-x")  # noqa: SLF001
    run.limit_memory(None)
    assert (worker / "cgroup.subtree_control").read_text() == "+memory"
    assert (worker / "agent-run-x" / "memory.max").read_text() == str(6 * GIB)
    # Already enabled: nothing is written again.
    (worker / "cgroup.subtree_control").write_text("memory pids")
    run.limit_memory(3 * GIB)
    assert (worker / "cgroup.subtree_control").read_text() == "memory pids"
    assert (worker / "agent-run-x" / "memory.max").read_text() == str(3 * GIB)


def test_a_limit_that_cannot_be_set_leaves_the_run_as_before(tmp_path: Path) -> None:
    worker = _worker(tmp_path, str(7 * GIB), str(8 * GIB))
    run = process_module._RunCgroup(worker / "agent-run-missing")  # noqa: SLF001
    run.limit_memory(None)  # no such run cgroup: logged, never raised


# -- 5. memory pressure in the step's events ---------------------------------------------


def _cgroup(tmp_path: Path, full_total: int, *, full_avg60: float, oom: int) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory.pressure").write_text(
        f"some avg10=99.00 avg60=97.07 avg300=97.31 total={full_total + 10}\n"
        f"full avg10=97.78 avg60={full_avg60:.2f} avg300=95.86 total={full_total}\n"
    )
    (tmp_path / "memory.current").write_text(f"{7 * GIB}\n")
    (tmp_path / "memory.high").write_text(f"{7 * GIB}\n")
    (tmp_path / "memory.max").write_text("max\n")
    (tmp_path / "memory.events").write_text(f"low 0\nhigh 9\nmax 0\noom 0\noom_kill {oom}\n")
    return tmp_path


def test_the_worker_cgroup_is_the_parent_of_the_supervisor(tmp_path: Path) -> None:
    proc = tmp_path / "cgroup"
    proc.write_text("0::/supervisor\n")
    assert cgroup_memory.worker_cgroup(proc, tmp_path / "fs") == tmp_path / "fs"
    proc.write_text("0::/system.slice/x.service\n")
    assert cgroup_memory.worker_cgroup(proc, tmp_path / "fs") == (
        tmp_path / "fs" / "system.slice" / "x.service"
    )


def test_a_step_stalled_on_memory_reads_memory_throttled(tmp_path: Path) -> None:
    group = _cgroup(tmp_path / "a", 1_000_000, full_avg60=1.0, oom=2)
    start = cgroup_memory.memory_snapshot(group)
    assert start is not None
    _cgroup(group, 1_000_000 + 80_000_000, full_avg60=1.0, oom=3)
    end = cgroup_memory.memory_snapshot(group)
    assert end is not None
    end = cgroup_memory.MemorySnapshot(**{**end.__dict__, "taken_at": start.taken_at + 100})
    event = cgroup_memory.memory_pressure_event(start, end)
    assert event is not None
    assert event["type"] == "hermes.memory_pressure"
    assert event["assessment"] == "memory-throttled"
    assert event["full_stall_seconds"] == 80.0
    assert event["full_stall_share"] == 0.8
    assert event["oom_kills"] == 1
    assert event["memory_current_bytes"] == 7 * GIB
    assert event["memory_max_bytes"] is None
    assert event["full_avg10"] == 97.78
    assert "memory-throttled" in event["summary"]
    assert "80% of the step" in event["summary"]
    lines = TranscriptNormalizer().normalize("codex", event)
    assert [(line.stream, line.text) for line in lines] == [("system", event["summary"])]


def test_a_step_without_memory_stalls_reads_memory_ok(tmp_path: Path) -> None:
    group = _cgroup(tmp_path, 0, full_avg60=0.0, oom=0)
    snapshot = cgroup_memory.memory_snapshot(group)
    event = cgroup_memory.memory_pressure_event(snapshot, snapshot)
    assert event is not None and event["assessment"] == "memory-ok"
    # Without a start reading, the last minute's average decides.
    hot = _cgroup(tmp_path / "hot", 0, full_avg60=42.0, oom=0)
    assert (
        cgroup_memory.memory_pressure_event(None, cgroup_memory.memory_snapshot(hot))["assessment"]
        == "memory-throttled"
    )
    assert cgroup_memory.memory_snapshot(tmp_path / "absent") is None
    assert cgroup_memory.memory_pressure_event(snapshot, None) is None


class _StatusAdapter:
    provider = Provider.CODEX

    def __init__(self, status: RunStatus) -> None:
        self.status = status

    async def authenticated(self) -> bool:
        return True

    async def execute(self, request: AgentRequest, on_event: Any = None) -> Any:
        from datetime import UTC, datetime

        from engineering_control.models.core import AgentResult

        now = datetime.now(UTC)
        return AgentResult(
            run_id=request.run_id,
            provider=request.provider,
            role=request.role,
            status=self.status,
            summary="bounded result",
            structured_result={},
            started_at=now,
            finished_at=now,
        )


class _PlainPack:
    def enrich(self, request: AgentRequest, _step: str) -> tuple[AgentRequest, Any]:
        from engineering_control.agents.instructions import InstructionBundle

        return request, InstructionBundle(
            prompt_prefix="", sha256="a" * 64, revision=None, sources=(), allowed_tools=()
        )


@pytest.mark.asyncio
async def test_a_failed_provider_step_records_the_worker_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engineering_control.activities import provider as provider_module

    heartbeats: list[dict[str, Any]] = []
    monkeypatch.setattr(provider_module.activity, "heartbeat", heartbeats.append)
    snapshot = cgroup_memory.memory_snapshot(_cgroup(tmp_path / "cg", 0, full_avg60=50.0, oom=0))
    monkeypatch.setattr(provider_module, "memory_snapshot", lambda: snapshot)
    worktree_root = tmp_path / "worktrees"
    worktree = worktree_root / "falcone" / "change-set"
    worktree.mkdir(parents=True)
    settings = Settings(worktree_root=worktree_root, run_root=tmp_path / "runs")
    request = {
        **_request(Provider.CODEX, worktree).model_dump(mode="json"),
        "skip_git_repository_check": False,
        "writable": True,
    }
    for status, recorded in ((RunStatus.TIMED_OUT, True), (RunStatus.SUCCESS, False)):
        heartbeats.clear()
        handler = provider_module.ProviderActivityHandler(
            settings, "codex", _StatusAdapter(status), _PlainPack()
        )
        await handler.handle("delivery.invoke_maker", {"maker_request": request})
        types = [beat.get("event_type") for beat in heartbeats]
        assert ("hermes.memory_pressure" in types) is recorded, status
