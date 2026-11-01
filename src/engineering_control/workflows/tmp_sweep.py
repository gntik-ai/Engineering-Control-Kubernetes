"""NEW-226: the backstop sweep of a role worker's private /tmp.

The workers run with PrivateTmp=true: /tmp is a tmpfs of their own, held in RAM and
charged to the worker, with no swap. Provider runs now get a TMPDIR of their own on disk
(adapters.run_temp), but tools that hardcode /tmp, or processes killed mid-write, still
leave files there. On worker start and every interval, the top-level /tmp entries are
removed that nobody wrote for the idle time and that no running step wrote:

- "wrote" is the newest ctime in the entry, which no tool can set back (tar and pnpm
  restore old mtimes; ctime is the time of that restore);
- on an idle worker, the pass removes what nobody wrote for the idle time, and stops
  before the next removal once a step has started (a walk cannot prove that a running
  step will not write into what it has already visited);
- while steps run (the claude worker is rarely idle since provider rotation), it removes
  only directories nobody wrote for the shorter busy idle time nor since the oldest
  running step started: only steps that have ended, whose processes are gone, wrote
  them. Each is renamed out of reach before its last check and goes back (never over
  a new entry of that name) if anything was written in it; a step that starts during
  the pass stops it. Files wait for an idle worker. 2026-11-01: a Claude
  checker left 5.1 GB under /tmp/rv121 (CARGO_TARGET_DIR) on a worker that was never
  idle, and its memory pressure then timed out every new Claude run's
  `claude auth status` probe;
- anything that is, contains or lies inside a live run directory is kept;
- node-compile-cache stays, and so do Codex's bubblewrap mount targets: a running
  sandbox may have one mounted, and unlinking it would lift that mount.

The sweep runs only where /tmp really is a systemd PrivateTmp, never on a shared /tmp.
"""

from __future__ import annotations

import ctypes
import itertools
import logging
import os
import re
import secrets
import stat
import time
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from engineering_control.adapters.run_temp import remove_tree

LOGGER = logging.getLogger(__name__)
KEEP_NAMES = frozenset({"node-compile-cache", ".git", ".codex", ".agents", ".aws"})
# A busy pass renames an entry to this prefix before its last check and removal.
QUARANTINE_PREFIX = ".hermes-sweep-"
# An entry larger than this is not walked to the end: it is kept, never guessed at.
MAX_WALK_ENTRIES = 200_000
_PRIVATE_TMP_ROOT = re.compile(r"^/systemd-private-[0-9a-f]{32}-[^/]+/tmp$")
# Start times (time.time()) of the steps this worker process is running.
_RUNNING: dict[int, float] = {}
_TOKENS = itertools.count()
# Incremented by every step start: a sweep pass stops when it changes.
_STARTED = [0]


@contextmanager
def running_step() -> Iterator[None]:
    """Mark one activity as running for the sweep's "written by a running step" rule."""
    token = next(_TOKENS)
    _RUNNING[token] = time.time()
    _STARTED[0] += 1
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


