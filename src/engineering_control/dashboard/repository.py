from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import create_engine, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.coverage_outcome import coverage_outcome, normalized_coverage_status
from engineering_control.dashboard.auth import PasswordService
from engineering_control.dashboard.observability import DashboardObservabilityProjection
from engineering_control.dashboard.schemas import ActivityEventInput
from engineering_control.dashboard.session_store import (
    SessionAuthorization,
    lock_authentication,
    retire_session,
)
from engineering_control.dashboard.status_mapping import (
    TERMINAL_STATUSES,
    map_status,
)
from engineering_control.dashboard.time_projection import project_duration
from engineering_control.db.models import (
    AgentExecution,
    BacklogIssueRecord,
    Campaign,
    ChangeSet,
    CoverageCell,
    DashboardAuthentication,
    DashboardEvent,
    DashboardSessionRevocation,
    Deployment,
    Enhancement,
    Finding,
    GitHubBacklogCampaign,
    Run,
    SolutionLoop,
    Step,
    VerificationResult,
)
from engineering_control.operations.issue_delivery_approval import (
    expired_unreferenced_reissue_event_ids,
)
from engineering_control.retention_limits import RETAINED_DASHBOARD_EVENT_TYPES
from engineering_control.run_projection import TERMINAL_RUN_OUTCOMES
from engineering_control.security.redaction import SecretRedactor

_EVENT_METADATA_KEYS = frozenset(
    {
        "action",
        "attempt",
        "blocker_code",
        "cell_status",
        "duration_ms",
        "error_code",
        "finding_disposition",
        "operation",
        "provider",
        "role",
        "result",
        "status",
        "step",
        "task_queue",
        "worker_identity",
    }
)
_SESSION_BINDING_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _latest(*values: datetime | None) -> datetime | None:
    present = [
        value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        for value in values
        if value
    ]
    return max(present) if present else None


def _freshness(value: datetime | None, *, stale_seconds: int = 120) -> dict[str, Any]:
    observed = value if value is None or value.tzinfo else value.replace(tzinfo=UTC)
    if observed is None:
        return {"observed_at": None, "freshness": "unknown", "stale": True}
    age = max(0, int((datetime.now(UTC) - observed).total_seconds()))
    return {
        "observed_at": _utc_iso(observed),
        "freshness": "stale" if age > stale_seconds else "fresh",
        "stale": age > stale_seconds,
        "age_seconds": age,
    }


