"""NEW-228: loopback-only networking and a writable checker copy for the agent sandbox.

gntik-ui makers and checkers could not run the browser suites (a11y: Playwright and axe
against `vite preview` on localhost:4173; visual): "listen EPERM ::1:4173". Opt-in per
project (manifest ``sandbox:``, travelling in ``toolchain_preparation.agent_sandbox``).

Codex keeps today's sandbox (no sockets at all): the opt-in applies to Claude runs only.

Claude Code 2.1.283 has no sandbox in this deployment (its own needs socat, which the
worker does not have; its allowLocalBinding is macOS-only). Its documented
CLAUDE_CODE_SHELL_PREFIX runs every Bash tool command as ``<prefix> '<command>'``;
the prefix is scripts/agent-loopback-shell, a root-owned script in the release that runs
the command under bwrap with network and PID namespaces of its own (only ``lo``) and a
filesystem view holding no host unix socket: read-only system trees, the run's own
directories (named here, pinned in --settings) and fresh tmpfs mounts for /run, /tmp,
/var/tmp, /dev/shm and the home. Same user.

The Claude checker (claude-runner) can only read the codex-runner-owned worktree, so its
builds failed with EACCES. With ``checker_scratch_copy`` it works in a private copy of it.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

AGENT_SANDBOX_KEY = "agent_sandbox"


@dataclass(frozen=True)
class AgentSandbox:
    loopback_network: bool = False
    checker_scratch_copy: bool = False
    # Set by the provider worker, never read from a payload: the verified, fully resolved
    # loopback shell (the path checked is the path run) and the directories, besides the
    # run's working directory and TMPDIR, its commands see (read-only or writable).
    loopback_shell: str | None = None
    readable_roots: tuple[str, ...] = ()
    writable_roots: tuple[str, ...] = ()


# Fields only the provider worker sets.
_WORKER_FIELDS = frozenset({"loopback_shell", "readable_roots", "writable_roots"})


def payload_agent_sandbox(payload: Mapping[str, Any]) -> AgentSandbox | None:
    """The opt-ins delivery.prepare_toolchain put in the payload; absent is today's sandbox."""
    preparation = payload.get("toolchain_preparation")
    if not isinstance(preparation, Mapping) or AGENT_SANDBOX_KEY not in preparation:
        return None
    raw = preparation[AGENT_SANDBOX_KEY]
    known = {field.name for field in fields(AgentSandbox)} - _WORKER_FIELDS
    if (
        not isinstance(raw, Mapping)
        or set(raw) - known
        or any(value is not True for value in raw.values())
    ):
        raise ValueError("toolchain_preparation.agent_sandbox is malformed")
    return AgentSandbox(**raw) if raw else None


def claude_loopback_settings(
    shell: str,
    *,
    writable: Sequence[str] = (),
    readable: Sequence[str] = (),
) -> dict[str, Any]:
    """--settings JSON: flag settings beat user and project settings, so no `env` there can
    unset the prefix, pick another shell (one that ignores it) or name other directories
    for the shell to mount (every key is set, empty or not)."""
    for path in (*writable, *readable):
        if not path.startswith("/") or ":" in path:
            raise ValueError("loopback sandbox roots must be absolute paths without ':'")
    return {
        "env": {
            "CLAUDE_CODE_SHELL_PREFIX": shell,
            "CLAUDE_CODE_SHELL": "/bin/bash",
            "HERMES_LOOPBACK_WRITABLE": ":".join(writable),
            "HERMES_LOOPBACK_READABLE": ":".join(readable),
        }
    }


# The bubblewrap scripts/agent-loopback-shell runs.
LOOPBACK_SHELL_BWRAP = Path("/usr/bin/bwrap")


