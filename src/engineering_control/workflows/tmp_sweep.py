"""NEW-226: the backstop sweep of a role worker's private /tmp.

The workers run with PrivateTmp=true: /tmp is a tmpfs of their own, held in RAM and
charged to the worker, with no swap. Provider runs now get a TMPDIR of their own on disk
(adapters.run_temp), but tools that hardcode /tmp, or processes killed mid-write, still
leave files there. On worker start and every interval, the top-level /tmp entries are
removed that nobody wrote for the idle time and that no running step wrote:

- "wrote" is the newest ctime in the entry, which no tool can set back (tar and pnpm
  restore old mtimes; ctime is the time of that restore);
- an entry changed after the oldest running step started is kept, as is anything that
  is, contains or lies inside a live run directory;
- node-compile-cache stays, and so do Codex's bubblewrap mount targets: a running
  sandbox may have one mounted, and unlinking it would lift that mount.

The sweep runs only where /tmp really is a systemd PrivateTmp, never on a shared /tmp.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import stat
import time
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from engineering_control.adapters.run_temp import remove_tree

LOGGER = logging.getLogger(__name__)
KEEP_NAMES = frozenset({"node-compile-cache", ".git", ".codex", ".agents", ".aws"})
# An entry larger than this is not walked to the end: it is kept, never guessed at.
MAX_WALK_ENTRIES = 200_000
_PRIVATE_TMP_ROOT = re.compile(r"^/systemd-private-[0-9a-f]{32}-[^/]+/tmp$")
# Start times (time.time()) of the steps this worker process is running.
_RUNNING: dict[int, float] = {}
_TOKENS = itertools.count()


@contextmanager
def running_step() -> Iterator[None]:
    """Mark one activity as running for the sweep's "written by a running step" rule."""
    token = next(_TOKENS)
    _RUNNING[token] = time.time()
    try:
        yield
    finally:
        _RUNNING.pop(token, None)


def oldest_running_step() -> float | None:
    return min(_RUNNING.values(), default=None)


def is_private_tmp(mountinfo: Path = Path("/proc/self/mountinfo")) -> bool:
    """Whether /tmp is a systemd PrivateTmp bind (its topmost mount)."""
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError:
        return False
    root = None
    for line in lines:
        fields = line.split()
        if len(fields) > 4 and fields[4] == "/tmp":  # noqa: S108 - a mount point name
            root = fields[3]
    return root is not None and bool(_PRIVATE_TMP_ROOT.match(root))


def _overlaps(entry: Path, protected: Path) -> bool:
    return entry == protected or protected.is_relative_to(entry) or entry.is_relative_to(protected)


def _newest_change(path: Path, device: int, budget: int) -> float | None:
    """The newest ctime in ``path``, never following a symlink or leaving the file system.

    None (keep it) when the walk is cut short: too many entries, an unreadable
    directory, a mount point, or an entry that changed under the walk."""
    try:
        info = path.lstat()
    except OSError:
        return None
    if info.st_dev != device:
        return None
    newest = max(info.st_ctime, info.st_mtime)
    pending = [path] if stat.S_ISDIR(info.st_mode) else []
    seen = 1
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    seen += 1
                    if seen > budget:
                        return None
                    child = entry.stat(follow_symlinks=False)
                    if child.st_dev != device:
                        return None
                    newest = max(newest, child.st_ctime, child.st_mtime)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
        except OSError:
            return None
    return newest


def stale_entries(
    root: Path,
    *,
    now: float,
    idle_seconds: float,
    running_since: float | None = None,
    protected: Collection[Path] = (),
    max_entries: int = MAX_WALK_ENTRIES,
) -> list[Path]:
    """The top-level entries of ``root`` the sweep removes, in name order."""
    try:
        device = root.lstat().st_dev
        names = sorted(os.listdir(root))
    except OSError:
        return []
    cutoff = now - idle_seconds
    if running_since is not None:
        cutoff = min(cutoff, running_since)
    selected: list[Path] = []
    for name in names:
        entry = root / name
        if name in KEEP_NAMES or any(_overlaps(entry, path) for path in protected):
            continue
        newest = _newest_change(entry, device, max_entries)
        if newest is not None and newest < cutoff:
            selected.append(entry)
    return selected


@dataclass(frozen=True)
class SweepResult:
    removed: int
    failed: int


def sweep(
    root: Path,
    *,
    idle_seconds: float,
    running_since: float | None,
    protected: Collection[Path],
) -> SweepResult:
    removed = failed = 0
    device = root.lstat().st_dev
    for entry in stale_entries(
        root,
        now=time.time(),
        idle_seconds=idle_seconds,
        running_since=running_since,
        protected=protected,
    ):
        # Revalidated right before its removal: the selection pass may be minutes old,
        # and a step may have started (or written into the entry) since.
        started = oldest_running_step()
        cutoff = time.time() - idle_seconds
        if running_since is not None or started is not None:
            cutoff = min(cutoff, *(t for t in (running_since, started) if t is not None))
        newest = _newest_change(entry, device, MAX_WALK_ENTRIES)
        if newest is None or newest >= cutoff:
            continue
        if remove_tree(entry):
            removed += 1
        else:
            failed += 1
    return SweepResult(removed, failed)
