from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from temporalio.exceptions import ApplicationError

_REQUIRED = frozenset({"AUTH_METHOD", "BASE_URL", "USERNAME", "PASSWORD"})
_OPTIONAL = frozenset({"TENANT_ID", "WORKSPACE_ID", "REALM_OR_ISSUER"})


@dataclass(frozen=True, repr=False)
class SuperadminCredential:
    project: str
    base_url: str
    username: str
    password: str
    tenant_id: str | None = None
    workspace_id: str | None = None
    realm_or_issuer: str | None = None


class SuperadminCredentialLoader:
    """Read one strict worker-local username/password contract without exporting it."""

    def __init__(self, credential_root: Path, *, production_permissions: bool = True) -> None:
        self.credential_root = credential_root.resolve()
        self.production_permissions = production_permissions

    def load(self, project: str) -> SuperadminCredential:
        if project not in {"falcone", "musematic"}:
            raise ApplicationError("unknown superadmin project", non_retryable=True)
        raw_path = self.credential_root / project / "superadmin.env"
        if raw_path.is_symlink():
            raise ApplicationError(
                "superadmin credential symlinks are prohibited",
                type="PolicyViolation",
                non_retryable=True,
            )
        path = raw_path.resolve()
        if not path.is_file() or not path.is_relative_to(self.credential_root):
            raise ApplicationError(
                "superadmin credential is unavailable",
                type=f"MANUAL_{project.upper()}_SUPERADMIN_INPUT_REQUIRED",
                non_retryable=True,
            )
        metadata = path.stat()
        parent = path.parent.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ApplicationError(
                "superadmin credential is not a regular file", non_retryable=True
            )
        if self.production_permissions:
            if stat.S_IMODE(metadata.st_mode) != 0o640 or stat.S_IMODE(parent.st_mode) != 0o750:
                raise ApplicationError(
                    "superadmin credential permissions violate the worker contract",
                    type="PolicyViolation",
                    non_retryable=True,
                )
            if (
                metadata.st_uid != 0
                or parent.st_uid != 0
                or metadata.st_gid != os.getegid()
                or parent.st_gid != os.getegid()
            ):
                raise ApplicationError(
                    "superadmin credential ownership violates the worker contract",
                    type="PolicyViolation",
                    non_retryable=True,
                )
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if not separator or name in values or name not in _REQUIRED | _OPTIONAL:
                raise ApplicationError(
                    "superadmin credential contract contains an undeclared field",
                    type="PolicyViolation",
                    non_retryable=True,
                )
            values[name] = value
        if set(values) & _REQUIRED != _REQUIRED or any(not values[name] for name in _REQUIRED):
            raise ApplicationError(
                "superadmin credential contract is incomplete",
                type=f"MANUAL_{project.upper()}_SUPERADMIN_INPUT_REQUIRED",
                non_retryable=True,
            )
        if values["AUTH_METHOD"] != "username_password":
            raise ApplicationError(
                "only username/password superadmin auth is accepted",  # gitleaks:allow
                non_retryable=True,
            )
        parsed = urlparse(values["BASE_URL"])
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ApplicationError("superadmin target must be an HTTPS origin", non_retryable=True)
        return SuperadminCredential(
            project=project,
            base_url=values["BASE_URL"].rstrip("/"),
            username=values["USERNAME"],
            password=values["PASSWORD"],
            tenant_id=values.get("TENANT_ID") or None,
            workspace_id=values.get("WORKSPACE_ID") or None,
            realm_or_issuer=values.get("REALM_OR_ISSUER") or None,
        )