def verified_loopback_shell(shell: Path) -> tuple[Path | None, str | None]:
    """The prefix runs outside the namespace for every command, so the agent must not be
    able to replace it: a regular executable file whose resolved path (every component)
    this user cannot write. Returns (the resolved path to run, None) or (None, why not)."""
    if not os.access(LOOPBACK_SHELL_BWRAP, os.X_OK):
        return None, f"{LOOPBACK_SHELL_BWRAP} is not executable"
    if not shell.is_absolute():
        return None, f"{shell} is not an absolute path"
    try:
        resolved = shell.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as error:
        return None, f"{shell}: {type(error).__name__}"
    if not stat.S_ISREG(mode) or not os.access(resolved, os.X_OK):
        return None, f"{resolved} is not an executable file"
    for path in (resolved, *resolved.parents):
        if os.access(path, os.W_OK):
            return None, f"{path} is writable by this user"
    return resolved, None


def loopback_shell_problem(shell: Path) -> str | None:
    return verified_loopback_shell(shell)[1]


# The copy is bounded: a maker cannot make its checker fill the worker's disk.
SCRATCH_COPY_MAX_BYTES = 4 * 1024**3
SCRATCH_COPY_MAX_ENTRIES = 300_000


class ScratchCopyError(RuntimeError):
    """The maker's worktree could not be copied within the bounds."""


def copy_worktree(source: Path, destination: Path) -> None:
    """Copy a worktree for the checker: directories, regular files (mode and mtime kept,
    so package managers see the install as current) and symlinks as symlinks.

    A pnpm virtual store (``node_modules/.pnpm``, third-party packages only) is linked,
    not copied: the bulk of an install, never rebuilt. Workspace links (relative
    symlinks) then resolve inside the copy, so the checker builds its own copy's
    packages. Sockets, FIFOs and devices are skipped (a FIFO would block the copy).
    ``destination`` must not exist."""
    source = source.resolve(strict=True)
    destination.mkdir(mode=0o700)
    size = 0
    entries = 0
    directories: list[tuple[Path, Path]] = [(source, destination)]

    def count() -> None:
        nonlocal entries
        entries += 1  # before each entry is created: the bound holds for empty entries too
        if entries > SCRATCH_COPY_MAX_ENTRIES:
            raise ScratchCopyError(f"worktree exceeds {SCRATCH_COPY_MAX_ENTRIES} entries")

    for top, names, files in os.walk(source, followlinks=False):
        here = Path(top)
        target = destination / here.relative_to(source)
        for name in list(names):
            count()
            origin = here / name
            if name == ".pnpm" and here.name == "node_modules":
                names.remove(name)
                os.symlink(origin, target / name)
            elif origin.is_symlink():
                # os.walk lists a symlink to a directory as a directory; never followed.
                names.remove(name)
                os.symlink(os.readlink(origin), target / name)
            else:
                (target / name).mkdir(mode=0o700)
                directories.append((origin, target / name))
        for name in files:
            count()
            origin = here / name
            info = origin.lstat()
            if stat.S_ISLNK(info.st_mode):
                os.symlink(os.readlink(origin), target / name)
            elif stat.S_ISREG(info.st_mode):
                size += info.st_size
                if size > SCRATCH_COPY_MAX_BYTES:
                    raise ScratchCopyError(f"worktree exceeds {SCRATCH_COPY_MAX_BYTES} bytes")
                # No ACLs or other xattrs, and never a set-id bit: the copy is the checker's.
                shutil.copyfile(origin, target / name, follow_symlinks=False)
                os.chmod(target / name, (info.st_mode & 0o777) | stat.S_IRUSR | stat.S_IWUSR)
                os.utime(target / name, ns=(info.st_atime_ns, info.st_mtime_ns))
    # Directory metadata last: the copy's own writes above changed every mtime.
    for origin, target in reversed(directories):
        info = origin.stat()
        os.chmod(target, (info.st_mode & 0o777) | stat.S_IRWXU)
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))


def remove_tree(path: Path) -> None:
    """Remove a scratch copy, including directories the checker made unwritable."""
    if not path.is_dir() or path.is_symlink():
        return
    os.chmod(path, stat.S_IRWXU)
    for top, names, _files in os.walk(path):
        for name in names:
            child = os.path.join(top, name)
            if not os.path.islink(child):  # chmod follows symlinks
                with suppress(OSError):
                    os.chmod(child, stat.S_IRWXU)
    shutil.rmtree(path)
