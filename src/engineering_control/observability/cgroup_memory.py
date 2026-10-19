"""NEW-226: the provider worker's memory state, recorded when a provider step fails.

On 2026-10-19 two makers sat at 75-90% "full" memory pressure for hours and each only
showed a 2-hour timeout. A failed or timed-out step now records, in its own events, the
worker cgroup's memory.pressure, memory.current and limits, and how long the step spent
fully stalled on memory; past a threshold it reads "memory-throttled".

The worker cgroup is the unit's: with DelegateSubgroup=supervisor the worker runs in
"supervisor" and its runs beside it, so the parent holds MemoryHigh/MemoryMax and the
pressure of everything the worker runs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROC_CGROUP = Path("/proc/self/cgroup")
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_DELEGATED_SUBGROUP = "supervisor"
# "full" stall share of the step, or the last minute's "full" average, from which the
# step reads memory-throttled: every runnable task stalled on memory a tenth of the time.
THROTTLED_FULL_SHARE = 0.10
THROTTLED_FULL_AVG60 = 10.0
EVENT_TYPE = "hermes.memory_pressure"


@dataclass(frozen=True)
class MemorySnapshot:
    taken_at: float
    current: int | None
    high: int | None
    maximum: int | None
    some: dict[str, float]
    full: dict[str, float]
    oom_kills: int | None


def worker_cgroup(
    proc_cgroup: Path = _PROC_CGROUP, cgroup_root: Path = _CGROUP_ROOT
) -> Path | None:
    try:
        lines = proc_cgroup.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("0::"):
            own = cgroup_root / line[3:].strip().lstrip("/")
            return own.parent if own.name == _DELEGATED_SUBGROUP else own
    return None


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _bytes(text: str | None) -> int | None:
    value = (text or "").strip()
    return int(value) if value.isdigit() else None


def parse_pressure(text: str) -> dict[str, dict[str, float]]:
    """``some``/``full`` -> avg10, avg60, avg300, total (microseconds) of memory.pressure."""
    parsed: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        kind, _, rest = line.partition(" ")
        if kind not in {"some", "full"}:
            continue
        values: dict[str, float] = {}
        for field in rest.split():
            key, _, raw = field.partition("=")
            try:
                values[key] = float(raw)
            except ValueError:
                continue
        parsed[kind] = values
    return parsed


def _oom_kills(text: str | None) -> int | None:
    for line in (text or "").splitlines():
        key, _, value = line.partition(" ")
        if key == "oom_kill" and value.strip().isdigit():
            return int(value)
    return None


def memory_snapshot(cgroup: Path | None = None) -> MemorySnapshot | None:
    """Read the worker cgroup's memory state; None where there is no cgroup v2 to read."""
    cgroup = cgroup if cgroup is not None else worker_cgroup()
    if cgroup is None:
        return None
    pressure_text = _read(cgroup / "memory.pressure")
    current = _bytes(_read(cgroup / "memory.current"))
    if pressure_text is None and current is None:
        return None
    pressure = parse_pressure(pressure_text or "")
    return MemorySnapshot(
        taken_at=time.monotonic(),
        current=current,
        high=_bytes(_read(cgroup / "memory.high")),
        maximum=_bytes(_read(cgroup / "memory.max")),
        some=pressure.get("some", {}),
        full=pressure.get("full", {}),
        oom_kills=_oom_kills(_read(cgroup / "memory.events")),
    )


def _gib(value: int | None) -> str:
    return "unlimited" if value is None else f"{value / (1 << 30):.1f} GiB"


def memory_pressure_event(
    start: MemorySnapshot | None, end: MemorySnapshot | None
) -> dict[str, Any] | None:
    """A bounded event for the step's transcript; None when nothing could be read."""
    if end is None:
        return None
    event: dict[str, Any] = {
        "type": EVENT_TYPE,
        "memory_current_bytes": end.current,
        "memory_high_bytes": end.high,
        "memory_max_bytes": end.maximum,
        **{f"some_{key}": end.some.get(key) for key in ("avg10", "avg60")},
        **{f"full_{key}": end.full.get(key) for key in ("avg10", "avg60")},
    }
    share: float | None = None
    if start is not None:
        elapsed = end.taken_at - start.taken_at
        stalled = end.full.get("total", 0.0) - start.full.get("total", 0.0)
        if elapsed > 0 and "total" in start.full and "total" in end.full:
            share = min(1.0, max(0.0, stalled / 1_000_000 / elapsed))
            event["full_stall_seconds"] = round(stalled / 1_000_000, 1)
            event["full_stall_share"] = round(share, 3)
        if start.oom_kills is not None and end.oom_kills is not None:
            event["oom_kills"] = max(0, end.oom_kills - start.oom_kills)
    throttled = (share is not None and share >= THROTTLED_FULL_SHARE) or (
        end.full.get("avg60", 0.0) >= THROTTLED_FULL_AVG60
    )
    event["assessment"] = "memory-throttled" if throttled else "memory-ok"
    stall = f"; fully stalled {share:.0%} of the step" if share is not None else ""
    kills = f"; {event['oom_kills']} OOM kill(s)" if event.get("oom_kills") else ""
    event["summary"] = (
        f"Worker memory: {event['assessment']} (full avg10 {end.full.get('avg10', 0.0):.1f}% "
        f"avg60 {end.full.get('avg60', 0.0):.1f}%, some avg60 {end.some.get('avg60', 0.0):.1f}%"
        f"{stall}{kills}; {_gib(end.current)} in use, high {_gib(end.high)}, "
        f"max {_gib(end.maximum)})"
    )
    return event
