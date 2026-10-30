"""NEW-243 phase 0: one-off, root-only repair of ChangeSet worktrees and their Git metadata
written before the post-step normalisation existed, so the checker and publishing
identities can read them and the cleanup (scan-runner) can remove them.

Run as root on the worker through ``scripts/normalise-worktree-ownership.sh`` (dry run by
default, ``--apply`` to change). What it does, and nothing else:

1. Every directory and regular file of each ChangeSet worktree
   (``<platform>/worktrees/<repository>/<uuid>``) gets :func:`ownership.normalised_mode`
   (the ACL mask made rwX again; "other" never widened, its write removed).
2. In each metadata repository (``<platform>/cache/scan/worktree-git/<repository>/<uuid>.git``)
   the same below ``refs/`` and ``logs/``, and below ``objects/`` (not ``objects/info``)
   read for the mask only (objects stay unwritable). The sealed parts (top level, config,
   objects/info, worktrees/) are never touched: verify_metadata refuses them group-writable.
3. Directories under ``objects/``, ``refs/heads/`` and ``logs/refs/heads/``, and reflog
   files under ``logs/refs/heads/``, that are not the manager's are given to the manager
   (scan-runner), so its cleanup can remove them.

Root follows no symlink here: every path is opened component by component with O_NOFOLLOW
(an intermediate symlink, e.g. ``logs/refs`` -> a mirror, is refused), every entry is
opened relative to its parent's descriptor and checked with fstat before it is changed
through that descriptor, and nothing on another filesystem is changed or entered.
"""

from __future__ import annotations

import argparse
import os
import pwd
import re
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from engineering_control.worktrees.ownership import (
    Budget,
    Parts,
    fd_path,
    mount_id,
    normalised_mode,
    open_child_strict,
    open_directory_strict,
    walk,
)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass
class RepairReport:
    worktrees: int = 0
    metadata: int = 0
    modes: int = 0
    owners: int = 0
    raced: int = 0
    foreign_device: int = 0
    refused: list[str] = field(default_factory=list)
    # Trees whose walk did not visit everything (unlistable directories, races, errors):
    # the repair is not complete and the run exits non-zero.
    incomplete: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def _place(descriptor: int) -> tuple[int, int | None]:
    """Filesystem and mount of a descriptor: a bind mount shares the first, not the second."""
    return os.fstat(descriptor).st_dev, mount_id(descriptor)


def _readable_mask(mode: int) -> int:
    """Objects are immutable: the mask gets read (and search for directories) only."""
    if stat.S_ISDIR(mode):
        return normalised_mode(mode)
    return (stat.S_IMODE(mode) | stat.S_IRGRP) & ~stat.S_IWOTH


def _children(parent: int) -> list[str]:
    listing = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=parent)
    try:
        with os.scandir(listing) as entries:
            return sorted(entry.name for entry in entries)
    finally:
        os.close(listing)


def _repair_tree(
    root: int,
    label: str,
    report: RepairReport,
    *,
    apply: bool,
    mode_for: Callable[[int], int],
    owner: tuple[int, int] | None,
    own: Callable[[Parts, os.stat_result], bool],
    prune: Callable[[Parts], bool] | None = None,
) -> None:
    def visit(descriptor: int, info: os.stat_result, parts: Parts, _: object) -> None:
        if prune is not None and parts and prune(parts):
            return
        shown = "/".join((label, *parts))
        wanted = mode_for(info.st_mode)
        if wanted != stat.S_IMODE(info.st_mode):
            report.modes += 1
            if apply:
                os.chmod(fd_path(descriptor), wanted)
            else:
                report.lines.append(f"would set mode {wanted:04o} on {shown}")
        if owner is not None and own(parts, info) and info.st_uid != owner[0]:
            report.owners += 1
            if apply:
                os.chown(fd_path(descriptor), *owner)
            else:
                report.lines.append(f"would give {shown} to the manager")

    result = walk(root, Budget(max_entries=50_000_000), visit, prune=prune)
    report.raced += result.raced
    report.foreign_device += len(result.foreign_device)
    if not result.complete:
        report.incomplete.append(f"{label}: {result.reason}")


def repair(platform_root: Path, manager: str, *, apply: bool) -> RepairReport:
    """Repair (or, without ``apply``, list) every ChangeSet worktree and metadata
    repository below ``platform_root``; see the module documentation."""
    account = pwd.getpwnam(manager)
    owner = (account.pw_uid, account.pw_gid)
    report = RepairReport()
    platform = open_directory_strict(platform_root)
    try:
        for kind, relative, suffix in (
            ("worktree", ("worktrees",), ""),
            ("metadata", ("cache", "scan", "worktree-git"), ".git"),
        ):
            try:
                base = platform
                opened: list[int] = []
                for part in relative:
                    base = open_child_strict(base, part)
                    opened.append(base)
            except OSError as error:
                for descriptor in opened:
                    os.close(descriptor)
                report.refused.append(f"{'/'.join(relative)}: {error.strerror}")
                continue
            try:
                _repair_kind(kind, base, "/".join(relative), suffix, report, owner, apply)
            finally:
                for descriptor in opened:
                    os.close(descriptor)
    finally:
        os.close(platform)
    return report


