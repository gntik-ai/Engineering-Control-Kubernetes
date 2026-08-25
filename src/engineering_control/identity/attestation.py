from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SuperadminReadinessAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: Literal["falcone", "musematic"]
    contract_id: Literal["falcone-superadmin", "musematic-superadmin"]
    file_present: bool
    parser_valid: bool
    target_valid: bool
    authentication_valid: bool
    superadmin_role_valid: bool
    worker_identity: str = Field(pattern=r"^browser-runner@[A-Za-z0-9.-]{1,253}$")
    checked_timestamp: datetime
    attestation_ttl_seconds: int = Field(ge=30, le=3600)
    contract_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    blocker_code: str = Field(pattern=r"^[A-Z0-9_]+$")

    @model_validator(mode="after")
    def binding(self) -> SuperadminReadinessAttestation:
        if self.contract_id != f"{self.project}-superadmin":
            raise ValueError("superadmin attestation contract does not match its project")
        return self

    def current(self, *, now: datetime | None = None) -> bool:
        checked = self.checked_timestamp
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        return checked <= current + timedelta(seconds=30) and current <= checked + timedelta(
            seconds=self.attestation_ttl_seconds
        )

    def ready(self) -> bool:
        return self.current() and all(
            (
                self.file_present,
                self.parser_valid,
                self.target_valid,
                self.authentication_valid,
                self.superadmin_role_valid,
                self.blocker_code == "NONE",
            )
        )


def superadmin_contract_hash(project: str) -> str:
    if project not in {"falcone", "musematic"}:
        raise ValueError("superadmin contract project is invalid")
    return (
        "sha256:"
        + hashlib.sha256(
            f"{project}:username_password:BASE_URL:USERNAME:PASSWORD:v2".encode()
        ).hexdigest()
    )
