from __future__ import annotations

import asyncio
import logging
import os
import signal
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engineering_control.security.redaction import SecretRedactor

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
_PROVIDER_STREAM_LIMIT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    events: tuple[dict[str, Any], ...]
    stderr: str
    timed_out: bool = False


# NEW-79a: offline toolchains the scan worker prepares for makers and checkers
# (delivery.prepare_toolchain fills these caches with network; they are read-only here).
TOOLCHAIN_CACHE_ROOT = Path(
    os.environ.get("ENGINEERING_TOOLCHAIN_CACHE_ROOT", "/srv/engineering/cache/scan/toolchain")
)
GO_ROOT_BIN = Path("/usr/local/go/bin")


@dataclass(frozen=True)
class SandboxToolchain:
    """NEW-159: a project's non-default Node.js/pnpm selection for the agent sandbox.

    ``path_prefix`` directories go first on PATH; ``variables`` are added verbatim. The
    default toolchain (Node 22, worker pnpm) is represented by ``None``, never by an
    empty instance, so the default environment stays byte-identical.
    """

    path_prefix: tuple[str, ...]
    variables: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.path_prefix or any(
            not entry.startswith("/") or ":" in entry for entry in self.path_prefix
        ):
            raise ValueError("sandbox toolchain PATH entries must be absolute directories")
        if any(key == "PATH" for key, _ in self.variables):
            raise ValueError("sandbox toolchain variables cannot replace PATH")


def sandbox_toolchain_environment(
    base: Mapping[str, str], toolchain: SandboxToolchain | None = None
) -> dict[str, str]:
    """Go (module cache prefilled, no proxy) and Trivy (DB prefilled, offline) settings."""
    path = base.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if GO_ROOT_BIN.is_dir() and str(GO_ROOT_BIN) not in path.split(":"):
        path = f"{GO_ROOT_BIN}:{path}"
    extra: dict[str, str] = {}
    if toolchain is not None:
        # NEW-159: the project's selected node and pnpm win over /usr/local/bin.
        path = ":".join((*toolchain.path_prefix, path))
        extra = dict(toolchain.variables)
    return {
        "PATH": path,
        "GOMODCACHE": str(TOOLCHAIN_CACHE_ROOT / "go-mod"),
        # The sandbox may write only the worktree and the temp directory.
        "GOCACHE": str(Path(tempfile.gettempdir()) / f"go-build-{os.getuid()}"),
        "GOPROXY": "off",
        "GOTOOLCHAIN": "local",
        "GOTELEMETRY": "off",
        # Two parallel compile jobs: the worker has little RAM (NEW-77 OOM kills).
        "GOFLAGS": "-mod=mod -p=2",
        "TRIVY_CACHE_DIR": str(TOOLCHAIN_CACHE_ROOT / "trivy"),
        "TRIVY_SKIP_DB_UPDATE": "true",
        "TRIVY_SKIP_JAVA_DB_UPDATE": "true",
        "TRIVY_OFFLINE_SCAN": "true",
        "TRIVY_CACHE_BACKEND": "memory",
        "TRIVY_NO_PROGRESS": "true",
        **extra,
    }


