"""NEW-188: processes an agent run started are stopped when the run ends.

The agent CLI runs commands in their own sessions, so killing its process group missed
them. On 2026-10-12 a maker's hung `vitest run` processes outlived the maker's 2-hour
timeout, held the output pipes and stalled the codex worker until it was restarted.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from engineering_control.adapters import process as process_module
from engineering_control.adapters.process import run_jsonl_process

# Prints the pid of a process that leaves the agent's session, as a JSONL event.
_ESCAPE = 'setsid sleep 300 & echo "{\\"type\\":\\"escaped\\",\\"pid\\":$!}"'


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat:
            state = stat.read().rsplit(b")", 1)[1].split()[0]
    except OSError:
        return False
    return state != b"Z"


def _escaped_pid(events: tuple[dict[str, object], ...]) -> int:
    pids = [int(str(event["pid"])) for event in events if event.get("type") == "escaped"]
    assert len(pids) == 1, events
    return pids[0]


@pytest.fixture(autouse=True)
def _fast_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_module, "_TRACK_INTERVAL_SECONDS", 0.1)


@pytest.mark.asyncio
async def test_an_escaped_process_is_stopped_when_the_agent_exits() -> None:
    started = time.monotonic()
    result = await run_jsonl_process(
        ["bash", "-c", _ESCAPE + "; sleep 0.5; exit 0"],
        prompt="",
        cwd=os.getcwd(),
        timeout_seconds=60,
        provider="synthetic",
    )
    pid = _escaped_pid(result.events)
    assert result.exit_code == 0
    assert not result.timed_out
    # It held stdout and stderr; before NEW-188 the readers waited for it.
    assert time.monotonic() - started < 20
    assert not _alive(pid)


@pytest.mark.asyncio
async def test_an_escaped_process_is_stopped_when_the_agent_times_out() -> None:
    started = time.monotonic()
    result = await run_jsonl_process(
        ["bash", "-c", _ESCAPE + "; sleep 300"],
        prompt="",
        cwd=os.getcwd(),
        timeout_seconds=2,
        provider="synthetic",
    )
    pid = _escaped_pid(result.events)
    assert result.timed_out
    assert time.monotonic() - started < 30
    assert not _alive(pid)


@pytest.mark.asyncio
async def test_an_escaped_process_is_stopped_when_the_activity_is_cancelled() -> None:
    seen: list[int] = []

    async def on_event(event: dict[str, object]) -> None:
        if event.get("type") == "escaped":
            seen.append(int(str(event["pid"])))

    task = asyncio.create_task(
        run_jsonl_process(
            ["bash", "-c", _ESCAPE + "; sleep 300"],
            prompt="",
            cwd=os.getcwd(),
            timeout_seconds=300,
            provider="synthetic",
            on_event=on_event,
        )
    )
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.05)
    assert seen
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=30)
    assert not _alive(seen[0])


@pytest.mark.asyncio
async def test_an_orphan_recorded_while_still_linked_is_stopped() -> None:
    # The intermediate shell exits after a scan has seen the sleeper, which is then
    # reparented away from the agent; its recorded identity still identifies it.
    script = (
        'sh -c \'setsid sleep 300 </dev/null >/dev/null 2>&1 & echo "{\\"type\\":\\"escaped\\",'
        '\\"pid\\":$!}"; sleep 1\'; sleep 0.5; exit 0'
    )
    result = await run_jsonl_process(
        ["bash", "-c", script],
        prompt="",
        cwd=os.getcwd(),
        timeout_seconds=60,
        provider="synthetic",
    )
    pid = _escaped_pid(result.events)
    assert not _alive(pid)


@pytest.mark.asyncio
async def test_a_term_resistant_process_is_killed_even_if_cleanup_is_cancelled() -> None:
    seen: list[int] = []

    async def on_event(event: dict[str, object]) -> None:
        if event.get("type") == "escaped":
            seen.append(int(str(event["pid"])))

    stubborn = (
        "setsid bash -c 'trap \"\" TERM; while :; do sleep 1; done' & "
        'echo "{\\"type\\":\\"escaped\\",\\"pid\\":$!}"; sleep 300'
    )
    task = asyncio.create_task(
        run_jsonl_process(
            ["bash", "-c", stubborn],
            prompt="",
            cwd=os.getcwd(),
            timeout_seconds=300,
            provider="synthetic",
            on_event=on_event,
        )
    )
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.05)
    assert seen
    await asyncio.sleep(0.5)  # let a scan record it
    task.cancel()
    await asyncio.sleep(0.3)  # cleanup is now inside the SIGTERM grace period
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=30)
    assert not _alive(seen[0])


@pytest.mark.asyncio
async def test_output_that_does_not_finish_is_never_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_module, "_READER_DRAIN_SECONDS", 0.5)

    async def slow(event: dict[str, object]) -> None:
        await asyncio.sleep(2)

    with pytest.raises(process_module.ProviderOutputIncompleteError):
        await run_jsonl_process(
            ["bash", "-c", 'for i in 1 2 3; do echo "{\\"n\\":$i}"; done'],
            prompt="",
            cwd=os.getcwd(),
            timeout_seconds=60,
            provider="synthetic",
            on_event=slow,
        )


@pytest.mark.asyncio
async def test_unrelated_processes_survive() -> None:
    bystander = subprocess.Popen(  # noqa: ASYNC220 - a plain child of the test
        ["sleep", "300"],  # noqa: S607
        start_new_session=True,
    )
    try:
        await run_jsonl_process(
            ["bash", "-c", _ESCAPE + "; sleep 0.5; exit 0"],
            prompt="",
            cwd=os.getcwd(),
            timeout_seconds=60,
            provider="synthetic",
        )
        assert _alive(bystander.pid)
    finally:
        bystander.kill()
        bystander.wait()


def _dead_pid() -> int:
    """A pid with no process (or a different one) behind it: a safe stand-in root."""
    probe = subprocess.Popen(["true"])  # noqa: S607
    probe.wait()
    return probe.pid


def test_init_and_this_worker_cannot_be_the_root() -> None:
    for pid in (0, 1, os.getpid()):
        with pytest.raises(ValueError):
            process_module._AgentProcesses(root_pid=pid)  # noqa: SLF001


def test_this_worker_and_its_ancestors_are_never_signalled() -> None:
    tracked = process_module._AgentProcesses(root_pid=_dead_pid())  # noqa: SLF001
    table = process_module._process_table()  # noqa: SLF001
    own = os.getpid()
    parent = table[own][0]
    tracked.seen[own] = table[own][1]
    tracked.seen[parent] = table[parent][1]
    alive = tracked._alive()  # noqa: SLF001
    assert own not in alive
    assert parent not in alive


def test_a_reused_pid_is_never_signalled() -> None:
    bystander = subprocess.Popen(["sleep", "300"])  # noqa: S607
    try:
        tracked = process_module._AgentProcesses(root_pid=_dead_pid())  # noqa: SLF001
        # Recorded with another start time: the pid now belongs to a different process.
        table = process_module._process_table()  # noqa: SLF001
        tracked.seen[bystander.pid] = table[bystander.pid][1] + 1
        asyncio.run(tracked.reap())
        assert _alive(bystander.pid)
    finally:
        bystander.kill()
        bystander.wait()


def test_a_reused_root_pid_is_not_walked() -> None:
    root = subprocess.Popen(
        ["bash", "-c", "sleep 300 & wait"],  # noqa: S607
        start_new_session=True,
    )
    try:
        for _ in range(50):
            if any(ppid == root.pid for ppid, _ in process_module._process_table().values()):  # noqa: SLF001
                break
            time.sleep(0.05)
        tracked = process_module._AgentProcesses(root_pid=root.pid)  # noqa: SLF001
        assert tracked.root_started is not None
        tracked.root_started += 1  # the pid now names a different process
        tracked.scan()
        assert tracked.seen == {}
    finally:
        os.killpg(root.pid, signal.SIGKILL)
        root.wait()


@pytest.mark.asyncio
async def test_a_child_started_while_the_agent_terminates_is_stopped(tmp_path: Path) -> None:
    marker = tmp_path / "late.pid"
    script = (
        f"trap 'setsid sleep 300 </dev/null >/dev/null 2>&1 & echo $! > {marker}; "
        "sleep 3; exit 0' TERM; sleep 300 & wait"
    )
    result = await run_jsonl_process(
        ["bash", "-c", script],
        prompt="",
        cwd=os.getcwd(),
        timeout_seconds=1,
        provider="synthetic",
    )
    assert result.timed_out
    # The agent is frozen and killed: its SIGTERM handler never runs, so it never got
    # the chance to start a child while it was being stopped.
    assert not marker.exists()


@pytest.mark.asyncio
async def test_cancelling_during_the_drain_stops_the_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_module, "_READER_DRAIN_SECONDS", 60.0)
    delivered: list[object] = []

    async def slow(event: dict[str, object]) -> None:
        await asyncio.sleep(0.3)
        delivered.append(event)

    task = asyncio.create_task(
        run_jsonl_process(
            ["bash", "-c", 'for i in $(seq 1 30); do echo "{\\"n\\":$i}"; done'],
            prompt="",
            cwd=os.getcwd(),
            timeout_seconds=60,
            provider="synthetic",
            on_event=slow,
        )
    )
    await asyncio.sleep(1.5)  # the agent has exited; events are still being delivered
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    count = len(delivered)
    assert count < 30
    await asyncio.sleep(1)
    assert len(delivered) == count


@pytest.mark.asyncio
async def test_processes_forking_while_they_are_stopped_are_all_killed(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    script = (
        "for i in $(seq 1 3); do setsid bash -c "
        f"'while :; do sleep 300 </dev/null >/dev/null 2>&1 & echo $! >> {pids}; sleep 0.01; done' "
        f"</dev/null >/dev/null 2>&1 & echo $! >> {pids}; done; sleep 300"
    )
    result = await run_jsonl_process(
        ["bash", "-c", script],
        prompt="",
        cwd=os.getcwd(),
        timeout_seconds=2,
        provider="synthetic",
    )
    assert result.timed_out
    spawned = [int(line) for line in pids.read_text().split()]
    assert len(spawned) > 10
    assert [pid for pid in spawned if _alive(pid)] == []


@pytest.mark.asyncio
async def test_an_orphaned_stopped_group_gets_no_chance_to_run_a_handler(tmp_path: Path) -> None:
    # The inner shell (job control on) puts the looping child in its own process group.
    # Killing the inner shell before that child orphans the stopped group, and the kernel
    # sends it SIGHUP and SIGCONT; the child's handler would then start a process.
    marker = tmp_path / "hup.pid"
    (tmp_path / "child.sh").write_text(
        f"trap 'setsid sleep 300 </dev/null >/dev/null 2>&1 & echo $! > {marker}; exit 0' HUP\n"
        "while :; do sleep 0.2; done\n"
    )
    (tmp_path / "inner.sh").write_text(f"set -m\nbash {tmp_path / 'child.sh'} &\nwait\n")
    result = await run_jsonl_process(
        ["bash", "-c", f"bash {tmp_path / 'inner.sh'} & sleep 300"],
        prompt="",
        cwd=os.getcwd(),
        timeout_seconds=2,
        provider="synthetic",
    )
    assert result.timed_out
    await asyncio.sleep(0.5)
    assert not marker.exists() or not _alive(int(marker.read_text()))


@pytest.mark.asyncio
async def test_parents_are_frozen_first_and_killed_last(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, signal.Signals]] = []
    real = process_module._signal  # noqa: SLF001

    def record(pid: int, started: int, sig: signal.Signals) -> None:
        calls.append((pid, sig))
        real(pid, started, sig)

    monkeypatch.setattr(process_module, "_signal", record)
    root = subprocess.Popen(  # noqa: ASYNC220 - a plain child of the test
        ["bash", "-c", "bash -c 'bash -c \"sleep 300; :\" & wait' & wait"],  # noqa: S607
        start_new_session=True,
    )
    try:
        tracked = process_module._AgentProcesses(root_pid=root.pid)  # noqa: SLF001
        for _ in range(100):
            tracked.scan()
            if len(tracked.seen) >= 3:
                break
            await asyncio.sleep(0.05)
        table = process_module._process_table()  # noqa: SLF001
        depth = {pid: 0 for pid in tracked.seen}
        for pid in depth:
            walk = pid
            while walk != root.pid and walk in table:
                walk = table[walk][0]
                depth[pid] += 1
        await tracked.reap()
    finally:
        os.killpg(root.pid, signal.SIGKILL)
        root.wait()
    stops = [depth[pid] for pid, sig in calls if sig == signal.SIGSTOP]
    kills = [depth[pid] for pid, sig in calls if sig == signal.SIGKILL]
    assert len(stops) >= 3
    assert stops == sorted(stops)
    assert kills == sorted(kills, reverse=True)


def _delegated() -> bool:
    own = process_module._own_cgroup()  # noqa: SLF001
    return (
        own is not None
        and own.name == process_module.DELEGATED_SUBGROUP
        and os.access(own.parent, os.W_OK)
    )


def test_run_cgroups_need_the_explicit_delegation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = tmp_path / "worker.service"
    for name in ("supervisor", "elsewhere"):
        (service / name).mkdir(parents=True)
    (service / "cgroup.kill").write_text("")
    monkeypatch.setattr(process_module, "_own_cgroup", lambda: service / "elsewhere")
    assert process_module._RunCgroup.create() is None  # noqa: SLF001
    assert sorted(entry.name for entry in service.iterdir()) == [
        "cgroup.kill",
        "elsewhere",
        "supervisor",
    ]


def test_the_stale_sweep_spares_live_and_populated_run_cgroups(tmp_path: Path) -> None:
    prefix = process_module._RUN_CGROUP_PREFIX  # noqa: SLF001
    for name, populated in (("stale", 0), ("busy", 1), ("live", 0)):
        directory = tmp_path / f"{prefix}{name}"
        directory.mkdir()
        (directory / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n")
    process_module._LIVE_RUN_CGROUPS.add(f"{prefix}live")  # noqa: SLF001
    removed: list[str] = []
    real_rmdir = Path.rmdir

    def rmdir(self: Path) -> None:
        removed.append(self.name)
        for child in self.iterdir():
            child.unlink()
        real_rmdir(self)

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Path, "rmdir", rmdir)
            process_module._remove_stale_run_cgroups(tmp_path)  # noqa: SLF001
    finally:
        process_module._LIVE_RUN_CGROUPS.discard(f"{prefix}live")  # noqa: SLF001
    assert removed == [f"{prefix}stale"]


@pytest.mark.skipif(
    not _delegated(), reason="needs a delegated cgroup (DelegateSubgroup=supervisor)"
)
@pytest.mark.asyncio
async def test_a_run_cgroup_takes_down_everything_the_run_started(tmp_path: Path) -> None:
    marker = tmp_path / "hup.pid"
    pids = tmp_path / "pids"
    (tmp_path / "child.sh").write_text(
        f"trap 'setsid sleep 300 </dev/null >/dev/null 2>&1 & echo $! > {marker}; exit 0' HUP\n"
        "while :; do sleep 0.2; done\n"
    )
    (tmp_path / "inner.sh").write_text(f"set -m\nbash {tmp_path / 'child.sh'} &\nwait\n")
    script = (
        f"bash {tmp_path / 'inner.sh'} & "
        f"(setsid sleep 300 </dev/null >/dev/null 2>&1 & echo $! >> {pids}) ; "
        f"setsid bash -c 'while :; do sleep 300 </dev/null >/dev/null 2>&1 & echo $! >> {pids}; "
        "sleep 0.01; done' </dev/null >/dev/null 2>&1 & "
        + _ESCAPE.replace("setsid sleep 300", "setsid sleep 300 </dev/null")
        + "; sleep 1; exit 0"
    )
    own = process_module._own_cgroup()  # noqa: SLF001
    assert own is not None
    before = {entry.name for entry in own.parent.iterdir()}
    result = await run_jsonl_process(
        ["bash", "-c", script],
        prompt="",
        cwd=os.getcwd(),
        timeout_seconds=60,
        provider="synthetic",
    )
    spawned = [int(line) for line in pids.read_text().split()] + [_escaped_pid(result.events)]
    if marker.exists():
        spawned.append(int(marker.read_text()))
    assert len(spawned) > 3
    assert [pid for pid in spawned if _alive(pid)] == []
    after = {entry.name for entry in own.parent.iterdir()}
    assert {name for name in after - before if name.startswith("agent-run-")} == set()


@pytest.mark.asyncio
async def test_an_agent_that_never_reads_its_prompt_still_times_out() -> None:
    # stdin stays open and is never read: the prompt write blocks once the pipe is full.
    result = await asyncio.wait_for(
        run_jsonl_process(
            ["bash", "-c", "sleep 300"],
            prompt="x" * 4_000_000,  # far beyond the pipe buffer
            cwd=os.getcwd(),
            timeout_seconds=2,
            provider="synthetic",
        ),
        timeout=30,
    )
    assert result.timed_out


@pytest.mark.asyncio
async def test_an_agent_that_cannot_be_stopped_fails_the_run_without_holding_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_module, "_AGENT_EXIT_SECONDS", 0.5)

    async def no_reap(self: object, *, include_root: bool = False) -> None:
        return None

    # Stands in for a leader stuck in uninterruptible sleep: no kill takes effect (it
    # ends by itself 3 s later, long after the bound).
    monkeypatch.setattr(process_module._RunCgroup, "create", classmethod(lambda cls: None))  # noqa: SLF001
    monkeypatch.setattr(process_module._AgentProcesses, "reap", no_reap)  # noqa: SLF001
    monkeypatch.setattr(process_module.os, "killpg", lambda pid, sig: None)
    started = time.monotonic()
    with pytest.raises(process_module.ProviderCleanupIncompleteError):
        await run_jsonl_process(
            ["bash", "-c", "sleep 3"],
            prompt="",
            cwd=os.getcwd(),
            timeout_seconds=1,
            provider="synthetic",
        )
    assert time.monotonic() - started < 3


@pytest.mark.asyncio
async def test_a_prompt_that_cannot_be_encoded_starts_nothing(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    with pytest.raises(UnicodeEncodeError):
        await run_jsonl_process(
            ["bash", "-c", f"touch {marker}"],
            prompt="\ud800",  # an unpaired surrogate
            cwd=os.getcwd(),
            timeout_seconds=10,
            provider="synthetic",
        )
    assert not marker.exists()


@pytest.mark.asyncio
async def test_an_unexpected_prompt_delivery_error_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_drain(self: object) -> None:
        raise OSError("synthetic stdin failure")

    monkeypatch.setattr(asyncio.StreamWriter, "drain", failing_drain)
    with pytest.raises(OSError, match="synthetic stdin failure"):
        await run_jsonl_process(
            ["bash", "-c", "cat >/dev/null; exit 0"],
            prompt="hello",
            cwd=os.getcwd(),
            timeout_seconds=10,
            provider="synthetic",
        )
