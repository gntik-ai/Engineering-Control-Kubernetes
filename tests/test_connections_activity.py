from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from engineering_control.activities.connections import ConnectionsActivityHandler
from engineering_control.config import Settings


@pytest.mark.asyncio
async def test_connections_refresh_step_runs_the_bounded_projector_and_reports_scope(
    tmp_path: Path,
) -> None:
    """NEW-05: the periodic schedule must drive the exact same bounded probe set

    recheck-connections already runs on demand, not a second, divergent implementation.
    """
    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path / 'connections.db'}")
    handler = ConnectionsActivityHandler(settings)
    handler.projector.refresh = AsyncMock()  # type: ignore[method-assign]

    result = await handler.handle("connections.refresh", {"scope": "public"})

    handler.projector.refresh.assert_awaited_once_with("public")
    assert result["status"] == "REFRESHED"
    assert result["scope"] == "public"


@pytest.mark.asyncio
async def test_connections_refresh_step_defaults_scope_to_all(tmp_path: Path) -> None:
    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path / 'connections.db'}")
    handler = ConnectionsActivityHandler(settings)
    handler.projector.refresh = AsyncMock()  # type: ignore[method-assign]

    result = await handler.handle("connections.refresh", {})

    handler.projector.refresh.assert_awaited_once_with("all")
    assert result["scope"] == "all"
