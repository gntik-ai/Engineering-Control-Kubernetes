"""Atomic text writes that never follow a symlink at the destination (NEW-120 review)."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path


def open_directory_no_follow(root: Path, directory: Path, *, mode: int = 0o750) -> int:
    """Create `directory` under the trusted `root`, refusing a symlink at EVERY component.

    NEW-120 review 2: O_NOFOLLOW on the final component alone let a symlink at an
    intermediate component (e.g. scan/ui-kit) redirect creation and writes. Each component
    below `root` is created with mkdir(dir_fd=...) and opened with O_DIRECTORY|O_NOFOLLOW
    relative to its already-opened parent, so no component can be a symlink."""
    relative = directory.relative_to(root)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"refusing a non-canonical evidence path: {relative}")
    # The root is trusted configuration (settings.evidence_root): created if missing and
    # opened normally; every component BELOW it must be a real directory.
    root.mkdir(parents=True, exist_ok=True, mode=mode)
    current = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in relative.parts:
            try:
                os.mkdir(part, mode, dir_fd=current)
            except FileExistsError:
                pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = following
    except BaseException:
        os.close(current)
        raise
    # NEW-120 review 3: the caller keeps the VERIFIED descriptor and writes relative to it
    # (write_text_at); reopening the path would race a symlink swap of a parent.
    return current


def write_text_at(directory: int, name: str, text: str, *, mode: int = 0o640) -> None:
    """Atomically write `name` inside the already-verified directory descriptor."""
    if "/" in name or name in {"", ".", ".."}:
        raise ValueError(f"refusing a non-simple file name: {name!r}")
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise ValueError(f"refusing to overwrite a non-regular file or symlink: {name}")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory
    )
    created = True
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        created = False
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def write_text_no_follow(path: Path, text: str, *, mode: int = 0o640) -> None:
    """Write `text` to `path` via a temporary file in the same directory and `os.replace`.

    The directory is opened with O_NOFOLLOW, an existing destination must be a regular
    file (a symlink is refused, never followed), and the temporary file is created with
    O_EXCL|O_NOFOLLOW; `os.replace` swaps the directory entry, never a symlink's target.
    """
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    created = False
    try:
        try:
            existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError(f"refusing to overwrite a non-regular file or symlink: {path.name}")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=directory,
        )
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        created = False
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)
