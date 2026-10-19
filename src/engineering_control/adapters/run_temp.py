"""NEW-226: a private temporary directory for each provider run.

The agent workers run with PrivateTmp=true, a tmpfs: whatever a finished run's tools left
in /tmp stayed in RAM, charged to the worker, with no swap to page it out (2026-10-19:
claude 1.64 GB, codex 0.97 GB; memory pressure "full" 75-90% until 689 entries were
deleted by hand). Each run now gets TMPDIR/TMP/TEMP of its own on disk, removed when the
run ends, however it ends. The agent CLI and everything it starts inherit it: Codex makes
$TMPDIR a writable root of its workspace-write sandbox (and keeps its bubblewrap
mount-target registry there), Claude Code keeps its own per-uid temp directory and the
Bash tool's output files under it.

Runs of one worker share a uid, so file modes alone cannot keep them apart: the run
directories sit in a parent the runner may enter but not list (0300), under names nobody
can guess. Inside the Codex sandbox that parent is read-only (it cannot be opened up)
and /proc shows only the run's own processes, so one Codex run cannot find another's.
A Claude run has no such sandbox: like every unsandboxed process of the uid, it could
chmod the parent or read a sibling's environment in /proc, exactly as it can already
read the other runs' worktrees. Separating those needs a uid or sandbox per run.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import stat
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from pathlib import Path

LOGGER = logging.getLogger(__name__)
RUN_TEMP_PARENT = "run-tmp"
# Short on purpose: sockets created under TMPDIR (Chromium's SingletonSocket, tsx and
# Claude Code IPC) must fit AF_UNIX's 108 bytes.
_NAME_BYTES = 8
# Run directories of this worker process that exist now: no sweep ever touches them.
_LIVE: set[Path] = set()
# Run directories whose removal failed (a process that could not be stopped still wrote
# in it): the periodic sweep retries them.
_UNREMOVED: set[Path] = set()
# Removals still running in their threads.
_REMOVALS: set[threading.Thread] = set()


def run_temp_environment(directory: Path) -> dict[str, str]:
    """The variables POSIX tools, Python, Node.js and Go read their temp directory from."""
    value = str(directory)
    return {"TMPDIR": value, "TMP": value, "TEMP": value}


def _parent(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("provider temporary root must be a real directory")
    parent = root / RUN_TEMP_PARENT
    with suppress(FileExistsError):
        parent.mkdir(mode=0o300)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("run temporary parent must be a directory this runner owns")
    if stat.S_IMODE(info.st_mode) != 0o300:
        os.chmod(parent, 0o300)
    return parent


@contextmanager
def run_temporary_directory(root: Path) -> Iterator[Path]:
    """A new 0700 directory under ``root``/run-tmp, removed when the block exits."""
    parent = _parent(root)
    while True:
        path = parent / secrets.token_hex(_NAME_BYTES)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            continue
        break
    _LIVE.add(path)
    try:
        yield path
    finally:
        # Started even when the activity is cancelled; a thread, so a large tree never
        # holds the worker's event loop (the other run's output, heartbeats). The path
        # stays live until it is gone; a worker stopped meanwhile clears it at start.
        removal = threading.Thread(target=_remove_run_directory, args=(path,), daemon=True)
        _REMOVALS.add(removal)
        removal.start()


def _remove_run_directory(path: Path) -> None:
    try:
        if not remove_tree(path):
            _UNREMOVED.add(path)
            LOGGER.warning("run temporary directory not removed; the sweep retries it")
    finally:
        _LIVE.discard(path)
        _REMOVALS.discard(threading.current_thread())


def wait_for_removals(timeout: float = 30.0) -> None:
    """Join the removals started so far (worker shutdown, tests)."""
    for removal in list(_REMOVALS.copy()):
        removal.join(timeout)


def optional_run_temporary_directory(root: str | None) -> AbstractContextManager[Path | None]:
    """A run directory under ``root``; without a root (a bare adapter) none, as before."""
    return run_temporary_directory(Path(root)) if root else nullcontext(None)


def live_run_temporaries() -> frozenset[Path]:
    return frozenset(_LIVE)


def retry_unremoved() -> int:
    """Remove run directories an earlier removal left behind; returns how many went."""
    removed = 0
    for path in sorted(_UNREMOVED.copy()):  # the sweep runs in a thread
        if remove_tree(path):
            _UNREMOVED.discard(path)
            removed += 1
    return removed


def clear_run_temporaries(root: Path) -> int:
    """At worker start: remove every run directory a stopped or killed worker left.

    Only this worker process uses the parent, and it has no run yet, so nothing in it is
    live. Listing needs read permission, which the parent gets only for this moment."""
    parent = root / RUN_TEMP_PARENT
    try:
        info = parent.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("run temporary parent must be a directory this runner owns")
    removed = 0
    os.chmod(parent, 0o700)
    try:
        for name in sorted(os.listdir(parent)):
            path = parent / name
            if path not in _LIVE and remove_tree(path):
                removed += 1
    finally:
        os.chmod(parent, 0o300)
    return removed


def remove_tree(path: Path) -> bool:
    """Remove a file or directory tree this runner owns, never following a symlink.

    A second pass first restores the owner's access to directories a tool made
    read-only or unreadable (Go module copies, chmod'ed test fixtures)."""
    for attempt in range(2):
        with suppress(OSError):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        if not os.path.lexists(path):
            return True
        if attempt == 0:
            _restore_owner_access(path)
    return False


def _restore_owner_access(path: Path) -> None:
    """Give the owner rwx on every directory of the tree, through descriptors only.

    Each directory is opened with O_NOFOLLOW (O_PATH works without any permission) and
    changed through its /proc/self/fd link, so a directory swapped for a symlink
    meanwhile fails to open instead of redirecting the chmod outside the tree."""
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        pending = [os.open(path, flags)]
    except OSError:
        return
    while pending:
        handle = pending.pop()
        try:
            with suppress(OSError):
                os.chmod(f"/proc/self/fd/{handle}", 0o700)
            try:
                listing = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=handle)
            except OSError:
                continue
            try:
                with os.scandir(listing) as entries:
                    names = [entry.name for entry in entries if entry.is_dir(follow_symlinks=False)]
            finally:
                os.close(listing)
            for name in names:
                with suppress(OSError):
                    pending.append(os.open(name, flags, dir_fd=handle))
        finally:
            os.close(handle)
