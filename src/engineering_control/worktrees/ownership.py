"""NEW-243 phase 0: cross-runner ownership of a ChangeSet worktree.

A ChangeSet worktree is created by the manager (scan-runner) and shared through POSIX ACLs:
the manager rwX, the maker's runner rwX, the checker's runner r-X, the publishing
identities r-X (worktrees/manager.py ``_grant_access``). Two kernel rules break that
sharing when another provider takes over a worktree:

1. A file's ACL mask is cut by the mode its creator asked for: a tool that creates a
   directory 0755 or a file 0600/0644 leaves a mask of r-x/---/r--, and every named entry
   (the manager's write, the checker's read) is masked with it.
2. Only a file's owner (or root) may change its mode or ACL. The manager cannot repair
   files a runner created; the runner that created them can.

So the runner that wrote a worktree restores the mask on what it owns when its step ends
(:func:`normalise_owned_tree`, called by the provider activity after every writable run),
which keeps it readable for the checker and the publishing identities and removable by the
manager's cleanup. Nothing here hands a worktree to another writer: NEW-243 keeps the maker
provider fixed per ChangeSet (see the design), so a worktree has one writing runner.

The mask is the group-class permission bits of a file with an ACL; restoring it never adds
an ACL entry. Without an ACL the same bits are the owning group's, which for a runner is
its private group (no other member). The "other" class is never widened, and any other
write bit is removed: nothing here makes a path world-writable.

Every walk here is descriptor-relative (:func:`walk`): each component is opened with
O_PATH|O_NOFOLLOW relative to its parent's descriptor and checked with fstat against the
entry that was listed, and a change goes through that descriptor (``/proc/self/fd``), never
through a path that could be swapped for a symlink in between. Entries on another
filesystem are neither changed nor entered. Directories are enumerated incrementally and
the budget (entries, deadline, stop event) is checked for every entry listed; a walk that
skipped anything (an unlistable directory, a raced entry, an error, the budget) reports
``complete=False``.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

WORKTREE_OWNERSHIP_EVENT = "hermes.worktree_ownership"
_PATH_FLAGS = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
_LIST_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC

Parts = tuple[str, ...]


@dataclass
class Budget:
    """How much one walk may do: entries, a monotonic deadline, an external stop."""

    max_entries: int = 1_000_000
    deadline: float | None = None
    stop: threading.Event | None = None
    seen: int = 0
    reason: str | None = None

    @classmethod
    def seconds(
        cls, seconds: float, *, max_entries: int = 1_000_000, stop: threading.Event | None = None
    ) -> Budget:
        return cls(max_entries=max_entries, deadline=time.monotonic() + seconds, stop=stop)

    def spend(self) -> bool:
        if self.stop is not None and self.stop.is_set():
            self.reason = "stopped"
        elif self.deadline is not None and time.monotonic() >= self.deadline:
            self.reason = "deadline"
        elif self.seen >= self.max_entries:
            self.reason = "entries"
        else:
            self.seen += 1
            return True
        return False


@dataclass
class WalkReport:
    entries: int = 0
    # Entries that changed between listing and opening (swapped, replaced).
    raced: int = 0
    # Entries on another filesystem (a mount inside the tree): never changed or entered.
    foreign_device: list[Parts] = field(default_factory=list)
    # Directories this identity could not list.
    unlisted: list[Parts] = field(default_factory=list)
    # Entries that are a mount point (another mount id than the root's, e.g. a bind mount
    # of the same filesystem): never changed or entered.
    mount_points: list[Parts] = field(default_factory=list)
    # Entries whose mount id could not be read (fail closed: skipped).
    mount_unknown: int = 0
    # Entries that could not be inspected or opened for another reason.
    errors: int = 0
    stopped: str | None = None

    @property
    def complete(self) -> bool:
        """True only when every entry below the root was visited: nothing unlisted,
        raced, errored, stopped by the budget, or skipped on another filesystem or mount."""
        return self.reason is None

    @property
    def reason(self) -> str | None:
        if self.stopped is not None:
            return self.stopped
        if self.unlisted:
            return "unlisted"
        if self.foreign_device:
            return "foreign_device"
        if self.mount_points:
            return "mount_point"
        if self.mount_unknown:
            return "mount_info_unavailable"
        if self.raced:
            return "raced"
        if self.errors:
            return "error"
        return None


Visitor = Callable[[int, os.stat_result, Parts, os.stat_result | None], None]


def open_directory_strict(path: Path) -> int:
    """An O_PATH descriptor of ``path``, opened component by component from ``/`` with
    O_NOFOLLOW: any symlink on the way (an intermediate one included) is refused."""
    if not path.is_absolute():
        raise ValueError("a strict open needs an absolute path")
    descriptor = os.open("/", _PATH_FLAGS | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part in {"", ".", ".."}:
                raise ValueError(f"refusing path component {part!r}")
            child = os.open(part, _PATH_FLAGS | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def open_child_strict(parent: int, name: str) -> int:
    """An O_PATH descriptor of the directory ``name`` in ``parent`` (never a symlink)."""
    if "/" in name or name in {"", ".", ".."}:
        raise ValueError(f"refusing path component {name!r}")
    return os.open(name, _PATH_FLAGS | os.O_DIRECTORY, dir_fd=parent)


def mount_id(descriptor: int) -> int | None:
    """The mount id of an open descriptor (``mnt_id`` in /proc/self/fdinfo), or None when
    it cannot be read. Two entries of one filesystem in different mounts (a bind mount)
    share st_dev but not the mount id."""
    try:
        with open(f"/proc/self/fdinfo/{descriptor}", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("mnt_id:"):
                    return int(line.split(":", 1)[1])
    except (OSError, ValueError):
        return None
    return None


def fd_path(descriptor: int) -> str:
    """The /proc path of an open descriptor: syscalls on it act on that exact inode."""
    return f"/proc/self/fd/{descriptor}"


@dataclass
class _Level:
    """One directory being enumerated: its O_PATH descriptor, the descriptor its listing
    reads, and the live scandir iterator (entries are read as they are needed)."""

    directory: int
    listing: int
    entries: Iterator[os.DirEntry[str]]
    close_entries: Callable[[], None]
    parts: Parts
    info: os.stat_result


def _enter(directory: int, parts: Parts, info: os.stat_result) -> _Level:
    listing = os.open(".", _LIST_FLAGS, dir_fd=directory)
    try:
        iterator = os.scandir(listing)
    except BaseException:
        os.close(listing)
        raise
    return _Level(directory, listing, iterator, iterator.close, parts, info)


def walk(
    root: int,
    budget: Budget,
    visit: Visitor,
    *,
    prune: Callable[[Parts], bool] | None = None,
) -> WalkReport:
    """Visit ``root`` (an O_PATH directory descriptor the caller owns) and every directory
    and regular file below it, depth first, never through a symlink and never onto another
    filesystem. ``visit(fd, info, parts, parent_info)`` gets an O_PATH descriptor that stays
    open only during the call; ``prune(parts)`` keeps a directory from being entered.
    Directories are read incrementally; the budget is checked for every entry read."""
    report = WalkReport()
    root_info = os.fstat(root)
    if not stat.S_ISDIR(root_info.st_mode):
        raise NotADirectoryError("walk root is not a directory")
    root_mount = mount_id(root)
    if root_mount is None:
        # Without mount identities no mount crossing can be excluded: change nothing.
        report.stopped = "mount_info_unavailable"
        return report
    visit(root, root_info, (), None)
    report.entries += 1
    try:
        stack = [_enter(root, (), root_info)]
    except OSError:
        report.unlisted.append(())
        return report
    try:
        while stack:
            level = stack[-1]
            if not budget.spend():
                report.stopped = budget.reason
                return report
            try:
                entry = next(level.entries)
            except StopIteration:
                stack.pop()
                _leave(level, root)
                continue
            except OSError:
                report.errors += 1
                stack.pop()
                _leave(level, root)
                continue
            descriptor = _open_entry(level, entry.name, root_info, root_mount, report)
            if descriptor is None:
                continue
            keep = False
            try:
                info = os.fstat(descriptor)
                child = (*level.parts, entry.name)
                visit(descriptor, info, child, level.info)
                report.entries += 1
                if stat.S_ISDIR(info.st_mode) and not (prune is not None and prune(child)):
                    try:
                        stack.append(_enter(descriptor, child, info))
                    except OSError:
                        report.unlisted.append(child)
                        continue
                    keep = True
            finally:
                if not keep:
                    os.close(descriptor)
    finally:
        for level in stack:
            _leave(level, root)
    return report


def _leave(level: _Level, root: int) -> None:
    level.close_entries()
    os.close(level.listing)
    if level.directory != root:
        os.close(level.directory)


def _open_entry(
    level: _Level, name: str, root_info: os.stat_result, root_mount: int, report: WalkReport
) -> int | None:
    """An O_PATH descriptor of the directory or regular file ``name`` once it is proven to
    be the entry that was listed (same inode, type and filesystem) in the root's mount;
    None to skip it."""
    try:
        listed = os.stat(name, dir_fd=level.directory, follow_symlinks=False)
    except FileNotFoundError:
        return None  # removed meanwhile: nothing left to visit
    except OSError:
        report.errors += 1
        return None
    if not (stat.S_ISDIR(listed.st_mode) or stat.S_ISREG(listed.st_mode)):
        return None  # symlinks, sockets, FIFOs and devices are never touched
    if listed.st_dev != root_info.st_dev:
        report.foreign_device.append((*level.parts, name))
        return None
    try:
        descriptor = os.open(name, _PATH_FLAGS, dir_fd=level.directory)
    except FileNotFoundError:
        return None
    except OSError:
        report.raced += 1  # e.g. ELOOP: swapped for a symlink after listing
        return None
    info = os.fstat(descriptor)
    if (
        (info.st_dev, info.st_ino) != (listed.st_dev, listed.st_ino)
        or stat.S_IFMT(info.st_mode) != stat.S_IFMT(listed.st_mode)
        or info.st_dev != root_info.st_dev
    ):
        os.close(descriptor)
        report.raced += 1
        return None
    entry_mount = mount_id(descriptor)
    if entry_mount != root_mount:
        os.close(descriptor)
        if entry_mount is None:
            report.mount_unknown += 1
        else:
            report.mount_points.append((*level.parts, name))
        return None
    return descriptor


def normalised_mode(mode: int) -> int:
    """The permission bits a runner leaves on a worktree entry it owns.

    Group class (the ACL mask) gets read and write, and execute for a directory or a file
    its owner may execute; "other" loses write. Set-id and sticky bits are kept as they
    are (the units run with RestrictSUIDSGID=true, so a runner cannot create set-id files).
    """
    bits = stat.S_IMODE(mode)
    bits |= stat.S_IRGRP | stat.S_IWGRP
    if stat.S_ISDIR(mode) or bits & stat.S_IXUSR:
        bits |= stat.S_IXGRP
    return bits & ~stat.S_IWOTH


@dataclass(frozen=True)
class NormalisationReport:
    scanned: int
    changed: int
    failed: int
    raced: int = 0
    unlisted: int = 0
    foreign_device: int = 0
    mount_points: int = 0
    errors: int = 0
    complete: bool = True
    reason: str | None = None


def normalise_owned_tree(
    root: Path, *, owner: int | None = None, budget: Budget | None = None
) -> NormalisationReport:
    """Restore the ACL mask on every entry of ``root`` that ``owner`` (default: this
    process's effective uid) owns, so the worktree's named ACL entries apply again.

    Entries owned by anyone else are left alone (the kernel would refuse anyway). Errors on
    single entries are counted, never raised. The report is ``complete`` only when every
    entry was visited and every change succeeded."""
    uid = os.geteuid() if owner is None else owner
    changed = failed = 0

    def visit(descriptor: int, info: os.stat_result, _: Parts, __: object) -> None:
        nonlocal changed, failed
        if info.st_uid != uid:
            return
        wanted = normalised_mode(info.st_mode)
        if wanted == stat.S_IMODE(info.st_mode):
            return
        try:
            os.chmod(fd_path(descriptor), wanted)
        except OSError:
            failed += 1
            return
        changed += 1

    descriptor = open_directory_strict(root)
    try:
        report = walk(descriptor, budget or Budget(), visit)
    finally:
        os.close(descriptor)
    reason = report.reason or ("failed" if failed else None)
    return NormalisationReport(
        scanned=report.entries,
        changed=changed,
        failed=failed,
        raced=report.raced,
        unlisted=len(report.unlisted),
        foreign_device=len(report.foreign_device),
        mount_points=len(report.mount_points) + report.mount_unknown,
        errors=report.errors,
        complete=report.complete and not failed,
        reason=reason,
    )
