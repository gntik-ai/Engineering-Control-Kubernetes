from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from temporalio.exceptions import ApplicationError

from engineering_control.config import Settings
from engineering_control.db.models import QuotaSnapshot
from engineering_control.models.enums import Provider


class QuotaSnapshotRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    provider: Provider
    captured_at: datetime
    remaining_capacity: float | None = Field(default=None, ge=0)
    total_capacity: float | None = Field(default=None, ge=0)
    reset_at: datetime | None = None
    running_jobs: int = Field(default=0, ge=0)
    rate_limited: bool = False
    source: str = Field(min_length=1, max_length=80)


class MetadataActivityHandler:
    """Controller-side authoritative metadata writes for idempotent activity results."""

    supported_steps = frozenset({"quota.persist"})

    def __init__(self, settings: Settings) -> None:
        database_url = settings.database_url.get_secret_value()
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=2,
            connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        if step != "quota.persist":
            raise ApplicationError("unsupported metadata activity", non_retryable=True)
        raw_snapshots = payload.get("snapshots")
        if not isinstance(raw_snapshots, list):
            raise ApplicationError(
                "quota persistence requires snapshot records",
                type="PolicyViolation",
                non_retryable=True,
            )
        try:
            snapshots = [QuotaSnapshotRecord.model_validate(item) for item in raw_snapshots]
        except ValidationError as error:
            raise ApplicationError(
                "quota snapshot failed strict validation",
                type="PolicyViolation",
                non_retryable=True,
            ) from error
        await asyncio.to_thread(self._persist_quota, snapshots)
        return {**payload, "status": "PERSISTED", "persisted_snapshots": len(snapshots)}

    def _persist_quota(self, snapshots: list[QuotaSnapshotRecord]) -> None:
        for snapshot in snapshots:
            try:
                with self.sessions.begin() as session:
                    captured = snapshot.captured_at.astimezone(UTC)
                    identity = uuid5(
                        NAMESPACE_URL,
                        f"engineering-quota:{snapshot.provider.value}:{captured.isoformat()}",
                    )
                    if session.get(QuotaSnapshot, identity) is not None:
                        continue
                    session.add(
                        QuotaSnapshot(
                            id=identity,
                            provider=snapshot.provider.value,
                            captured_at=captured,
                            remaining_capacity=snapshot.remaining_capacity,
                            total_capacity=snapshot.total_capacity,
                            reset_at=(
                                snapshot.reset_at.astimezone(UTC) if snapshot.reset_at else None
                            ),
                            running_jobs=snapshot.running_jobs,
                            rate_limited=snapshot.rate_limited,
                            source=snapshot.source,
                        )
                    )
            except IntegrityError:
                # A concurrent retry may have committed the deterministic primary key first.
                continue
