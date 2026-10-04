"""Atomic text writes that never follow a symlink at the destination (NEW-120 review)."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path


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
