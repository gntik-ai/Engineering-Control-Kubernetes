"""NEW-108: root-attested change-request approvals for the GitHub worker.

The GitHub worker deliberately has no database credential. As with the backlog write
policy (NEW-94), durable authorization reaches it as a root-owned file: the API and the
controller only drop an id-only request; a root reconciler re-reads PostgreSQL, re-verifies
the row against its approval record, and writes (or revokes) the attestation the worker
reads. Nothing in an activity payload can create or widen an attestation.

Every directory is opened with O_DIRECTORY|O_NOFOLLOW and validated by descriptor, and
every entry is handled relative to that descriptor without following links, so a
writer that owns a parent directory cannot redirect root's reads, writes or unlinks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ATTESTATION_TTL = timedelta(hours=24)
# root:engineering-control, sticky, group may only create (no listing, no foreign unlink).
REQUEST_DIRECTORY_MODE = 0o1730
ATTESTATION_DIRECTORY_MODE = 0o750
QUARANTINE = ".quarantine"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ENTRY = re.compile(rf"^({_UUID})\.json$")
_WRITER_TEMPORARY = re.compile(r"^\.[A-Za-z0-9_-]{1,80}\.tmp$")
_MAXIMUM_REQUEST_BYTES = 4096
_MAXIMUM_ATTESTATION_BYTES = 1_000_000


class ChangeRequestAttestation(BaseModel):
    """A root-written copy of one APPROVED, re-verified change request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change_request_id: str = Field(pattern=rf"^{_UUID}$")
    revision: int = Field(ge=1)
    decision_sequence: int = Field(ge=1)
    project: Literal["falcone", "musematic", "llmwiki"]
    target_repository: str
    approval_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    proposal: dict[str, Any]
    review: dict[str, Any]
    description_redacted: str
    issue_title: str
    issue_body: str
    approved_by: str
    approved_at: datetime
    attested_at: datetime
    expires_at: datetime


class ChangeRequestAttestationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    change_request_id: str = Field(pattern=rf"^{_UUID}$")
    decision_sequence: int = Field(ge=0)
    requested_at: datetime


@dataclass(frozen=True)
class PendingRequest:
    change_request_id: UUID
    name: str
    inode: int
    modified_ns: int
    content_sha256: str


def request_attestation(
    root: Path, change_request_id: UUID, *, decision_sequence: int, now: datetime
) -> Path:
    """Writer side (API/controller): ask the root reconciler to re-derive one attestation."""
    request = ChangeRequestAttestationRequest(
        change_request_id=str(change_request_id),
        decision_sequence=decision_sequence,
        requested_at=now,
    )
    target = root / f"{change_request_id}.json"
    descriptor, raw = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=root)
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(request.model_dump_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def _open_directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


class _Directory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.descriptor = -1

    def __enter__(self) -> Self:
        self.descriptor = _open_directory(self.path)
        try:
            self._validate(os.fstat(self.descriptor))
        except BaseException:
            os.close(self.descriptor)
            raise
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        os.close(self.descriptor)

    def _validate(self, metadata: os.stat_result) -> None:
        raise NotImplementedError

    def _entry(self, name: str) -> os.stat_result:
        return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)

    def _read(self, name: str, maximum: int) -> tuple[os.stat_result, bytes]:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=self.descriptor,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
                raise ValueError("entry is not a bounded regular file")
            content = os.read(descriptor, maximum + 1)
        finally:
            os.close(descriptor)
        if len(content) > maximum:
            raise ValueError("entry exceeds its bound")
        return metadata, content


