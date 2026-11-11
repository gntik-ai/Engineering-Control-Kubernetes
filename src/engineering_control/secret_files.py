"""Reading secrets that are mounted into the process as files.

In a cluster every secret the control plane holds (service tokens, the dashboard session
key, the notification key) arrives as a file of a mounted Secret volume. Such a volume has a
fixed shape the kubelet controls: the visible name is a symbolic link into a ``..data``
directory of the same volume, the file belongs to root, and its group and mode come from the
pod's ``fsGroup`` and the volume's ``defaultMode``.

One rule for all of them, so a secret is never read from a file other processes of the pod's
user could have planted or anyone else can read:

* the path is absolute and resolves to a regular file inside its own directory (the
  ``..data`` indirection is allowed, leaving the volume is not);
* nobody outside owner and group can read it, and only the owner may write it;
* an owner-only file belongs to root or to this process's user; a group-readable file
  belongs to root and to one of this process's groups;
* its size is within the bounds the caller states.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def read_mounted_secret(path: Path, *, label: str, minimum: int = 1, maximum: int = 4096) -> bytes:
    """Return the bytes of the secret at ``path``, or raise without revealing them.

    ``PermissionError`` for custody problems (location, owner, mode), ``ValueError`` for a
    file that is not a secret of the expected size.
    """
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    directory = Path(os.path.realpath(path.parent))
    resolved = Path(os.path.realpath(path))
    if not resolved.is_relative_to(directory):
        raise PermissionError(f"{label} resolves outside its own directory")
    descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} must be a regular file")
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IXGRP | stat.S_IRWXO):
            raise PermissionError(
                f"{label} permissions are too broad: it may grant group read but not group "
                "write/execute or other access"
            )
        if metadata.st_mode & stat.S_IRGRP:
            # Readable through a group: only as root's file shared with a group of this
            # service, which is how a mounted Secret with an fsGroup arrives.
            if metadata.st_uid != 0 or metadata.st_gid not in {os.getegid(), *os.getgroups()}:
                raise PermissionError(
                    f"group-readable {label} must belong to root and to a group of this service"
                )
        elif metadata.st_uid not in {0, os.geteuid()}:
            raise PermissionError(f"{label} is not owned by root or by this service")
        raw = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    if len(raw) > maximum:
        raise ValueError(f"{label} is larger than {maximum} bytes")
    if len(raw) < minimum:
        raise ValueError(f"{label} is smaller than {minimum} bytes")
    return raw


def read_mounted_token(path: Path, *, label: str, minimum: int = 32) -> str:
    """A text token (surrounding whitespace ignored) of at least ``minimum`` characters."""
    token = read_mounted_secret(path, label=label).decode("utf-8").strip()
    if len(token) < minimum:
        raise ValueError(f"{label} must contain at least {minimum} characters")
    return token
