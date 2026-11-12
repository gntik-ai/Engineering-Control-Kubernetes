from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID

from engineering_control.dashboard.admission import AdmissionReader, unknown_admission
from engineering_control.dashboard.repository import DashboardRepository


class DashboardService:
    """Bounded read facade kept separate from HTTP and persistence details."""

    def __init__(
        self, repository: DashboardRepository, admission_reader: AdmissionReader | None = None
    ) -> None:
        self.repository = repository
        self.admission_reader = admission_reader

    async def backlog(
        self,
        *,
        project: str | None,
        status: str | None,
        search: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, object]:
        return await self.repository.list_backlog(
            project=project,
            status=status,
            search=search,
            page=page,
            page_size=page_size,
        )

    async def audits(self, *, project: str | None, status: str | None) -> dict[str, object]:
        return await self.repository.list_audits(project=project, status=status)

    async def overview(self) -> dict[str, object]:
        value = await asyncio.to_thread(self.repository.observability.overview)
        value["admission"] = (
            await self.admission_reader.read() if self.admission_reader else unknown_admission()
        )
        return value

    async def operation_response(self, value: dict[str, object]) -> dict[str, object]:
        """Keep the durable receipt authoritative when dispatcher observation fails.

        This is observational, not authorization. Never change an operation's
        status or turn an already accepted write into an apparent failed submit.
        """
        result = dict(value)
        if value.get("status") in {"PENDING", "DISPATCHING"}:
            try:
                async with asyncio.timeout(2):
                    observation = (
                        await self.admission_reader.read()
                        if self.admission_reader
                        else unknown_admission()
                    )
            except Exception:  # noqa: BLE001 - receipt must survive observation failure
                observation = unknown_admission()
            result["dispatch_observation"] = observation
        return result

    async def workflows(
        self,
        *,
        project: str | None,
        workflow_type: str | None,
        status: str | None,
        provider: str | None,
        role: str | None,
        worker: str | None,
        search: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self.repository.observability.list_workflows,
            project=project,
            workflow_type=workflow_type,
            status=status,
            provider=provider,
            role=role,
            worker=worker,
            search=search,
            date_from=date_from,
            date_to=date_to,
            cursor=cursor,
            limit=limit,
        )

    async def workflow(self, workflow_id: str) -> dict[str, object] | None:
        return await asyncio.to_thread(self.repository.observability.workflow, workflow_id)

    async def executions(
        self,
        *,
        project: str | None,
        workflow_id: str | None,
        issue_number: int | None,
        campaign_id: UUID | None,
        provider: str | None,
        role: str | None,
        status: str | None,
        worker: str | None,
        host: str | None,
        phase: str | None,
        search: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self.repository.observability.list_executions,
            project=project,
            workflow_id=workflow_id,
            issue_number=issue_number,
            campaign_id=campaign_id,
            provider=provider,
            role=role,
            status=status,
            worker=worker,
            host=host,
            phase=phase,
            search=search,
            date_from=date_from,
            date_to=date_to,
            cursor=cursor,
            limit=limit,
        )

    async def execution(self, execution_id: UUID) -> dict[str, object] | None:
        return await asyncio.to_thread(self.repository.observability.execution, execution_id)

    async def execution_logs(
        self,
        execution_id: UUID,
        *,
        limit: int,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
    ) -> dict[str, object] | None:
        return await asyncio.to_thread(
            self.repository.observability.execution_logs,
            execution_id,
            limit=limit,
            before_sequence=before_sequence,
            after_sequence=after_sequence,
        )

    async def services(self) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.observability.services)

    async def projects(self) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.observability.projects)

    async def project_exists(self, name: str) -> bool:
        return await asyncio.to_thread(self.repository.projects.exists, name)

    async def sync_registry_projects(self) -> list[str]:
        return await asyncio.to_thread(self.repository.projects.sync_registry)

    async def create_project(
        self, *, name: str, display_name: str | None, repository: str
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.projects.create(
                name=name, display_name=display_name, repository=repository
            )
        )

    async def rename_project(self, name: str, display_name: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.projects.rename, name, display_name)

    async def archive_project(self, name: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.projects.archive, name)

    async def root_blockers(
        self,
        *,
        project: str | None,
        campaign_id: UUID | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self.repository.observability.root_blockers,
            project=project,
            campaign_id=campaign_id,
            cursor=cursor,
            limit=limit,
        )

    async def findings(self, *, project: str | None, limit: int) -> dict[str, object]:
        return await self.repository.list_findings_view(project=project, limit=limit)

    async def enhancements(self, *, project: str | None, limit: int) -> dict[str, object]:
        return await self.repository.list_enhancements_view(project=project, limit=limit)
