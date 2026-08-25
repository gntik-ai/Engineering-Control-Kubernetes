from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from engineering_control.policy.github_write import GitHubWritePolicyDocument

DEFAULT_GITHUB_WRITE_POLICY: dict[str, object] = {
    "enabled": False,
    "safe_repositories": [],
}
KNOWN_INVALID_GENERATED_POLICY: dict[str, object] = {
    "schema_version": 1,
    "enabled": False,
    "safe_repositories": [],
    "reason": "DISABLED_UNTIL_SEPARATE_OPERATOR_WRITE_VALIDATION",
}
ReconcileAction = Literal["CREATED", "PRESERVED", "REPLACED_KNOWN_INVALID_DEFAULT"]


def load_policy_data(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError(f"GitHub write policy must not be a symlink: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"GitHub write policy cannot be loaded safely: {path}") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"GitHub write policy must be a string-keyed object: {path}")
    return value


def validate_default_template(path: Path) -> bytes:
    value = load_policy_data(path)
    if value != DEFAULT_GITHUB_WRITE_POLICY or set(value) != set(DEFAULT_GITHUB_WRITE_POLICY):
        raise ValueError("source GitHub write policy is not the exact strict disabled default")
    try:
        GitHubWritePolicyDocument.model_validate(value)
    except ValidationError as exc:
        raise ValueError("source GitHub write policy fails the strict runtime model") from exc
    return path.read_bytes()


def _atomic_install(
    *,
    content: bytes,
    target: Path,
    mode: int,
    owner_uid: int | None,
    owner_gid: int | None,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        if owner_uid is not None and owner_gid is not None:
            os.chown(temporary, owner_uid, owner_gid)
        os.replace(temporary, target)
        directory_descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_known_invalid(*, target: Path, backup_root: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_directory = backup_root / timestamp
    suffix = 0
    while backup_directory.exists():
        suffix += 1
        backup_directory = backup_root / f"{timestamp}-{suffix}"
    backup_directory.mkdir(parents=True, mode=0o700)
    backup_directory.chmod(0o700)
    backup_path = backup_directory / f"{target.name}.known-invalid"
    shutil.copyfile(target, backup_path)
    backup_path.chmod(0o600)
    if os.geteuid() == 0:
        os.chown(backup_directory, 0, 0)
        os.chown(backup_path, 0, 0)
    return backup_path


def reconcile_policy(
    *,
    template: Path,
    target: Path,
    backup_root: Path,
    mode: int = 0o640,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> tuple[ReconcileAction, Path | None]:
    content = validate_default_template(template)
    if not target.parent.is_dir() or target.parent.is_symlink():
        raise ValueError(
            f"GitHub policy parent must be an existing real directory: {target.parent}"
        )
    if target.is_symlink():
        raise ValueError(f"GitHub write policy must not be a symlink: {target}")
    if not target.exists():
        _atomic_install(
            content=content,
            target=target,
            mode=mode,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        return "CREATED", None

    current = load_policy_data(target)
    try:
        GitHubWritePolicyDocument.model_validate(current)
    except ValidationError as exc:
        if current != KNOWN_INVALID_GENERATED_POLICY:
            raise ValueError(
                "refusing to replace unknown or operator-modified invalid policy"
            ) from exc
        backup_path = _backup_known_invalid(target=target, backup_root=backup_root)
        _atomic_install(
            content=content,
            target=target,
            mode=mode,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        return "REPLACED_KNOWN_INVALID_DEFAULT", backup_path
    return "PRESERVED", None
