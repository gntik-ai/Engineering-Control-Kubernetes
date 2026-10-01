"""NEW-108: root-attested change-request approvals for the GitHub worker.

The GitHub worker deliberately has no database credential. As with the backlog write
policy (NEW-94), durable authorization reaches it as a root-owned file: the API and the
controller only drop an id-only request; a root reconciler re-reads the row from
PostgreSQL, re-verifies it, and writes (or revokes) the attestation the worker reads.
Nothing in an activity payload can create or widen an attestation.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ATTESTATION_TTL = timedelta(hours=24)
_REQUEST_FILE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$")
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class ChangeRequestAttestation(BaseModel):
    """A root-written copy of one APPROVED, re-verified change request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change_request_id: str = Field(pattern=_UUID)
    revision: int = Field(ge=1)
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
    attested_at: datetime
    expires_at: datetime


class ChangeRequestAttestationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    change_request_id: str = Field(pattern=_UUID)
    requested_at: datetime


def request_attestation(root: Path, change_request_id: UUID, *, now: datetime) -> Path:
    """Ask the root reconciler to re-derive one attestation from durable state."""
    request = ChangeRequestAttestationRequest(
        change_request_id=str(change_request_id), requested_at=now
    )
    target = root / f"{change_request_id}.json"
    _atomic_write(target, request.model_dump_json(), mode=0o600)
    return target


def pending_requests(root: Path, *, owner: int | None) -> tuple[list[UUID], list[Path], list[str]]:
    """Valid request ids, every request file to consume, and per-file rejection reasons."""
    identifiers: list[UUID] = []
    consumed: list[Path] = []
    rejected: list[str] = []
    if not root.is_dir():
        return identifiers, consumed, rejected
    for path in sorted(root.iterdir()):
        if path.name.startswith("."):
            continue
        consumed.append(path)
        try:
            metadata = path.lstat()
            if (
                not _REQUEST_FILE.fullmatch(path.name)
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (owner is not None and metadata.st_uid != owner)
            ):
                raise ValueError("request file identity is invalid")
            request = ChangeRequestAttestationRequest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if f"{request.change_request_id}.json" != path.name:
                raise ValueError("request file does not name its change request")
            identifiers.append(UUID(request.change_request_id))
        except (OSError, ValueError, ValidationError):
            rejected.append(path.name[:80])
    return identifiers, consumed, rejected


def attested_identifiers(root: Path) -> list[UUID]:
    if not root.is_dir():
        return []
    return [
        UUID(path.name.removesuffix(".json"))
        for path in sorted(root.iterdir())
        if _REQUEST_FILE.fullmatch(path.name)
    ]


def write_attestation(
    root: Path, attestation: ChangeRequestAttestation, *, group: int | None
) -> Path:
    target = root / f"{attestation.change_request_id}.json"
    _atomic_write(target, attestation.model_dump_json(), mode=0o640, group=group)
    return target


def revoke_attestation(root: Path, change_request_id: UUID) -> bool:
    target = root / f"{change_request_id}.json"
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True


def load_attestation(
    root: Path, change_request_id: str, *, production: bool, now: datetime
) -> ChangeRequestAttestation | None:
    """The current root-owned attestation, or None when absent, unsafe, expired or foreign."""
    if not re.fullmatch(_UUID, change_request_id):
        return None
    path = root / f"{change_request_id}.json"
    try:
        metadata = path.lstat()
        parent = root.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        return None
    if production and (
        metadata.st_uid != 0
        or parent.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o640
        or metadata.st_gid != parent.st_gid
    ):
        return None
    try:
        attestation = ChangeRequestAttestation.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, ValidationError):
        return None
    if attestation.change_request_id != change_request_id:
        return None
    expires_at = attestation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        return None
    return attestation


def _atomic_write(target: Path, content: str, *, mode: int, group: int | None = None) -> None:
    descriptor, raw = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=target.parent)
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, mode)
        if group is not None:
            os.fchown(descriptor, 0, group)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