def _repair_kind(
    kind: str,
    base: int,
    label: str,
    suffix: str,
    report: RepairReport,
    owner: tuple[int, int],
    apply: bool,
) -> None:
    base_device = (os.fstat(base).st_dev, mount_id(base))
    if base_device[1] is None:
        report.incomplete.append(f"{label}: mount_info_unavailable")
        return
    for repository in _children(base):
        try:
            repository_fd = open_child_strict(base, repository)
        except (OSError, ValueError):
            report.refused.append(f"{label}/{repository}: not a real directory")
            continue
        try:
            if _place(repository_fd) != base_device:
                report.refused.append(f"{label}/{repository}: another filesystem or mount")
                continue
            for name in _children(repository_fd):
                identity = name.removesuffix(suffix) if suffix else name
                if (suffix and not name.endswith(suffix)) or not _UUID.fullmatch(identity):
                    continue
                shown = f"{label}/{repository}/{name}"
                try:
                    changeset = open_child_strict(repository_fd, name)
                except (OSError, ValueError):
                    report.refused.append(f"{shown}: not a real directory")
                    continue
                try:
                    if _place(changeset) != base_device:
                        report.refused.append(f"{shown}: another filesystem or mount")
                        continue
                    if kind == "worktree":
                        report.worktrees += 1
                        _repair_tree(
                            changeset,
                            shown,
                            report,
                            apply=apply,
                            mode_for=normalised_mode,
                            owner=None,
                            own=lambda parts, info: False,
                        )
                    else:
                        report.metadata += 1
                        _repair_metadata(changeset, shown, report, owner, apply, base_device)
                finally:
                    os.close(changeset)
        finally:
            os.close(repository_fd)


def _repair_metadata(
    metadata: int,
    label: str,
    report: RepairReport,
    owner: tuple[int, int],
    apply: bool,
    device: tuple[int, int | None],
) -> None:
    for sub in ("objects", "refs", "logs"):
        try:
            root = open_child_strict(metadata, sub)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            report.refused.append(f"{label}/{sub}: not a real directory")
            continue
        try:
            if _place(root) != device:
                report.refused.append(f"{label}/{sub}: another filesystem or mount")
                continue
            if sub == "objects":
                _repair_tree(
                    root,
                    f"{label}/objects",
                    report,
                    apply=apply,
                    mode_for=_readable_mask,
                    owner=owner,
                    own=lambda parts, info: stat.S_ISDIR(info.st_mode),
                    prune=lambda parts: parts[:1] == ("info",),
                )
            elif sub == "refs":
                _repair_tree(
                    root,
                    f"{label}/refs",
                    report,
                    apply=apply,
                    mode_for=normalised_mode,
                    owner=owner,
                    own=lambda parts, info: parts[:1] == ("heads",) and stat.S_ISDIR(info.st_mode),
                )
            else:
                _repair_tree(
                    root,
                    f"{label}/logs",
                    report,
                    apply=apply,
                    mode_for=normalised_mode,
                    owner=owner,
                    own=lambda parts, info: parts[:2] == ("refs", "heads") and len(parts) > 2,
                )
        finally:
            os.close(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--apply", action="store_true", help="change (default: dry run)")
    parser.add_argument(
        "--platform-root",
        type=Path,
        default=Path(os.environ.get("ENGINEERING_PLATFORM_ROOT", "/srv/engineering")),
    )
    parser.add_argument(
        "--manager", default=os.environ.get("ENGINEERING_WORKTREE_MANAGER", "scan-runner")
    )
    arguments = parser.parse_args(argv)
    if arguments.apply and os.geteuid() != 0:
        print(
            "--apply must run as root (only root may change another owner's files)", file=sys.stderr
        )
        return 1
    try:
        report = repair(arguments.platform_root, arguments.manager, apply=arguments.apply)
    except KeyError:
        print(f"Manager identity does not exist: {arguments.manager}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    for line in report.lines[:2000]:
        print(line)
    for line in report.refused:
        print(f"refused {line}")
    for line in report.incomplete:
        print(f"incomplete {line}")
    mode = "applied" if arguments.apply else "dry run"
    print(
        f"{mode}: {report.worktrees} worktree(s), {report.metadata} metadata repositories, "
        f"{report.modes} mode change(s), {report.owners} path(s) for {arguments.manager}, "
        f"{report.raced} changed during the walk, {report.foreign_device} on another "
        "filesystem (skipped)"
    )
    if report.incomplete or report.refused:
        print(
            f"NOT COMPLETE: {len(report.incomplete)} incomplete walk(s), "
            f"{len(report.refused)} refused path(s)",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
