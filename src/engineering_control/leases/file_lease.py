from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO


class LeaseConflict(RuntimeError):
    pass


class FileLease:
    """Host-local lock; durable ownership is also recorded in PostgreSQL by activities."""

    def __init__(self, root: Path, key: str, owner: str) -> None:
        if not key or any(part in {"", ".", ".."} for part in key.split("/")):
            raise ValueError("invalid lease key")
        self.root = root
        self.key = key.replace("/", "__")
        self.owner = owner
        self.path = root / f"{self.key}.lease"
        self._file: IO[str] | None = None

    def acquire(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o750)
        handle = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o640)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.seek(0)
            current = handle.read(4096)
            handle.close()
            raise LeaseConflict(f"lease {self.key} is held: {current}") from error
        handle.seek(0)
        handle.truncate()
        json.dump(
            {
                "owner": self.owner,
                "pid": os.getpid(),
                "acquired_at": datetime.now(UTC).isoformat(),
            },
            handle,
            sort_keys=True,
        )
        handle.flush()
        os.fsync(handle.fileno())
        self._file = handle

    def release(self) -> None:
        if self._file is None:
            return
        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None

    def __enter__(self) -> FileLease:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