class DashboardRepository:
    def __init__(
        self, database_url: str, *, password_service: PasswordService | None = None
    ) -> None:
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
        )
        self._session_revocation_dialect = self.engine.dialect.name
        if self._session_revocation_dialect not in {"postgresql", "sqlite"}:
            self.engine.dispose()
            raise ValueError("dashboard session revocation database is unsupported")
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.passwords = password_service or PasswordService()
        self.redactor = SecretRedactor()
        self.observability = DashboardObservabilityProjection(self.sessions, self.redactor)

    async def ping(self) -> bool:
        return await asyncio.to_thread(self._ping)

    def _ping(self) -> bool:
        with self.sessions() as session:
            return bool(session.scalar(select(1)) == 1)

    async def ready(self) -> bool:
        return await asyncio.to_thread(self._ready)

    def _ready(self) -> bool:
        with self.sessions() as session:
            initialized = session.get(DashboardAuthentication, 1) is not None
            # Touch the revocation relation even when it is empty so a code-before-
            # migration rollout cannot return a false-green readiness response.
            session.execute(
                select(DashboardSessionRevocation.session_binding_hash).limit(1)
            ).first()
            return initialized

    async def authentication_version(self) -> int | None:
        return await asyncio.to_thread(self._authentication_version)

    def authentication_version_sync(self) -> int | None:
        return self._authentication_version()

    def _authentication_version(self) -> int | None:
        with self.sessions() as session:
            row = session.get(DashboardAuthentication, 1)
            return row.password_version if row else None

    async def session_authorized(self, session_binding_hash: str, *, password_version: int) -> bool:
        return await asyncio.to_thread(
            self._session_authorized,
            session_binding_hash,
            password_version,
        )

    def _session_authorized(self, session_binding_hash: str, password_version: int) -> bool:
        if _SESSION_BINDING_DIGEST.fullmatch(session_binding_hash) is None:
            return False
        now = datetime.now(UTC)
        revoked = (
            select(DashboardSessionRevocation.session_binding_hash)
            .where(
                DashboardSessionRevocation.session_binding_hash == session_binding_hash,
                DashboardSessionRevocation.password_version == password_version,
                DashboardSessionRevocation.expires_at > now,
            )
            .exists()
        )
        with self.sessions() as session:
            return (
                session.scalar(
                    select(DashboardAuthentication.id).where(
                        DashboardAuthentication.id == 1,
                        DashboardAuthentication.password_version == password_version,
                        ~revoked,
                    )
                )
                is not None
            )

    async def revoke_session(
        self,
        session_binding_hash: str,
        *,
        password_version: int,
        expires_at: datetime,
    ) -> None:
        await asyncio.to_thread(
            self._revoke_session,
            session_binding_hash,
            password_version,
            expires_at,
        )

    def _revoke_session(
        self,
        session_binding_hash: str,
        password_version: int,
        expires_at: datetime,
    ) -> None:
        if _SESSION_BINDING_DIGEST.fullmatch(session_binding_hash) is None:
            raise ValueError("dashboard session binding is invalid")
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        with self.sessions.begin() as session:
            lock_authentication(session)
            retire_session(
                session, SessionAuthorization(session_binding_hash, password_version, expires_at)
            )

    async def confirm_login(
        self, password_version: int, previous: SessionAuthorization | None
    ) -> None:
        await asyncio.to_thread(self._confirm_login, password_version, previous)

    def _confirm_login(self, password_version: int, previous: SessionAuthorization | None) -> None:
        with self.sessions.begin() as session:
            row = lock_authentication(session)
            if row.password_version != password_version:
                raise ValueError("dashboard authentication changed; sign in again")
            # A valid password can recover from a lost rotation response even if
            # the presented old cookie was already revoked. Other sessions stay.
            if previous is not None:
                retire_session(session, previous)

    async def verify_password(self, password: str) -> int | None:
        return await asyncio.to_thread(self._verify_password, password)

    def _verify_password(self, password: str) -> int | None:
        with self.sessions() as session:
            row = session.get(DashboardAuthentication, 1)
            password_hash = row.password_hash if row else None
            valid = self.passwords.verify(password_hash, password)
            return row.password_version if valid and row else None

    async def initialize_password(self, password: str) -> int:
        password_hash = await asyncio.to_thread(self.passwords.hash, password)
        return await asyncio.to_thread(self._initialize_password_hash, password_hash)

    def initialize_password_sync(self, password: str) -> int:
        return self._initialize_password_hash(self.passwords.hash(password))

    def _initialize_password_hash(self, password_hash: str) -> int:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(DashboardAuthentication, 1, with_for_update=True)
            if row is not None:
                raise ValueError("dashboard password is already initialized")
            session.add(
                DashboardAuthentication(
                    id=1,
                    password_hash=password_hash,
                    password_version=1,
                    created_at=now,
                    password_changed_at=now,
                )
            )
        return 1

    def reset_password_sync(self, password: str) -> int:
        password_hash = self.passwords.hash(password)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = lock_authentication(session)
            row.password_hash = password_hash
            row.password_version += 1
            row.password_changed_at = now
            return row.password_version

    async def change_password(self, current_password: str, new_password: str) -> int | None:
        self.passwords.validate(new_password)
        new_hash = await asyncio.to_thread(self.passwords.hash, new_password)
        return await asyncio.to_thread(
            self._change_password, current_password, new_password, new_hash
        )

    def _change_password(
        self, current_password: str, new_password: str, new_hash: str
    ) -> int | None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = lock_authentication(session)
            if not self.passwords.verify(row.password_hash, current_password):
                return None
            if self.passwords.verify(row.password_hash, new_password):
                raise ValueError("new password must differ from current password")
            row.password_hash = new_hash
            row.password_version += 1
            row.password_changed_at = now
            return row.password_version

    async def list_backlog(
        self,
        *,
        project: str | None,
        status: str | None,
        search: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._list_backlog,
            project=project,
            status=status,
            search=search,
            page=page,
            page_size=page_size,
        )

    def _list_backlog(
        self,
        *,
        project: str | None,
        status: str | None,
        search: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        with self.sessions() as session:
            ranked_issue_query = select(
                BacklogIssueRecord.id.label("issue_id"),
                func.row_number()
                .over(
                    partition_by=(
                        GitHubBacklogCampaign.project,
                        BacklogIssueRecord.repository,
                        BacklogIssueRecord.issue_number,
                    ),
                    order_by=(
                        GitHubBacklogCampaign.imported_at.desc().nulls_last(),
                        GitHubBacklogCampaign.created_at.desc().nulls_last(),
                        BacklogIssueRecord.updated_at.desc(),
                        BacklogIssueRecord.id.desc(),
                    ),
                )
                .label("campaign_rank"),
            ).join(
                GitHubBacklogCampaign,
                BacklogIssueRecord.backlog_campaign_id == GitHubBacklogCampaign.id,
            )
            if project:
                ranked_issue_query = ranked_issue_query.where(
                    GitHubBacklogCampaign.project == project
                )
            ranked_issues = ranked_issue_query.subquery()
            query = (
                select(BacklogIssueRecord, GitHubBacklogCampaign)
                .join(
                    GitHubBacklogCampaign,
                    BacklogIssueRecord.backlog_campaign_id == GitHubBacklogCampaign.id,
                )
                .join(ranked_issues, ranked_issues.c.issue_id == BacklogIssueRecord.id)
                .where(ranked_issues.c.campaign_rank == 1)
                .order_by(
                    BacklogIssueRecord.updated_at.desc(),
                    BacklogIssueRecord.id.desc(),
                )
                .limit(5000)
            )
            if project:
                query = query.where(GitHubBacklogCampaign.project == project)
            if search:
                needle = f"%{search.lower()}%"
                query = query.where(
                    or_(
                        func.lower(BacklogIssueRecord.title).like(needle),
                        func.cast(BacklogIssueRecord.issue_number, StringType()).like(needle),
                    )
                )
            for issue, campaign in session.execute(query).all():
                change_set = (
                    session.get(ChangeSet, issue.change_set_id) if issue.change_set_id else None
                )
                run = None
                if change_set is not None:
                    run = session.scalar(
                        select(Run)
                        .where(Run.change_set_id == change_set.id)
                        .order_by(Run.updated_at.desc())
                        .limit(1)
                    )
                raw_status = (
                    (run.status if run else None)
                    or (change_set.state if change_set else None)
                    or issue.delivery_status
                    or issue.candidate_status
                )
                workflow_id = run.workflow_id if run else campaign.workflow_id
                context = self._backlog_execution_context(
                    session, workflow_id=workflow_id, run=run, change_set=change_set
                )
                mapped = map_status(
                    raw_status, phase=context["current_phase"], entity_type="backlog"
                )
                normalized = mapped.normalized
                if status and normalized != status:
                    continue
                blocker = None
                if normalized in {"blocked", "failed"}:
                    blocker = (
                        (run.error_code if run else None)
                        or mapped.blocker_code
                        or issue.eligibility_reason
                    )
                last = _latest(
                    issue.updated_at,
                    campaign.updated_at,
                    change_set.updated_at if change_set else None,
                    run.updated_at if run else None,
                )
                records.append(
                    {
                        "backlog_campaign_id": str(campaign.id),
                        "repository": issue.repository,
                        "issue_number": issue.issue_number,
                        "issue_url": self._safe_issue_url(issue.issue_url_reference),
                        "title": issue.title,
                        "project": campaign.project,
                        "issue_state": issue.state,
                        "risk": issue.calculated_risk,
                        "eligibility_status": issue.eligibility_status,
                        "normalized_status": normalized,
                        "raw_status": raw_status,
                        "current_phase": context["current_phase"],
                        "queued_at": _utc_iso(run.created_at if run else campaign.created_at),
                        "started_at": _utc_iso(run.started_at if run else None),
                        "ended_at": _utc_iso(run.finished_at if run else None),
                        "last_activity_at": _utc_iso(last),
                        "blocker": self._sanitize_text(blocker),
                        "workflow_id": workflow_id,
                        "source": "backlog_issue_records/change_sets/runs/agent_executions",
                        **_freshness(last),
                        **context,
                    }
                )
            loop_query = select(SolutionLoop).order_by(SolutionLoop.updated_at.desc()).limit(1000)
            if project:
                loop_query = loop_query.where(SolutionLoop.project == project)
            for loop in session.scalars(loop_query).all():
                state = loop.state_json if isinstance(loop.state_json, dict) else {}
                number = state.get("issue_number")
                title = str(state.get("issue_title") or state.get("title") or "Solution loop")[:300]
                context = self._backlog_execution_context(
                    session, workflow_id=loop.workflow_id, run=None, change_set=None
                )
                context["current_phase"] = (
                    context["current_phase"] or str(state.get("phase") or loop.status)[:120]
                )
                mapped = map_status(
                    loop.status, phase=context["current_phase"], entity_type="solution_loop"
                )
                normalized = mapped.normalized
                if status and normalized != status:
                    continue
                if search and search.lower() not in f"{number or ''} {title}".lower():
                    continue
                if number and any(
                    item["issue_number"] == number and item["project"] == loop.project
                    for item in records
                ):
                    continue
                records.append(
                    {
                        "issue_number": number if isinstance(number, int) and number > 0 else None,
                        "issue_url": self._safe_issue_url(state.get("issue_url")),
                        "title": title,
                        "project": loop.project,
                        "normalized_status": normalized,
                        "raw_status": loop.status,
                        "current_phase": context["current_phase"],
                        "queued_at": _utc_iso(loop.created_at),
                        "started_at": _utc_iso(loop.created_at if normalized != "queued" else None),
                        "ended_at": _utc_iso(
                            loop.updated_at if normalized in TERMINAL_STATUSES else None
                        ),
                        "last_activity_at": _utc_iso(loop.updated_at),
                        "blocker": self._sanitize_text(
                            state.get("blocker") or state.get("last_error") or mapped.blocker_code
                        ),
                        "workflow_id": loop.workflow_id,
                        "source": "solution_loops/agent_executions",
                        **_freshness(loop.updated_at),
                        **context,
                    }
                )
        records.sort(
            key=lambda item: (
                item["normalized_status"] in TERMINAL_STATUSES,
                item["last_activity_at"] or "",
            ),
            reverse=False,
        )
        active = [item for item in records if item["normalized_status"] not in TERMINAL_STATUSES]
        terminal = [item for item in records if item["normalized_status"] in TERMINAL_STATUSES]
        active.sort(key=lambda item: item["last_activity_at"] or "", reverse=True)
        terminal.sort(key=lambda item: item["last_activity_at"] or "", reverse=True)
        records = active + terminal
        total = len(records)
        start = (page - 1) * page_size
        return {
            "items": records[start : start + page_size],
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": max(1, (total + page_size - 1) // page_size),
            "refreshed_at": _utc_iso(datetime.now(UTC)),
        }

    def _backlog_execution_context(
        self,
        session: Session,
        *,
        workflow_id: str | None,
        run: Run | None,
        change_set: ChangeSet | None,
    ) -> dict[str, Any]:
        execution = (
            session.scalar(
                select(AgentExecution)
                .where(AgentExecution.workflow_id == workflow_id)
                .order_by(AgentExecution.updated_at.desc())
                .limit(1)
            )
            if workflow_id
            else None
        )
        steps = (
            list(
                session.scalars(
                    select(Step).where(Step.run_id == run.id).order_by(Step.ordinal)
                ).all()
            )
            if run
            else []
        )
        deployments = (
            list(
                session.scalars(
                    select(Deployment)
                    .where(Deployment.change_set_id == change_set.id)
                    .order_by(Deployment.updated_at.desc())
                    .limit(1)
                ).all()
            )
            if change_set
            else []
        )
        verification = (
            session.scalar(
                select(VerificationResult)
                .where(VerificationResult.run_id == run.id)
                .order_by(VerificationResult.updated_at.desc())
                .limit(1)
            )
            if run
            else None
        )
        start = execution.started_at if execution else run.started_at if run else None
        end = execution.ended_at if execution else run.finished_at if run else None
        now = datetime.now(UTC)
        selected_status = execution.status if execution else run.status if run else "UNKNOWN"
        timing = project_duration(
            start,
            end,
            running=(
                selected_status.upper() not in TERMINAL_RUN_OUTCOMES
                and map_status(selected_status).normalized not in TERMINAL_STATUSES
            ),
            now=now,
        )
        recovery = change_set.recovery_json if change_set else {}
        recovery = recovery if isinstance(recovery, dict) else {}
        return {
            "active_provider": execution.provider if execution else None,
            "provider_role": execution.role if execution else None,
            "worker": execution.worker_kind if execution else run.worker if run else None,
            "last_heartbeat": _utc_iso(execution.last_heartbeat_at if execution else None),
            **timing,
            "attempt": execution.attempt if execution else 1,
            "max_attempts": execution.max_attempts if execution else 1,
            "execution_id": str(execution.id) if execution else None,
            "current_phase": (
                execution.phase
                if execution and execution.phase
                else steps[-1].name
                if steps
                else change_set.state
                if change_set
                else None
            ),
            "pr_state": "linked" if change_set and change_set.source_pr_url else None,
            "ci_state": recovery.get("ci_status"),
            "build_state": recovery.get("build_status")
            or ("artifact recorded" if change_set and change_set.chart_version else None),
            "deployment_state": deployments[0].status if deployments else None,
            "verification_state": verification.status if verification else None,
            "timeline": [
                {
                    "phase": step.name,
                    "status": map_status(
                        step.status, phase=step.name, entity_type="step"
                    ).normalized,
                }
                for step in steps
            ],
        }

    async def list_audits(
        self, *, project: str | None, status: str | None, limit: int = 200
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._list_audits, project, status, limit)

    def _list_audits(self, project: str | None, status: str | None, limit: int) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        with self.sessions() as session:
            query = select(Campaign).order_by(Campaign.updated_at.desc()).limit(limit)
            if project:
                query = query.where(Campaign.project == project)
            for campaign in session.scalars(query).all():
                campaign_mapping = map_status(campaign.status, entity_type="campaign")
                normalized = campaign_mapping.normalized
                cells = session.scalars(
                    select(CoverageCell)
                    .where(CoverageCell.campaign_id == campaign.id)
                    .order_by(CoverageCell.updated_at.desc())
                ).all()
                runs = session.scalars(
                    select(Run)
                    .where(Run.campaign_id == campaign.id)
                    .order_by(Run.updated_at.desc())
                ).all()
                findings = session.scalars(
                    select(Finding).where(Finding.campaign_id == campaign.id)
                ).all()
                statuses = [cell.status.upper() for cell in cells]
                outcome = coverage_outcome(statuses)
                normalized = normalized_coverage_status(normalized, outcome)
                if status and normalized != status:
                    continue
                passed = sum(
                    value in {"NO_FINDING", "VERIFIED_FIXED", "PASS", "PASSED"}
                    for value in statuses
                )
                blocked = statuses.count("BLOCKED")
                failed = sum(value in {"FAILED", "ERROR", "TIMED_OUT"} for value in statuses)
                skipped = sum(value in {"SKIPPED", "EXCLUDED"} for value in statuses)
                running = sum(
                    value in {"IN_PROGRESS", "RETEST_REQUIRED", "RUNNING"} for value in statuses
                )
                queued = sum(value in {"NOT_STARTED", "QUEUED", "PENDING"} for value in statuses)
                terminal = (
                    passed
                    + blocked
                    + failed
                    + skipped
                    + sum(value in {"CANDIDATE_FOUND", "CONFIRMED_FINDING"} for value in statuses)
                )
                total = len(cells)
                completion = round((terminal / total) * 100, 1) if total else 0.0
                current = next(
                    (
                        cell
                        for cell in cells
                        if cell.status.upper()
                        not in {
                            "NO_FINDING",
                            "VERIFIED_FIXED",
                            "PASS",
                            "PASSED",
                            "BLOCKED",
                            "FAILED",
                            "ERROR",
                            "TIMED_OUT",
                            "SKIPPED",
                            "EXCLUDED",
                            "CANDIDATE_FOUND",
                            "CONFIRMED_FINDING",
                        }
                    ),
                    cells[0] if cells else None,
                )
                run = runs[0] if runs else None
                metadata = (
                    campaign.metadata_json if isinstance(campaign.metadata_json, dict) else {}
                )
                raw_root_values = metadata.get("root_blockers")
                root_values = (
                    cast(dict[str, Any], raw_root_values)
                    if isinstance(raw_root_values, dict)
                    else {}
                )
                root_codes = {
                    str(value.get("code") or identity)
                    for identity, value in root_values.items()
                    if isinstance(value, dict)
                }
                blocked_by_root = sum(
                    cell.status.upper() == "BLOCKED" and str(cell.blocker) in root_codes
                    for cell in cells
                )
                current_result = (
                    current.result_json
                    if current is not None and isinstance(current.result_json, dict)
                    else {}
                )
                execution = session.scalar(
                    select(AgentExecution)
                    .where(
                        AgentExecution.campaign_id == campaign.id,
                        AgentExecution.workflow_id == (run.workflow_id if run else ""),
                    )
                    .order_by(AgentExecution.updated_at.desc())
                    .limit(1)
                )
                blocker = next((cell.blocker for cell in cells if cell.blocker), None)
                last = _latest(
                    campaign.updated_at,
                    *(cell.updated_at for cell in cells[:20]),
                    *(value.updated_at for value in runs[:20]),
                )
                items.append(
                    {
                        "campaign_id": str(campaign.id),
                        "workflow_id": run.workflow_id if run else None,
                        "project": campaign.project,
                        "name": campaign.name,
                        "preset": metadata.get("preset") or campaign.name,
                        "environment": metadata.get("environment")
                        or (current.environment if current else None),
                        "raw_status": campaign.status,
                        "execution_status": campaign_mapping.normalized,
                        "coverage_outcome": outcome,
                        "normalized_status": normalized,
                        "created_at": _utc_iso(campaign.created_at),
                        "queued_at": _utc_iso(run.created_at if run else campaign.created_at),
                        "started_at": _utc_iso(run.started_at if run else None),
                        "ended_at": _utc_iso(run.finished_at if run else None),
                        "total_cells": total,
                        "queued_cells": queued,
                        "running_cells": running,
                        "passed_cells": passed,
                        "blocked_cells": blocked,
                        "blocked_by_root": blocked_by_root,
                        "independently_blocked": max(0, blocked - blocked_by_root),
                        "runnable_cells": max(0, total - skipped),
                        "failed_cells": failed,
                        "skipped_cells": skipped,
                        "completion_percentage": completion,
                        "repository_surface": current.repository_surface if current else None,
                        "domain": current.domain if current else None,
                        "persona": current.persona if current else None,
                        "flow": current.flow if current else None,
                        "lens": current.lens if current else None,
                        "route_operation": current_result.get("route")
                        or current_result.get("operation"),
                        "provider": execution.provider if execution else None,
                        "provider_role": execution.role if execution else None,
                        "worker": execution.worker_kind
                        if execution
                        else (run.worker if run else None),
                        "execution_id": str(execution.id) if execution else None,
                        "cell_started_at": _utc_iso(current.created_at if current else None),
                        "cell_ended_at": _utc_iso(
                            current.updated_at
                            if current
                            and current.status.upper()
                            in {
                                "NO_FINDING",
                                "VERIFIED_FIXED",
                                "PASS",
                                "PASSED",
                                "BLOCKED",
                                "FAILED",
                                "ERROR",
                                "TIMED_OUT",
                                "SKIPPED",
                                "EXCLUDED",
                                "CANDIDATE_FOUND",
                                "CONFIRMED_FINDING",
                            }
                            else None
                        ),
                        "cell_result": current.status if current else None,
                        "persona_lifecycle": self._safe_persona_lifecycle(current_result),
                        "root_blockers": [
                            {
                                "code": str(code)[:120],
                                "classification": (
                                    value.get("classification") if isinstance(value, dict) else None
                                ),
                                "summary": self._sanitize_text(
                                    value.get("summary") if isinstance(value, dict) else code
                                ),
                                "dependent_cell_count": sum(
                                    str(cell.blocker) == str(code) for cell in cells
                                ),
                                "resolution_state": (
                                    value.get("resolution_state")
                                    if isinstance(value, dict)
                                    else "open"
                                ),
                            }
                            for identity, value in root_values.items()
                            for code in [
                                str(value.get("code") or identity)
                                if isinstance(value, dict)
                                else str(identity)
                            ]
                        ],
                        "candidate_findings": sum(
                            value.disposition == "CANDIDATE" for value in findings
                        ),
                        "confirmed_findings": sum(
                            value.disposition == "CONFIRMED" for value in findings
                        ),
                        "published_findings": sum(
                            value.disposition in {"PUBLISHED", "VERIFIED_PUBLISHED"}
                            for value in findings
                        ),
                        "blocker": self._sanitize_text(
                            blocker
                            or (run.error_code if run else None)
                            or campaign_mapping.blocker_code
                        ),
                        "last_activity_at": _utc_iso(last),
                        "source": "campaigns/coverage_cells/runs/findings/agent_executions",
                        **_freshness(last),
                    }
                )
        active = [item for item in items if item["normalized_status"] not in TERMINAL_STATUSES]
        terminal_items = [item for item in items if item["normalized_status"] in TERMINAL_STATUSES]
        active.sort(key=lambda item: item["last_activity_at"] or "", reverse=True)
        terminal_items.sort(key=lambda item: item["last_activity_at"] or "", reverse=True)
        return {
            "items": active + terminal_items,
            "refreshed_at": _utc_iso(datetime.now(UTC)),
        }

    @staticmethod
    def _safe_persona_lifecycle(result: dict[str, Any]) -> dict[str, str] | None:
        raw = result.get("persona_lifecycle")
        if not isinstance(raw, dict):
            return None
        allowed = {"requested", "activated", "authenticated", "cleaned", "restored", "cleanup"}
        return {
            str(key): str(value)[:80]
            for key, value in raw.items()
            if key in allowed and isinstance(value, (str, bool, int))
        }

    async def record_event(self, event: ActivityEventInput) -> int:
        return await asyncio.to_thread(self._record_event, event)

    def record_event_sync(self, event: ActivityEventInput) -> int:
        return self._record_event(event)

    def _record_event(self, event: ActivityEventInput) -> int:
        metadata = self._safe_event_metadata(event.metadata)
        message = self._sanitize_text(event.message) or "Activity recorded"
        with self.sessions.begin() as session:
            row = DashboardEvent(
                occurred_at=event.occurred_at or datetime.now(UTC),
                level=event.level,
                event_type=event.event_type,
                source=event.source,
                host=event.host,
                worker_kind=event.worker_kind,
                project=event.project,
                workflow_id=event.workflow_id,
                temporal_run_id=event.temporal_run_id,
                issue_number=event.issue_number,
                campaign_id=event.campaign_id,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                current_phase=event.current_phase,
                message=message[:500],
                metadata_json=metadata,
            )
            session.add(row)
            session.flush()
            return row.id

    async def recent_events(
        self, *, limit: int, after_id: int | None = None
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._recent_events, limit, after_id)

    def _recent_events(self, limit: int, after_id: int | None) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(DashboardEvent)
            if after_id is not None:
                query = (
                    query.where(DashboardEvent.id > after_id)
                    .order_by(DashboardEvent.id)
                    .limit(limit)
                )
            else:
                query = query.order_by(DashboardEvent.id.desc()).limit(limit)
            rows = list(session.scalars(query).all())
            if after_id is None:
                rows.reverse()
            return [self._serialize_event(row) for row in rows]

    async def delete_expired_events(self, *, retention_days: int) -> int:
        return await asyncio.to_thread(self._delete_expired_events, retention_days)

    def _delete_expired_events(self, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        with self.sessions.begin() as session:
            rows = list(
                session.scalars(
                    select(DashboardEvent)
                    .where(
                        DashboardEvent.occurred_at < cutoff,
                        DashboardEvent.event_type.not_in(RETAINED_DASHBOARD_EVENT_TYPES),
                    )
                    .limit(10_000)
                ).all()
            )
            # NEW-144 review 3: plus expired re-issue events no custody references
            # (filtered in Python; never a SQL id list).
            for event_id in expired_unreferenced_reissue_event_ids(
                session, cutoff, limit=max(0, 10_000 - len(rows))
            ):
                stale = session.get(DashboardEvent, event_id)
                if stale is not None:
                    rows.append(stale)
            for row in rows:
                session.delete(row)
            return len(rows)

    @staticmethod
    def _serialize_event(row: DashboardEvent) -> dict[str, Any]:
        return {
            "id": row.id,
            "timestamp": _utc_iso(row.occurred_at),
            "level": row.level,
            "event_type": row.event_type,
            "source": row.source,
            "host": row.host,
            "worker_kind": row.worker_kind,
            "project": row.project,
            "workflow_id": row.workflow_id,
            "temporal_run_id": row.temporal_run_id,
            "issue_number": row.issue_number,
            "campaign_id": row.campaign_id,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "current_phase": row.current_phase,
            "message": row.message,
            "metadata": row.metadata_json,
        }

    def _safe_event_metadata(self, value: dict[str, Any]) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if key not in _EVENT_METADATA_KEYS:
                continue
            if isinstance(item, (str, int, float, bool)) or item is None:
                safe[key] = item[:240] if isinstance(item, str) else item
        redacted = cast(dict[str, Any], self.redactor.redact(safe).value)
        while len(json.dumps(redacted, separators=(",", ":"), default=str)) > 4096 and redacted:
            redacted.pop(next(reversed(redacted)))
        return redacted

    def _sanitize_text(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(self.redactor.redact_text(str(value)).value)
        text = " ".join(text.split())
        return text[:500] or None

    @staticmethod
    def _safe_issue_url(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        if not value.startswith("https://github.com/") or "/issues/" not in value:
            return None
        return value[:500]

    async def list_findings_view(self, *, project: str | None, limit: int) -> dict[str, object]:
        return await asyncio.to_thread(self._list_findings_view, project=project, limit=limit)

    def _list_findings_view(self, *, project: str | None, limit: int) -> dict[str, object]:
        with self.sessions() as session:
            query = select(Finding)
            if project is not None:
                query = query.where(Finding.project == project)
            rows = session.scalars(query.order_by(Finding.created_at.desc()).limit(limit)).all()
            return {
                "items": [
                    {
                        "finding_id": str(row.id),
                        "campaign_id": str(row.campaign_id),
                        "project": row.project,
                        "domain": row.domain,
                        "persona": row.persona,
                        "severity": row.severity,
                        "confidence": row.confidence,
                        "risk_class": row.risk_class,
                        "disposition": row.disposition,
                        "affected_component": self._sanitize_text(row.affected_component),
                        "recommended_action": self._sanitize_text(row.recommended_action),
                        "created_at": _utc_iso(row.created_at),
                        "updated_at": _utc_iso(row.updated_at),
                    }
                    for row in rows
                ],
                "observed_at": _utc_iso(datetime.now(UTC)),
                "source": "persisted-findings-projection",
            }

    async def list_enhancements_view(self, *, project: str | None, limit: int) -> dict[str, object]:
        return await asyncio.to_thread(self._list_enhancements_view, project=project, limit=limit)

    def _list_enhancements_view(self, *, project: str | None, limit: int) -> dict[str, object]:
        with self.sessions() as session:
            query = select(Enhancement)
            if project is not None:
                query = query.where(Enhancement.project == project)
            rows = session.scalars(query.order_by(Enhancement.created_at.desc()).limit(limit)).all()
            return {
                "items": [
                    {
                        "enhancement_id": row.enhancement_key,
                        "project": row.project,
                        "campaign_id": row.campaign_id,
                        "persona": row.persona,
                        "affected_repository": row.affected_repository,
                        "risk_class": row.risk_class,
                        "status": row.status,
                        "summary": self._sanitize_text(
                            row.payload_json.get("summary")
                            or row.payload_json.get("title")
                            or row.payload_json.get("description")
                        ),
                        "created_at": _utc_iso(row.created_at),
                        "updated_at": _utc_iso(row.updated_at),
                    }
                    for row in rows
                ],
                "observed_at": _utc_iso(datetime.now(UTC)),
                "source": "persisted-enhancements-projection",
            }


def StringType() -> Any:
    from sqlalchemy import String

    return String()
