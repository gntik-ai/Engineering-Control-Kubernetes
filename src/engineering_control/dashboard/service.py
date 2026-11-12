from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from engineering_control.cluster import Cluster
from engineering_control.dashboard.accounts import (
    check_credential,
    provider_catalogue,
    secret_name,
)
from engineering_control.dashboard.admission import AdmissionReader, unknown_admission
from engineering_control.dashboard.project_records import ProjectInvalid
from engineering_control.dashboard.project_sections import Guide
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.github_app import GitHubApp, GitHubAppError


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
        added = await asyncio.to_thread(self.repository.projects.sync_registry)
        # Every project has its core sections, whenever it was created.
        await asyncio.to_thread(self.repository.sections.sync_core)
        return added

    async def project(self, name: str) -> dict[str, object] | None:
        """One project with its counts and its sections; None when there is none."""
        listing = await asyncio.to_thread(self.repository.observability.projects)
        for item in cast(list[dict[str, object]], listing["items"]):
            if item["project"] == name:
                sections = await asyncio.to_thread(self.repository.sections.of_project, name)
                return {"project": item, "sections": sections}
        return None

    async def add_section(self, project: str, *, name: str, guide: Guide) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.sections.add(project, name=name, guide=guide)
        )

    async def update_section(
        self, project: str, key: str, *, name: str | None, guide: Guide
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.sections.update(project, key, name=name, guide=guide)
        )

    async def routing_brief(self, project: str) -> str:
        return await asyncio.to_thread(self.repository.sections.brief, project)

    async def remove_section(self, project: str, key: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.sections.remove, project, key)

    async def assign_flow(self, project: str, key: str, flow: str | None) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.sections.assign, project, key, flow)

    async def items(
        self,
        *,
        project: str | None,
        section: str | None,
        state: str | None,
        search: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.backlog.listing(
                project=project,
                section=section,
                state=state,
                search=search,
                page=page,
                page_size=page_size,
            )
        )

    async def add_item(self, project: str, *, title: str, section: str | None) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.backlog.add(project, title=title, section=section)
        )

    async def route_item(self, item_id: UUID, section: str | None) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.backlog.route, item_id, section)

    async def set_item_state(self, item_id: UUID, state: str) -> dict[str, object]:
        return await asyncio.to_thread(self.repository.backlog.set_state, item_id, state)

    async def inbox(self) -> dict[str, object]:
        # An account nobody can work with stops everything, so it comes first.
        entries = await asyncio.to_thread(
            lambda: self.repository.accounts.inbox() + self.repository.backlog.inbox()
        )
        return {"items": entries, "total": len(entries)}

    async def accounts(self) -> dict[str, object]:
        listing = await asyncio.to_thread(self.repository.accounts.listing)
        return {**listing, "providers": provider_catalogue()}

    async def create_account(self, *, name: str, provider: str) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.accounts.create(name=name, provider=provider)
        )

    async def update_account(self, account_id: UUID, **changes: Any) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.accounts.update(account_id, **changes)
        )

    async def sign_in_account(
        self, account_id: UUID, pasted: str, cluster: Cluster | None
    ) -> dict[str, object]:
        """Check what was pasted, hand it to the cluster, then record that it is there."""
        account = await asyncio.to_thread(self.repository.accounts.get, account_id)
        credential = check_credential(str(account["provider"]), pasted)
        if cluster is None:
            raise ProjectInvalid(
                "This installation has no sandbox namespace, so a sign-in cannot be stored"
            )
        await cluster.put_secret(
            secret_name(account_id),
            credential.data,
            {
                "app.kubernetes.io/part-of": "engineering-control",
                "engineering-control/account": str(account_id),
                "engineering-control/provider": str(account["provider"]),
            },
        )
        return await asyncio.to_thread(
            self.repository.accounts.signed_in, account_id, credential.identity
        )

    async def remove_account(self, account_id: UUID, cluster: Cluster | None) -> dict[str, object]:
        """The sign-in leaves the cluster first; the record is removed only when it is gone."""
        await asyncio.to_thread(self.repository.accounts.get, account_id)
        if cluster is not None:
            await cluster.delete_secret(secret_name(account_id))
        return await asyncio.to_thread(self.repository.accounts.archive, account_id)

    async def bulk_items(self, project: str, **options: Any) -> dict[str, object]:
        return await asyncio.to_thread(lambda: self.repository.backlog.bulk(project, **options))

    async def backlog_status(self, project: str) -> dict[str, object] | None:
        return await asyncio.to_thread(self.repository.backlog.sync_status, project)

    async def sync_backlog(self, project: str, github: GitHubApp) -> dict[str, object]:
        """Read the open issues of the project's repository and bring its items in step."""
        repository = await asyncio.to_thread(self.repository.backlog.repository, project)
        try:
            issues, complete = await github.open_issues(repository)
        except GitHubAppError as error:
            await asyncio.to_thread(self.repository.backlog.record_failure, project, str(error))
            raise
        return await asyncio.to_thread(
            lambda: self.repository.backlog.apply_import(project, issues, complete=complete)
        )

    async def active_project_names(self) -> list[str]:
        return await asyncio.to_thread(
            lambda: [record.name for record in self.repository.projects.active()]
        )

    async def create_project(
        self,
        *,
        name: str,
        display_name: str | None,
        repository: str,
        default_branch: str = "main",
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            lambda: self.repository.projects.create(
                name=name,
                display_name=display_name,
                repository=repository,
                default_branch=default_branch,
            )
        )

    async def connected_repositories(self) -> dict[str, str]:
        return await asyncio.to_thread(self.repository.projects.connected_repositories)

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
