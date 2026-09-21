from __future__ import annotations

from typing import Any

from engineering_control.config import Settings
from engineering_control.operations.connections import ConnectionHealthProjector
from engineering_control.operations.repository import OperationsRepository


class ConnectionsActivityHandler:
    """Run the same bounded read-only connection-health probes `recheck-connections`

    already performs on demand, so a periodic schedule can keep the health snapshot
    from decaying to DEGRADED between missions (NEW-05).
    """

    supported_steps = frozenset({"connections.refresh"})

    def __init__(self, settings: Settings) -> None:
        # Deferred: OperationsRepository's engine construction isn't guarded by dialect
        # the way other controller handlers' are, so building it eagerly here breaks any
        # test that constructs ControlActivities against an in-memory sqlite Settings
        # without actually exercising this handler.
        self.settings = settings
        self._projector: ConnectionHealthProjector | None = None

    @property
    def projector(self) -> ConnectionHealthProjector:
        if self._projector is None:
            repository = OperationsRepository(
                self.settings.database_url.get_secret_value(),
                project_config_root=self.settings.project_config_root,
            )
            self._projector = ConnectionHealthProjector(repository)
        return self._projector

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        scope = str(payload.get("scope") or "all")
        await self.projector.refresh(scope)
        return {**payload, "status": "REFRESHED", "scope": scope}