def _newest_change(path: Path, device: int, budget: int, *, renamed: bool = False) -> float | None:
    """The newest ctime in ``path``, never following a symlink or leaving the file system.
    For an entry the sweep just ``renamed``, its own ctime (which the rename set) is left
    out (busy passes rename directories only); its mtime still shows an entry created or
    removed in it.

    None (keep it) when the walk is cut short: too many entries, an unreadable
    directory, a mount point, or an entry that changed under the walk."""
    try:
        info = path.lstat()
    except OSError:
        return None
    if info.st_dev != device:
        return None
    newest = info.st_mtime if renamed else max(info.st_ctime, info.st_mtime)
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
    protected: Collection[Path],
    busy_idle_seconds: float | None = None,
) -> SweepResult:
    """Remove the stale entries of ``root``: on an idle worker, those idle for
    ``idle_seconds``; while steps run, only with ``busy_idle_seconds``, those idle for it
    and not written since the oldest running step started."""
    removed = failed = 0
    started = _STARTED[0]
    if _RUNNING:
        if busy_idle_seconds is None:
            return SweepResult(removed, failed)
        return _sweep_finished_steps(root, idle_seconds=busy_idle_seconds, protected=protected)
    device = root.lstat().st_dev
    for entry in stale_entries(
        root, now=time.time(), idle_seconds=idle_seconds, protected=protected
    ):
        if _RUNNING or _STARTED[0] != started:
            break
        # Revalidated right before its removal: the selection pass may be minutes old.
        newest = _newest_change(entry, device, MAX_WALK_ENTRIES)
        if newest is None or newest >= time.time() - idle_seconds:
            continue
        if _RUNNING or _STARTED[0] != started:  # a step started during that walk
            break
        if remove_tree(entry):
            removed += 1
        else:
            failed += 1
    return SweepResult(removed, failed)


_RENAME_NOREPLACE = 1
_AT_FDCWD = -100
_LIBC = ctypes.CDLL(None, use_errno=True)


def _rename_noreplace(source: Path, target: Path) -> bool:
    """rename(2) that never replaces ``target``: renameat2(RENAME_NOREPLACE)."""
    result = _LIBC.renameat2(
        _AT_FDCWD, os.fsencode(source), _AT_FDCWD, os.fsencode(target), _RENAME_NOREPLACE
    )
    return bool(result == 0)


def _sweep_finished_steps(
    root: Path, *, idle_seconds: float, protected: Collection[Path]
) -> SweepResult:
    """While steps run: the entries nobody wrote for ``idle_seconds`` nor since the oldest
    running step started, so only finished steps (whose processes are gone) wrote them."""
    removed = failed = 0
    started = _STARTED[0]
    since = oldest_running_step()
    device = root.lstat().st_dev
    for entry in stale_entries(
        root,
        now=time.time(),
        idle_seconds=idle_seconds,
        running_since=since,
        protected=protected,
    ):
        if _STARTED[0] != started:
            break
        # Directories only: a rewritten file can carry an old mtime (cp -p, tar) and the
        # rename below sets the ctime that would show it. Files wait for an idle worker.
        try:
            info = entry.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        # Revalidated right before its removal against the same cutoff.
        newest = _newest_change(entry, device, MAX_WALK_ENTRIES)
        cutoff = time.time() - idle_seconds
        if since is not None:
            cutoff = min(cutoff, since)
        if newest is None or newest >= cutoff:
            continue
        if _STARTED[0] != started:  # a step started during that walk
            break
        # Out of reach by path before the last check: from here on a step that opens it
        # gets ENOENT instead of writing into a tree being removed, and a write made
        # before the rename, even to a file the walk above had already visited, shows
        # in this walk; the entry then goes back under its name.
        quarantine = root / f"{QUARANTINE_PREFIX}{secrets.token_hex(8)}"
        if not _rename_noreplace(entry, quarantine):
            continue
        # The renamed entry must still be the directory checked above (a step may have
        # replaced it by then); only its own ctime is left out of the walk.
        try:
            moved = quarantine.lstat()
        except OSError:
            continue
        same = stat.S_ISDIR(moved.st_mode) and (moved.st_dev, moved.st_ino) == (
            info.st_dev,
            info.st_ino,
        )
        newest = (
            _newest_change(quarantine, device, MAX_WALK_ENTRIES, renamed=True) if same else None
        )
        if newest is None or newest >= cutoff:
            if not _rename_noreplace(quarantine, entry):
                # Its name was taken meanwhile: kept, never merged over what is there.
                LOGGER.warning("private /tmp sweep kept %s as %s", entry.name, quarantine.name)
            continue
        if remove_tree(quarantine):
            removed += 1
        else:
            failed += 1
    return SweepResult(removed, failed)
