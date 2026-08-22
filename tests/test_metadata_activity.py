from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import QuotaSnapshot


@pytest.mark.asyncio
async def test_quota_persistence_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "metadata.sqlite"
    handler = MetadataActivityHandler(Settings(database_url=f"sqlite:///{database}"))
    Base.metadata.create_all(handler.engine)
    captured_at = datetime.now(UTC).isoformat()
    payload = {
        "snapshots": [
            {
                "provider": "codex",
                "captured_at": captured_at,
                "remaining_capacity": None,
                "total_capacity": None,
                "reset_at": None,
                "running_jobs": 0,
                "rate_limited": False,
                "source": "native_cli_auth_status;capacity_not_exposed",
                "authenticated": False,
            }
        ]
    }

    first = await handler.handle("quota.persist", payload)
    second = await handler.handle("quota.persist", payload)

    assert first["status"] == "PERSISTED"
    assert second["persisted_snapshots"] == 1
    with handler.sessions() as session:
        count = session.scalar(select(func.count()).select_from(QuotaSnapshot))
    assert count == 1
