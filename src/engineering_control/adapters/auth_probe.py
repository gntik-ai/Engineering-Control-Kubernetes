"""Bounded Linux native-auth diagnostics; never log authentication output.

The direct child is deliberately not reaped until the last process-group signal:
its unreaped PID pins the group identifier even after the leader has exited.
This owns ordinary descendants, not descendants which deliberately escape with
setsid(); service cgroup containment remains a separate operational requirement.
As with subprocess itself, OS process creation is not interruptible by this
deadline. There is no await between creation and establishing cleanup ownership.
"""

from __future__ import annotations

import asyncio
import math
import os
import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from engineering_control.adapters.process import subscription_only_environment

_POLL_SECONDS = 0.01
_TERM_GRACE_SECONDS = 0.25
_KILL_WAIT_SECONDS = 1.0


class AuthProbeError(RuntimeError):
    """Value-free inability to determine auth, distinct from a logged-out result."""

    def __init__(self, reason: Literal["TIMED_OUT", "OUTPUT_LIMIT", "INVALID_RESPONSE"]) -> None:
        self.reason = reason
        super().__init__("native authentication status unavailable: " + reason)


@dataclass(frozen=True)
class AuthProbeResult:
    exit_code: int | None
    stdout: bytes = field(default=b"", repr=False)
    failure: Literal["TIMED_OUT", "OUTPUT_LIMIT"] | None = None


def _exited(process: subprocess.Popen[bytes]) -> bool:
    return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


async def _cleanup(process: subprocess.Popen[bytes]) -> None:
    # Do not use poll()/wait() or the JSONL helper before the final group signal.
    # Both would release the leader PID, or skip remaining descendants.
    for sig, delay in ((signal.SIGTERM, _TERM_GRACE_SECONDS), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        if delay:
            await asyncio.sleep(delay)
    deadline = asyncio.get_running_loop().time() + _KILL_WAIT_SECONDS
    while not _exited(process):
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("native authentication child did not exit after termination")
        await asyncio.sleep(_POLL_SECONDS)
    process.wait(timeout=0)


async def _join_cleanup(process: subprocess.Popen[bytes]) -> None:
    cleanup = asyncio.create_task(_cleanup(process))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    # Cleanup failure must not be silently treated as authenticated/unavailable.
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


async def run_auth_probe(
    command: Sequence[str],
    *,
    provider: str,
    capture_stdout: bool = False,
    timeout_seconds: float = 15.0,
    max_stdout_bytes: int = 65_536,
) -> AuthProbeResult:
    if not command or any(not isinstance(item, str) or "\x00" in item for item in command):
        raise ValueError("invalid native authentication command")
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
        raise ValueError("native authentication timeout outside bound")
    if not 0 < max_stdout_bytes <= 65_536:
        raise ValueError("native authentication output limit outside bound")
    # Synchronous creation avoids cancellation in asyncio's spawn/handle handoff.
    # Execution, nonblocking reads and cleanup below yield to the event loop.
    process = subprocess.Popen(  # noqa: S603, ASYNC220
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=subscription_only_environment(provider),
        start_new_session=True,
        close_fds=True,
        bufsize=0,
    )
    try:
        stream = process.stdout
        if stream is not None:
            os.set_blocking(stream.fileno(), False)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        output = bytearray()
        eof = stream is None
        while True:
            if asyncio.get_running_loop().time() >= deadline:
                return AuthProbeResult(None, failure="TIMED_OUT")
            if stream is not None and not eof:
                try:
                    chunk = os.read(stream.fileno(), min(8192, max_stdout_bytes + 1 - len(output)))
                except BlockingIOError:
                    chunk = None
                if chunk == b"":
                    eof = True
                elif chunk:
                    output.extend(chunk)
                    if len(output) > max_stdout_bytes:
                        return AuthProbeResult(None, failure="OUTPUT_LIMIT")
            if _exited(process) and eof:
                # WNOWAIT retains ownership; cleanup reaps and supplies returncode.
                break
            await asyncio.sleep(_POLL_SECONDS)
    finally:
        try:
            if process.stdout is not None:
                process.stdout.close()
        finally:
            await _join_cleanup(process)
    return AuthProbeResult(process.returncode, bytes(output) if process.returncode == 0 else b"")
