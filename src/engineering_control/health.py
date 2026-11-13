"""A process's own account of itself: whether its loops still turn, and its numbers.

Every process of the platform listens on one more port, for the cluster alone:

``/healthz``
    200 while every loop the process has named turned within its time, 503 with the
    names of the ones that did not. The kubelet restarts a process whose loop stands.

``/metrics``
    The same, and what the process counts, in the text format Prometheus reads.

The listener has no route from outside and is in no Service. It answers GET and nothing
else, reads nothing but the request line, and says nothing that is not a number.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from typing import Final

import structlog

LOG = structlog.get_logger(__name__)
REQUEST_BYTES: Final = 4096
REQUEST_SECONDS: Final = 5.0
NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,60}$")
CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:120]


def sample(name: str, value: float | int, **labels: str) -> str:
    """One line of the text format. Names are the platform's own; values are numbers."""
    if not NAME.fullmatch(name):
        raise ValueError(f"not a metric name: {name}")
    shown = ",".join(f'{key}="{_label(str(labels[key]))}"' for key in sorted(labels))
    number = repr(float(value)) if isinstance(value, float) else str(int(value))
    return f"{name}{{{shown}}} {number}" if shown else f"{name} {number}"


class Health:
    """What one process knows about itself. Not shared between processes or threads'
    writers: a loop beats from the event loop it runs on."""

    def __init__(self, process: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.process = process
        self._clock = clock
        self._started = clock()
        self._loops: dict[str, tuple[float, float]] = {}
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}

    def expect(self, loop: str, *, within: float) -> None:
        """Name a loop that has to turn at least every ``within`` seconds from now on."""
        self._loops[loop] = (self._clock(), within)

    def forget(self, loop: str) -> None:
        self._loops.pop(loop, None)

    def beat(self, loop: str) -> None:
        """The loop turned. A loop that was never named is not watched."""
        known = self._loops.get(loop)
        if known is not None:
            self._loops[loop] = (self._clock(), known[1])

    def count(self, name: str, amount: int = 1, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        self._counters[key] = self._counters.get(key, 0) + amount

    def stalled(self) -> list[str]:
        now = self._clock()
        return sorted(name for name, (last, within) in self._loops.items() if now - last > within)

    def metrics(self) -> list[str]:
        now = self._clock()
        lines = [
            "# HELP engineering_process_uptime_seconds How long the process has been running.",
            "# TYPE engineering_process_uptime_seconds gauge",
            sample("engineering_process_uptime_seconds", now - self._started, process=self.process),
            "# HELP engineering_loop_age_seconds Seconds since a loop of the process last turned.",
            "# TYPE engineering_loop_age_seconds gauge",
        ]
        lines.extend(
            sample("engineering_loop_age_seconds", now - last, process=self.process, loop=name)
            for name, (last, _within) in sorted(self._loops.items())
        )
        lines.extend(
            [
                "# HELP engineering_loop_stalled One while a loop did not turn within its time.",
                "# TYPE engineering_loop_stalled gauge",
            ]
        )
        stalled = set(self.stalled())
        lines.extend(
            sample(
                "engineering_loop_stalled", int(name in stalled), process=self.process, loop=name
            )
            for name in sorted(self._loops)
        )
        seen: set[str] = set()
        for (name, labels), value in sorted(self._counters.items()):
            metric = f"engineering_{name}_total"
            if metric not in seen:
                seen.add(metric)
                lines.append(f"# TYPE {metric} counter")
            lines.append(sample(metric, value, process=self.process, **dict(labels)))
        return lines


Extra = Callable[[], Awaitable[list[str]]]


async def _answer(writer: asyncio.StreamWriter, status: str, body: str, kind: str) -> None:
    payload = body.encode()
    head = (
        f"HTTP/1.1 {status}\r\nContent-Type: {kind}\r\nContent-Length: {len(payload)}\r\n"
        "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
    )
    writer.write(head.encode() + payload)
    with contextlib.suppress(ConnectionError):
        await writer.drain()


def handler(
    health: Health, extra: Extra | None = None
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), REQUEST_SECONDS)
            parts = line[:REQUEST_BYTES].decode("latin-1").split()
            path = parts[1].split("?", 1)[0] if len(parts) >= 2 and parts[0] == "GET" else None
            if path == "/healthz":
                stalled = health.stalled()
                if stalled:
                    await _answer(
                        writer,
                        "503 Service Unavailable",
                        "stalled: " + ", ".join(stalled) + "\n",
                        "text/plain; charset=utf-8",
                    )
                else:
                    await _answer(writer, "200 OK", "ok\n", "text/plain; charset=utf-8")
            elif path == "/metrics":
                lines = health.metrics()
                if extra is not None:
                    try:
                        lines.extend(await extra())
                    except Exception as error:  # noqa: BLE001 - the process's own numbers stay
                        LOG.warning("metrics_incomplete", error=repr(error)[:200])
                await _answer(writer, "200 OK", "\n".join(lines) + "\n", CONTENT_TYPE)
            else:
                await _answer(writer, "404 Not Found", "not found\n", "text/plain; charset=utf-8")
        except (TimeoutError, ConnectionError, UnicodeError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    return handle


async def listen(
    health: Health, host: str, port: int, *, extra: Extra | None = None
) -> asyncio.Server:
    """Start the listener. The caller closes it when the process stops."""
    server = await asyncio.start_server(handler(health, extra), host, port, limit=REQUEST_BYTES)
    LOG.info("health_listening", process=health.process, port=port)
    return server