class RequestDirectory(_Directory):
    """Root side of the drop-only request directory."""

    def __init__(self, path: Path, *, owner: int | None, group: int | None, strict: bool) -> None:
        super().__init__(path)
        self.owner = owner
        self.group = group
        self.strict = strict

    def _validate(self, metadata: os.stat_result) -> None:
        if metadata.st_mode & stat.S_IWOTH:
            raise PermissionError("request directory is world-writable")
        if self.strict and (
            metadata.st_uid != 0
            or metadata.st_gid != self.group
            or stat.S_IMODE(metadata.st_mode) != REQUEST_DIRECTORY_MODE
        ):
            raise PermissionError("request directory is not the root-owned drop directory")

    def scan(self) -> tuple[list[PendingRequest], list[str]]:
        """Valid requests, plus every unexpected entry (quarantined, never unlinked)."""
        pending: list[PendingRequest] = []
        quarantined: list[str] = []
        for name in sorted(os.listdir(self.descriptor)):
            if name == QUARANTINE or _WRITER_TEMPORARY.fullmatch(name):
                continue
            try:
                metadata = self._entry(name)
            except FileNotFoundError:
                continue
            match = _ENTRY.fullmatch(name)
            if (
                match is None
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (self.owner is not None and metadata.st_uid != self.owner)
            ):
                quarantined.append(self._quarantine(name))
                continue
            try:
                opened, content = self._read(name, _MAXIMUM_REQUEST_BYTES)
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                quarantined.append(self._quarantine(name))
                continue
            if opened.st_ino != metadata.st_ino:
                continue  # replaced while reading; the next run sees the new request
            try:
                request = ChangeRequestAttestationRequest.model_validate_json(content)
            except ValidationError:
                quarantined.append(self._quarantine(name))
                continue
            if request.change_request_id != match.group(1):
                quarantined.append(self._quarantine(name))
                continue
            pending.append(
                PendingRequest(
                    change_request_id=UUID(request.change_request_id),
                    name=name,
                    inode=opened.st_ino,
                    modified_ns=opened.st_mtime_ns,
                    content_sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        return pending, quarantined

    def consume(self, request: PendingRequest) -> bool:
        """Unlink a request only if it is unchanged since it was read."""
        try:
            metadata, content = self._read(request.name, _MAXIMUM_REQUEST_BYTES)
        except (OSError, ValueError):
            return False
        if (
            metadata.st_ino != request.inode
            or metadata.st_mtime_ns != request.modified_ns
            or hashlib.sha256(content).hexdigest() != request.content_sha256
        ):
            return False
        os.unlink(request.name, dir_fd=self.descriptor)
        return True

    def _quarantine(self, name: str) -> str:
        try:
            os.mkdir(QUARANTINE, mode=0o700, dir_fd=self.descriptor)
        except FileExistsError:
            pass
        quarantine = os.open(
            QUARANTINE,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=self.descriptor,
        )
        try:
            metadata = os.fstat(quarantine)
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise PermissionError("request quarantine is not owned by the reconciler")
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            digest = hashlib.sha256(name.encode("utf-8", "surrogateescape")).hexdigest()[:16]
            os.rename(name, f"{stamp}-{digest}", src_dir_fd=self.descriptor, dst_dir_fd=quarantine)
        finally:
            os.close(quarantine)
        return f"{stamp}-{digest}"


class AttestationDirectory(_Directory):
    """Root side of the attestation directory the GitHub worker reads."""

    def __init__(self, path: Path, *, group: int | None, strict: bool) -> None:
        super().__init__(path)
        self.group = group
        self.strict = strict

    def _validate(self, metadata: os.stat_result) -> None:
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("attestation directory is writable by others")
        if self.strict and (
            metadata.st_uid != 0
            or metadata.st_gid != self.group
            or stat.S_IMODE(metadata.st_mode) != ATTESTATION_DIRECTORY_MODE
        ):
            raise PermissionError("attestation directory is not root-owned")

    def identifiers(self) -> list[UUID]:
        found: list[UUID] = []
        for name in sorted(os.listdir(self.descriptor)):
            match = _ENTRY.fullmatch(name)
            if match is not None and stat.S_ISREG(self._entry(name).st_mode):
                found.append(UUID(match.group(1)))
        return found

    def write(self, attestation: ChangeRequestAttestation) -> None:
        temporary = f".{secrets.token_hex(16)}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o640,
            dir_fd=self.descriptor,
        )
        try:
            os.fchmod(descriptor, 0o640)
            if self.group is not None:
                os.fchown(descriptor, 0, self.group)
            os.write(descriptor, attestation.model_dump_json().encode("utf-8"))
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.rename(
                temporary,
                f"{attestation.change_request_id}.json",
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self.descriptor)
            except FileNotFoundError:
                pass
            raise

    def revoke(self, change_request_id: UUID) -> bool:
        try:
            os.unlink(f"{change_request_id}.json", dir_fd=self.descriptor)
        except FileNotFoundError:
            return False
        return True


def load_attestation(
    root: Path, change_request_id: str, *, production: bool, now: datetime
) -> ChangeRequestAttestation | None:
    """Worker side: the current root-owned attestation, or None when absent, unsafe,
    expired or foreign."""
    if not re.fullmatch(_UUID, change_request_id):
        return None
    try:
        directory = _open_directory(root)
    except OSError:
        return None
    try:
        parent = os.fstat(directory)
        descriptor = os.open(
            f"{change_request_id}.json",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
    except OSError:
        os.close(directory)
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or metadata.st_size > _MAXIMUM_ATTESTATION_BYTES
        ):
            return None
        if production and (
            metadata.st_uid != 0
            or parent.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o640
            or metadata.st_gid != parent.st_gid
        ):
            return None
        raw = os.read(descriptor, _MAXIMUM_ATTESTATION_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
        os.close(directory)
    try:
        attestation = ChangeRequestAttestation.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        return None
    if attestation.change_request_id != change_request_id:
        return None
    expires_at = attestation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        return None
    return attestation