def subscription_only_environment(
    provider: str,
    overrides: Mapping[str, str] | None = None,
    *,
    toolchain: SandboxToolchain | None = None,
) -> dict[str, str]:
    """Build an environment that cannot silently select API-paid provider credentials."""
    env = dict(os.environ)
    forbidden = {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "OMNIROUTE_URL",
        "OMNIROUTE_BASE_URL",
        "LLM_BASE_URL",
        "MODEL_GATEWAY",
        "MODEL_ROUTER",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
    for key in forbidden:
        env.pop(key, None)
    env["ENGINEERING_PROVIDER"] = provider
    env["NO_COLOR"] = "1"
    # NEW-45: makers/checkers run the project's tests in shared worktrees. pytest
    # creates .pytest_cache through a 0700 temp directory, so the scan worker's next
    # `git clean` of that worktree failed with "Permission denied". No cache, no
    # bytecode: nothing an agent's test run leaves behind is unremovable.
    env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # NEW-48: a worktree .mypy_cache written by the maker is not writable by the
    # checker (another runner uid). /dev/null is mypy's documented "no cache".
    env["MYPY_CACHE_DIR"] = "/dev/null"
    env.update(sandbox_toolchain_environment(env, toolchain))
    if overrides:
        disallowed = forbidden.intersection(overrides)
        if disallowed:
            raise ValueError(
                f"API-paid or third-party provider variables are forbidden: {sorted(disallowed)}"
            )
        env.update(overrides)
    return env


# NEW-188: the agent CLI runs commands in their own sessions (codex's sandbox does), so
# killing its process group misses them. Left running, a maker's hung `vitest run`
# processes outlived a 2-hour timeout in the worker's cgroup and stalled the codex
# worker for 21 minutes (gntik-ui #20, 2026-10-12). The run cgroup above is the
# containment; this /proc tracker is the fallback where no cgroup is delegated. Every
# process descended from the agent is recorded while it is still linked to it. When the
# run ends they are frozen (SIGSTOP, parents first, rescanning until nothing new
# appears) and killed (children first). Known limits, which the cgroup does not have: a
# process started and orphaned within one scan interval before the agent exits on its
# own is not recorded, and a process group spread across branches can still be orphaned
# between two kills under heavy scheduling delay.
_TRACK_INTERVAL_SECONDS = 2.0
_FREEZE_ROUNDS = 50
_READER_DRAIN_SECONDS = 30.0
_AGENT_EXIT_SECONDS = 10.0
_PROC = Path("/proc")


class ProviderOutputIncompleteError(RuntimeError):
    """The agent exited but its output did not finish: never a successful result."""


class ProviderCleanupIncompleteError(RuntimeError):
    """The agent could not be stopped within the bound: the run fails, the worker goes on."""


# NEW-188: where the worker unit delegates its cgroup (Delegate=yes,
# DelegateSubgroup=supervisor, ProtectControlGroups=private), each agent run gets a
# cgroup of its own next to the worker's, and the kernel kills everything in it at once
# (cgroup.kill) when the run ends: no scan window, whatever the processes did with
# sessions, process groups or signal handlers. Elsewhere the tracker below is used.
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_RUN_CGROUP_PREFIX = "agent-run-"
_CGROUP_EMPTY_SECONDS = 10.0
# Moves itself into the run cgroup, then becomes the agent; if the move fails the agent
# never starts and the run fails visibly.
_ENTER_CGROUP = 'echo $$ > "$0" && exec "$@"'
# The unit's DelegateSubgroup=: only a worker placed there by that explicit delegation
# uses run cgroups; a writable cgroup reached any other way (a user session) does not.
DELEGATED_SUBGROUP = "supervisor"
# Run cgroups this worker process owns: the stale sweep never touches one that was just
# created and is not populated yet.
_LIVE_RUN_CGROUPS: set[str] = set()
# NEW-226: a run cgroup's memory.max. A runaway command is OOM-killed inside its own run
# (the agent sees a failed command) before the worker reaches MemoryHigh, where the
# kernel throttles every process in it: on 2026-10-19 one maker's 4.45 GB `node --test`
# held the codex worker at 97% memory pressure and the other loop's auth probe timed out.
# Unless configured, a run may use the worker's MemoryHigh (else MemoryMax) less this
# reserve for the supervisor (heartbeats, probes) and the other run's working set.
_RUN_MEMORY_RESERVE_BYTES = 1 << 30
_LOGGER = logging.getLogger(__name__)


def _own_cgroup() -> Path | None:
    try:
        lines = (_PROC / "self" / "cgroup").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("0::"):
            relative = line[3:].strip().lstrip("/")
            return _CGROUP_ROOT / relative if relative else None
    return None


class _RunCgroup:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def create(cls) -> _RunCgroup | None:
        own = _own_cgroup()
        if own is None or own.name != DELEGATED_SUBGROUP:
            return None
        parent = own.parent
        if not os.access(parent, os.W_OK) or not (parent / "cgroup.kill").exists():
            return None
        _remove_stale_run_cgroups(parent)
        path = parent / f"{_RUN_CGROUP_PREFIX}{os.urandom(16).hex()}"
        try:
            path.mkdir()
        except OSError:
            return None
        if not (path / "cgroup.kill").exists():
            with suppress(OSError):
                path.rmdir()
            return None
        _LIVE_RUN_CGROUPS.add(path.name)
        return cls(path)

    def limit_memory(self, configured: int | None) -> None:
        _limit_run_memory(self.path.parent, self.path, configured)

    def wrap(self, command: Sequence[str]) -> list[str]:
        return ["/bin/sh", "-c", _ENTER_CGROUP, str(self.path / "cgroup.procs"), *command]

    def populated(self) -> bool:
        try:
            events = (self.path / "cgroup.events").read_text()
        except OSError:
            return False
        return "populated 1" in events

    async def kill(self) -> None:
        with suppress(OSError):
            (self.path / "cgroup.kill").write_text("1")
        deadline = asyncio.get_running_loop().time() + _CGROUP_EMPTY_SECONDS
        # Polled: cgroup.events changes are not delivered to asyncio.
        while self.populated() and asyncio.get_running_loop().time() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        with suppress(OSError):
            self.path.rmdir()  # left in place if a process is stuck; a later sweep retries
        _LIVE_RUN_CGROUPS.discard(self.path.name)


def _cgroup_limit(path: Path) -> int | None:
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return int(value) if value.isdigit() else None


def run_memory_limit(worker: Path, configured: int | None) -> int | None:
    """NEW-226: the memory.max of a run cgroup under ``worker`` (None: no limit)."""
    if configured is not None:
        return configured
    budget = _cgroup_limit(worker / "memory.high") or _cgroup_limit(worker / "memory.max")
    if budget is None:
        return None
    return max(budget - _RUN_MEMORY_RESERVE_BYTES, budget // 2)


def _limit_run_memory(worker: Path, run: Path, configured: int | None) -> None:
    """Enable the delegated memory controller for run cgroups and cap this one.

    The unit delegates the controller (Delegate=yes) but leaves its subtree_control
    empty; the worker cgroup itself holds no process (the worker is in "supervisor"),
    so it may enable it. A limit that cannot be set leaves the run as it was before."""
    limit = run_memory_limit(worker, configured)
    if limit is None:
        return
    try:
        control = worker / "cgroup.subtree_control"
        if "memory" not in control.read_text().split():
            control.write_text("+memory")
        (run / "memory.max").write_text(str(limit))
    except OSError as error:
        _LOGGER.warning("run cgroup memory limit not applied: %s", error.strerror or error)


def _remove_stale_run_cgroups(parent: Path) -> None:
    """Remove empty run cgroups a crashed or stopped worker left behind."""
    try:
        entries = list(parent.iterdir())
    except OSError:
        return
    for entry in entries:
        if (
            entry.name.startswith(_RUN_CGROUP_PREFIX)
            and entry.name not in _LIVE_RUN_CGROUPS
            and not _RunCgroup(entry).populated()
        ):
            with suppress(OSError):
                entry.rmdir()


def _process_table() -> dict[int, tuple[int, int]]:
    """pid -> (ppid, start time in clock ticks) for every live process.

    Zombies are left out: they hold no pipes, cannot fork, and their children have
    already been reparented."""
    table: dict[int, tuple[int, int]] = {}
    try:
        entries = os.listdir(_PROC)
    except OSError:
        return table
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            data = (_PROC / entry / "stat").read_bytes()
        except OSError:
            continue
        # Fields after the parenthesised command name, which may contain spaces and ')'.
        fields = data[data.rfind(b")") + 2 :].split()
        if not fields or fields[0] in (b"Z", b"X"):
            continue
        try:
            table[int(entry)] = (int(fields[1]), int(fields[19]))
        except (IndexError, ValueError):
            continue
    return table


def _signal(pid: int, started: int, sig: signal.Signals) -> None:
    """Signal pid only if it is still the process that started at ``started``.

    The pidfd pins one process: if the pid is reused after it is opened, the start-time
    check reads the new process and nothing is sent; if not, the signal reaches the
    process that was checked."""
    try:
        handle = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if _process_table_entry(pid) == started:
            signal.pidfd_send_signal(handle, sig)
    except OSError:
        pass
    finally:
        os.close(handle)


def _process_table_entry(pid: int) -> int | None:
    try:
        data = (_PROC / str(pid) / "stat").read_bytes()
        return int(data[data.rfind(b")") + 2 :].split()[19])
    except (OSError, IndexError, ValueError):
        return None


class _AgentProcesses:
    """Processes one agent run started, identified by pid and start time.

    Membership is only ever inherited: a process is recorded when it descends from the
    agent, or from a recorded process, whose identity still matches. Nothing a command
    can write (environment, names) makes a process a member."""

    def __init__(self, root_pid: int) -> None:
        # The root is the agent CLI this run spawned: never init, never this worker.
        if root_pid <= 1 or root_pid == os.getpid():
            raise ValueError("agent process tracking needs the spawned agent's pid")
        self.root_pid = root_pid
        self.root_started = _process_table_entry(root_pid)
        self.seen: dict[int, int] = {}

    def scan(self) -> dict[int, tuple[int, int]]:
        table = _process_table()
        children: dict[int, list[int]] = {}
        for pid, (ppid, _) in table.items():
            children.setdefault(ppid, []).append(pid)
        # Walk only from processes whose identity is unchanged: a reused pid is not ours.
        pending = [
            pid for pid, started in self.seen.items() if table.get(pid, (0, -1))[1] == started
        ]
        if (
            self.root_started is not None
            and table.get(self.root_pid, (0, -1))[1] == self.root_started
        ):
            pending.append(self.root_pid)
        while pending:
            for child in children.get(pending.pop(), ()):
                started = table[child][1]
                if self.seen.get(child) != started:
                    self.seen[child] = started
                    pending.append(child)
        return table

    def _alive(self, *, include_root: bool = False) -> dict[int, int]:
        table = self.scan()
        # This worker and its ancestors are never signalled, whatever was recorded.
        protected = {1}
        pid = os.getpid()
        while pid > 1 and pid not in protected:
            protected.add(pid)
            pid = table.get(pid, (1, 0))[0]
        members = dict(self.seen)
        if include_root and self.root_started is not None:
            members[self.root_pid] = self.root_started
        return {
            pid: started
            for pid, started in members.items()
            if pid not in protected and table.get(pid, (0, -1))[1] == started
        }

    async def watch(self) -> None:
        while True:
            self.scan()
            await asyncio.sleep(_TRACK_INTERVAL_SECONDS)

    async def reap(self, *, include_root: bool = False) -> None:
        """Freeze every recorded process, then kill them all.

        Freezing goes parents first, so a frozen process never loses a running parent
        that could exit on its own. Killing goes children first: a stopped process group
        orphaned by its parent's death gets SIGHUP and SIGCONT from the kernel, and only
        a SIGKILL already pending keeps its handler from running."""
        frozen: set[tuple[int, int]] = set()
        for _ in range(_FREEZE_ROUNDS):
            fresh = set(self._alive(include_root=include_root).items()) - frozen
            if not fresh:
                break
            for pid, started in _by_depth(fresh, deepest_first=False):
                _signal(pid, started, signal.SIGSTOP)
            frozen |= fresh
            await asyncio.sleep(0.02)  # a fork that raced the stop shows up next scan
        for _ in range(5):
            alive = self._alive(include_root=include_root)
            if not alive:
                return
            for pid, started in _by_depth(alive.items(), deepest_first=True):
                _signal(pid, started, signal.SIGKILL)
            deadline = asyncio.get_running_loop().time() + 1.0
            # Polled: nothing signals the exit of a process that is not our child.
            while self._alive(include_root=include_root) and (  # noqa: ASYNC110
                asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.05)


def _by_depth(members: Iterable[tuple[int, int]], *, deepest_first: bool) -> list[tuple[int, int]]:
    """Order (pid, start) pairs by their depth in the process tree."""
    table = _process_table()

    def depth(pid: int) -> int:
        level = 0
        while pid > 1 and level < 4096 and pid in table:
            pid = table[pid][0]
            level += 1
        return level

    return sorted(members, key=lambda member: depth(member[0]), reverse=deepest_first)


async def _uninterrupted(cleanup: Awaitable[None]) -> bool:
    """Run cleanup to completion even if the caller is cancelled meanwhile.

    Returns whether a cancellation arrived, for the caller to re-raise."""
    task = asyncio.ensure_future(cleanup)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    return cancelled


async def _join(future: asyncio.Future[Any]) -> None:
    await asyncio.gather(future, return_exceptions=True)


async def _drain(readers: asyncio.Future[Any]) -> None:
    """Wait a bounded time for the output readers. Output that has not finished by then
    (a pipe held by a process that could not be stopped) fails the run: a result built
    from part of the events is never reported."""
    try:
        done, _ = await asyncio.wait({readers}, timeout=_READER_DRAIN_SECONDS)
    except asyncio.CancelledError:
        # The readers (and their event callbacks) never outlive the activity.
        readers.cancel()
        await _uninterrupted(_join(readers))
        raise
    if not done:
        readers.cancel()
        await asyncio.gather(readers, return_exceptions=True)
        raise ProviderOutputIncompleteError(
            "provider output did not finish within the drain limit after the agent exited"
        )
    readers.result()


async def _exited(process: asyncio.subprocess.Process) -> int:
    """The agent's exit status, as soon as it exits.

    Process.wait() resolves only once the child's pipes are closed too (when it is called
    before the exit), so a descendant still holding stdout kept it waiting for the whole
    agent timeout. The returncode is set on exit, whoever holds the pipes."""
    while process.returncode is None:  # noqa: ASYNC110 - no event fires before the pipes close
        await asyncio.sleep(0.1)
    return process.returncode


async def run_jsonl_process(
    command: Sequence[str],
    *,
    prompt: str,
    cwd: str,
    timeout_seconds: int,
    provider: str,
    on_event: EventCallback | None = None,
    environment: Mapping[str, str] | None = None,
    max_stderr_bytes: int = 256_000,
    toolchain: SandboxToolchain | None = None,
    run_memory_max_bytes: int | None = None,
) -> ProcessResult:
    if not command or any(not isinstance(part, str) or "\x00" in part for part in command):
        raise ValueError("command must be a non-empty string sequence without NUL bytes")

    redactor = SecretRedactor()
    safe_prompt = str(redactor.redact_text(prompt).value)
    prompt_bytes = safe_prompt.encode("utf-8")  # fails here, before anything is started
    cgroup = _RunCgroup.create()
    try:
        if cgroup is not None:
            cgroup.limit_memory(run_memory_max_bytes)
        process = await asyncio.create_subprocess_exec(
            *(cgroup.wrap(command) if cgroup is not None else command),
            cwd=cwd,
            env=subscription_only_environment(provider, environment, toolchain=toolchain),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=_PROVIDER_STREAM_LIMIT_BYTES,
        )
    except BaseException:
        if cgroup is not None:
            await _uninterrupted(cgroup.kill())
        raise
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdin = process.stdin
    stdout = process.stdout
    stderr_stream = process.stderr

    async def feed() -> None:
        # Inside the timeout and the cleanup: an agent that never reads its prompt cannot
        # block the run, and its output is read while the prompt is still being written.
        try:
            stdin.write(prompt_bytes)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            stdin.close()

    events: list[dict[str, Any]] = []
    stderr_chunks: list[bytes] = []
    stderr_size = 0

    async def consume_stdout() -> None:
        while True:
            try:
                line = await stdout.readline()
            except ValueError as error:
                raise RuntimeError(
                    "provider JSONL event exceeds the bounded stream limit"
                ) from error
            if not line:
                break
            report = redactor.redact_json_line(line.decode("utf-8", errors="replace"))
            try:
                import json

                event = json.loads(str(report.value))
            except (ValueError, TypeError):
                event = {"type": "provider.unstructured", "text": str(report.value).rstrip()}
            if not isinstance(event, dict):
                event = {"type": "provider.value", "value": event}
            events.append(event)
            if on_event is not None:
                await on_event(event)

    async def consume_stderr() -> None:
        nonlocal stderr_size
        pending = ""

        async def emit_visible(value: str) -> None:
            report = redactor.redact_text(value.rstrip("\r\n"))
            if on_event is not None and str(report.value):
                await on_event({"type": "provider.stderr", "text": str(report.value)})

        while chunk := await stderr_stream.read(8192):
            remaining = max_stderr_bytes - stderr_size
            if remaining > 0:
                stderr_chunks.append(chunk[:remaining])
                stderr_size += min(len(chunk), remaining)
            pending += chunk.decode("utf-8", errors="replace")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                await emit_visible(line)
            while len(pending.encode("utf-8")) > 16_384:
                encoded = pending.encode("utf-8")
                segment = encoded[:16_384].decode("utf-8", errors="ignore")
                await emit_visible(segment)
                pending = pending[len(segment) :]
        if pending:
            await emit_visible(pending)

    timed_out = False
    cancelled = False
    feeder = asyncio.ensure_future(feed())
    readers = asyncio.gather(consume_stdout(), consume_stderr())
    tracked = _AgentProcesses(process.pid) if cgroup is None else None
    watcher = asyncio.create_task(tracked.watch()) if tracked is not None else None
    try:
        await asyncio.wait_for(_exited(process), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
    except asyncio.CancelledError:
        cancelled = True

    async def finish() -> bool:
        """Stop everything the run started; False if the agent itself did not exit."""
        feeder.cancel()
        (delivered,) = await asyncio.gather(feeder, return_exceptions=True)
        if isinstance(delivered, Exception):
            feeder_errors.append(delivered)
        if cgroup is not None:
            # Everything the run started, still running or not, dies in one write.
            await cgroup.kill()
        if watcher is not None and tracked is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            # A timed-out or cancelled agent is frozen and killed with its descendants;
            # no SIGTERM handler runs, so it cannot start anything while it is stopped.
            await tracked.reap(include_root=process.returncode is None)
        if process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            # A process in uninterruptible sleep ignores even SIGKILL until it wakes;
            # the worker is never held hostage by it.
            with suppress(TimeoutError):
                await asyncio.wait_for(_exited(process), timeout=_AGENT_EXIT_SECONDS)
        return process.returncode is not None

    outcome: list[bool] = []
    feeder_errors: list[Exception] = []

    async def finish_and_record() -> None:
        outcome.append(await finish())

    cancelled = await _uninterrupted(finish_and_record()) or cancelled
    stopped = outcome[0]
    if cancelled or not stopped or feeder_errors:
        readers.cancel()
        await asyncio.gather(readers, return_exceptions=True)
        if cancelled:
            raise asyncio.CancelledError
        if not stopped:
            raise ProviderCleanupIncompleteError(
                "the agent did not exit after it was killed; its cgroup is left in place"
            )
        # The agent never received its whole prompt: its result is not trusted.
        raise feeder_errors[0]
    await _drain(readers)

    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    stderr = str(redactor.redact_text(stderr).value)
    return ProcessResult(process.returncode or 0, tuple(events), stderr, timed_out)


# Error code of a transient provider-capacity failure ("Selected model is at capacity").
# The provider activity retries the same invocation (same provider, same model) with a
# bounded backoff before it reports the failure; nothing ever switches provider or model.
PROVIDER_CAPACITY_ERROR = "PROVIDER_MODEL_AT_CAPACITY"
_CAPACITY_MARKERS = (
    "selected model is at capacity",
    "model is at capacity",
    "model is currently at capacity",
    "overloaded_error",
)


def _error_event_text(events: tuple[dict[str, Any], ...]) -> str:
    """Text of the provider's own error events only (never agent messages or output)."""
    parts: list[str] = []
    for event in events[-50:]:
        event_type = str(event.get("type", ""))
        if event_type == "result":
            # Claude's final envelope: only an explicit error result counts; a successful
            # result carries agent output and is never inspected.
            if event.get("is_error") is not True:
                continue
            keys: tuple[str, ...] = ("errors", "subtype", "result", "error")
        elif "error" in event or "error" in event_type or event_type.endswith(".failed"):
            keys = ("error", "message", "detail")
        else:
            continue
        for key in keys:
            value = event.get(key)
            if isinstance(value, list):
                parts.extend(str(item)[:2000] for item in value[:20])
            elif value is not None:
                parts.append(str(value)[:2000])
    return "\n".join(parts).lower()


def is_capacity_failure(result: ProcessResult) -> bool:
    if result.timed_out or result.exit_code == 0:
        return False
    text = f"{result.stderr.lower()}\n{_error_event_text(result.events)}"
    return any(marker in text for marker in _CAPACITY_MARKERS)


def classify_failure(result: ProcessResult) -> tuple[str, str]:
    if result.timed_out:
        return "TIMED_OUT", "PROVIDER_TIMEOUT"
    if is_capacity_failure(result):
        return "FAILED", PROVIDER_CAPACITY_ERROR
    combined = result.stderr.lower()
    if any(
        marker in combined
        for marker in (
            "rate limit",
            "rate_limit",
            "usage limit",
            "quota exceeded",
            "too many requests",
            "http 429",
        )
    ):
        return "RATE_LIMITED", "PROVIDER_RATE_LIMIT"
    return "FAILED", "PROVIDER_EXIT_NONZERO"
