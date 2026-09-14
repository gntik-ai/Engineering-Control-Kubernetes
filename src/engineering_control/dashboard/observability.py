from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.status_mapping import (
    TERMINAL_STATUSES,
    expected_next_step,
    map_status,
)
from engineering_control.db.models import (
    AgentExecution,
    AgentLogChunk,
    BacklogIssueRecord,
    Campaign,
    ChangeSet,
    CoverageCell,
    DashboardEvent,
    Deployment,
    GitHubBacklogCampaign,
    Issue,
    QuotaSnapshot,
    Run,
    Step,
    VerificationResult,
    WorkerHeartbeat,
)
from engineering_control.operations.connection_status import (
    PROVIDER_FRESHNESS_SECONDS,
    provider_connection_status,
)
from engineering_control.run_projection import classify_run_projection
from engineering_control.security.redaction import SecretRedactor


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def current_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def latest_time(*values: datetime | None) -> datetime | None:
    present = [current for value in values if (current := current_utc(value)) is not None]
    return max(present) if present else None


def duration_seconds(start: datetime | None, end: datetime | None) -> int:
    normalized_start = current_utc(start)
    normalized_end = current_utc(end)
    if normalized_start is None or normalized_end is None:
        return 0
    return max(0, int((normalized_end - normalized_start).total_seconds()))


def is_at_or_after(value: datetime | None, cutoff: datetime) -> bool:
    normalized = current_utc(value)
    return normalized is not None and normalized >= cutoff


def freshness(value: datetime | None, *, stale_seconds: int = 120) -> dict[str, Any]:
    observed = current_utc(value)
    if observed is None:
        return {"observed_at": None, "freshness": "unknown", "stale": True}
    age = max(0, int((datetime.now(UTC) - observed).total_seconds()))
    return {
        "observed_at": utc_iso(observed),
        "freshness": "stale" if age > stale_seconds else "fresh",
        "stale": age > stale_seconds,
        "age_seconds": age,
    }


def encode_cursor(identity: str, observed_at: str | None) -> str:
    raw = json.dumps(
        {"id": identity, "observed_at": observed_at},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_cursor(value: str | None) -> tuple[str, str | None] | None:
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid cursor") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
        raise ValueError("invalid cursor")
    observed = payload.get("observed_at")
    if observed is not None and not isinstance(observed, str):
        raise ValueError("invalid cursor")
    return payload["id"], observed


class DashboardObservabilityProjection:
    """Bounded deterministic projections for overview, workflows, executions, and health."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        redactor: SecretRedactor,
        *,
        stale_seconds: int = 120,
    ) -> None:
        self.sessions = sessions
        self.redactor = redactor
        self.stale_seconds = stale_seconds

    def overview(self) -> dict[str, Any]:
        with self.sessions() as session:
            runs = list(
                session.scalars(select(Run).order_by(Run.updated_at.desc()).limit(5000)).all()
            )
            serialized = [self._workflow(session, row, include_timeline=False) for row in runs]
            active = [
                item for item in serialized if item["normalized_status"] not in TERMINAL_STATUSES
            ]
            failed_cutoff = datetime.now(UTC) - timedelta(hours=24)
            active_campaigns = session.scalar(
                select(func.count())
                .select_from(Campaign)
                .where(~Campaign.status.in_(("COMPLETE", "COMPLETED", "FAILED", "CANCELLED")))
            )
            # Rank every membership before filtering: an older open/pending
            # snapshot must not reappear after a newer closed/terminal one.
            latest_memberships = (
                select(
                    BacklogIssueRecord.id.label("issue_id"),
                    func.count()
                    .over(
                        partition_by=(
                            BacklogIssueRecord.repository,
                            BacklogIssueRecord.issue_number,
                            GitHubBacklogCampaign.imported_at,
                        )
                    )
                    .label("same_timestamp_count"),
                    func.row_number()
                    .over(
                        partition_by=(
                            BacklogIssueRecord.repository,
                            BacklogIssueRecord.issue_number,
                        ),
                        order_by=(
                            GitHubBacklogCampaign.imported_at.desc(),
                            GitHubBacklogCampaign.id.desc(),
                        ),
                    )
                    .label("membership_rank"),
                )
                .join(
                    GitHubBacklogCampaign,
                    BacklogIssueRecord.backlog_campaign_id == GitHubBacklogCampaign.id,
                )
                .subquery()
            )
            inventory = (
                select(BacklogIssueRecord, GitHubBacklogCampaign)
                .join(
                    GitHubBacklogCampaign,
                    BacklogIssueRecord.backlog_campaign_id == GitHubBacklogCampaign.id,
                )
                .join(latest_memberships, latest_memberships.c.issue_id == BacklogIssueRecord.id)
                .where(
                    latest_memberships.c.membership_rank == 1,
                    latest_memberships.c.same_timestamp_count == 1,
                    func.upper(BacklogIssueRecord.state) == "OPEN",
                    BacklogIssueRecord.delivery_status.in_(("NOT_STARTED", "QUEUED")),
                    BacklogIssueRecord.final_closure_status == "OPEN",
                    BacklogIssueRecord.change_set_id.is_(None),
                    GitHubBacklogCampaign.status.in_(
                        (
                            "IMPORTED",
                            "QUEUED",
                            "AWAITING_APPROVAL",
                            "APPROVED_INVENTORY",
                            "APPROVED_POLICY_PENDING",
                            "APPROVED",
                            "RUNNING",
                            "PAUSED",
                            "BLOCKED",
                        )
                    ),
                    BacklogIssueRecord.eligibility_status.in_(
                        ("ELIGIBLE", "ELIGIBLE_FOR_REVALIDATION")
                    ),
                )
            )
            pending_inventory_count = session.scalar(
                select(func.count()).select_from(inventory.subquery())
            )
            ambiguous_inventory_count = session.scalar(
                select(func.count())
                .select_from(latest_memberships)
                .where(
                    latest_memberships.c.membership_rank == 1,
                    latest_memberships.c.same_timestamp_count > 1,
                )
            )
            next_issue = session.execute(
                inventory.order_by(
                    BacklogIssueRecord.planned_wave,
                    BacklogIssueRecord.planned_order,
                    BacklogIssueRecord.repository,
                    BacklogIssueRecord.issue_number,
                ).limit(1)
            ).first()
            services = self._services(session)
            active_work = []
            for item in active[:50]:
                serialized_item = dict(item)
                serialized_item.pop("updated_datetime", None)
                active_work.append(serialized_item)
            return {
                "summary": {
                    "active_workflows": len(active),
                    "pending_inventory_count": int(pending_inventory_count or 0),
                    "ambiguous_inventory_count": int(ambiguous_inventory_count or 0),
                    "blocked_workflows": sum(
                        item["normalized_status"] == "blocked" for item in serialized
                    ),
                    "failed_24h": sum(
                        item["normalized_status"] == "failed"
                        and is_at_or_after(item["updated_datetime"], failed_cutoff)
                        for item in serialized
                    ),
                    "active_audits": int(active_campaigns or 0),
                },
                "active_work": active_work,
                "idle": len(active) == 0,
                "next_inventory_candidate": (
                    {
                        "project": next_issue[1].project,
                        "repository": next_issue[0].repository,
                        "issue_number": next_issue[0].issue_number,
                        "title": self.redactor.redact_text(next_issue[0].title).value,
                        "issue_url": self._safe_issue_url(next_issue[0].issue_url_reference),
                        "campaign_id": str(next_issue[1].id),
                        "campaign_status": next_issue[1].status,
                        "inventory_observed_at": utc_iso(next_issue[1].imported_at),
                        "delivery_status": next_issue[0].delivery_status,
                        "eligibility_status": next_issue[0].eligibility_status,
                        "authorization": "NOT_EVALUATED",
                    }
                    if next_issue
                    else None
                ),
                "inventory_semantics": (
                    "Latest recorded membership per repository and issue; not a dispatch queue "
                    "or an approved next task. Current issue state and approval "
                    "require revalidation. Equal latest snapshot times are ambiguous and excluded."
                ),
                "services": services["services"],
                "providers": services["providers"],
                "source": "runs/backlog_issue_records/campaigns/worker_heartbeats/quota_snapshots",
                **freshness(datetime.now(UTC), stale_seconds=self.stale_seconds),
            }

    def list_workflows(
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
    ) -> dict[str, Any]:
        with self.sessions() as session:
            query = select(Run).order_by(Run.updated_at.desc()).limit(5000)
            if project:
                query = query.where(Run.project == project)
            if workflow_type:
                query = query.where(Run.run_type == workflow_type)
            if date_from:
                query = query.where(Run.created_at >= date_from)
            if date_to:
                query = query.where(Run.created_at <= date_to)
            if search:
                needle = f"%{search.lower()}%"
                query = query.where(
                    or_(
                        func.lower(Run.workflow_id).like(needle),
                        func.lower(Run.project).like(needle),
                        func.lower(Run.run_type).like(needle),
                        func.lower(func.coalesce(Run.summary, "")).like(needle),
                    )
                )
            rows = list(session.scalars(query).all())
            items = [self._workflow(session, row, include_timeline=False) for row in rows]
            if status:
                items = [item for item in items if item["normalized_status"] == status]
            if provider:
                items = [item for item in items if item["active_provider"] == provider]
            if role:
                items = [item for item in items if item["active_role"] == role]
            if worker:
                items = [item for item in items if item["worker"] == worker]
            items.sort(
                key=lambda item: (
                    item["normalized_status"] in TERMINAL_STATUSES,
                    -item["updated_datetime"].timestamp(),
                    item["workflow_id"],
                )
            )
            page = self._cursor_page(
                items,
                cursor,
                limit,
                identity_key="workflow_id",
                observed_key="updated_at",
            )
            for item in page["items"]:
                item.pop("updated_datetime", None)
            return {
                **page,
                "source": "runs/steps/agent_executions",
                "observed_at": utc_iso(datetime.now(UTC)),
            }

    def workflow(self, workflow_id: str) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.scalar(
                select(Run)
                .where(Run.workflow_id == workflow_id)
                .order_by(Run.updated_at.desc())
                .limit(1)
            )
            if row is None:
                return None
            item = self._workflow(session, row, include_timeline=True)
            item.pop("updated_datetime", None)
            return item

    def list_executions(
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
    ) -> dict[str, Any]:
        with self.sessions() as session:
            query = select(AgentExecution).order_by(AgentExecution.updated_at.desc()).limit(5000)
            if project:
                query = query.where(AgentExecution.project == project)
            if workflow_id:
                query = query.where(AgentExecution.workflow_id == workflow_id)
            if issue_number:
                query = query.where(AgentExecution.issue_number == issue_number)
            if campaign_id:
                query = query.where(AgentExecution.campaign_id == campaign_id)
            if provider:
                query = query.where(AgentExecution.provider == provider)
            if role:
                query = query.where(AgentExecution.role == role)
            if worker:
                query = query.where(AgentExecution.worker_kind == worker)
            if host:
                query = query.where(AgentExecution.host == host)
            if phase:
                query = query.where(AgentExecution.phase == phase)
            if date_from:
                query = query.where(AgentExecution.created_at >= date_from)
            if date_to:
                query = query.where(AgentExecution.created_at <= date_to)
            if search:
                needle = f"%{search.lower()}%"
                query = query.where(
                    or_(
                        func.lower(AgentExecution.workflow_id).like(needle),
                        func.lower(AgentExecution.role).like(needle),
                        func.lower(func.coalesce(AgentExecution.phase, "")).like(needle),
                        func.lower(func.coalesce(AgentExecution.blocker_code, "")).like(needle),
                    )
                )
            items = [self._execution(row) for row in session.scalars(query).all()]
            if status:
                items = [item for item in items if item["normalized_status"] == status]
            items.sort(
                key=lambda item: (
                    item["normalized_status"] in TERMINAL_STATUSES,
                    -item["updated_datetime"].timestamp(),
                    item["execution_id"],
                )
            )
            page = self._cursor_page(
                items,
                cursor,
                limit,
                identity_key="execution_id",
                observed_key="updated_at",
            )
            for item in page["items"]:
                item.pop("updated_datetime", None)
            return {
                **page,
                "source": "agent_executions",
                "observed_at": utc_iso(datetime.now(UTC)),
            }

    def execution(self, execution_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(AgentExecution, execution_id)
            if row is None:
                return None
            item = self._execution(row)
            item.pop("updated_datetime", None)
            item["workflow_url"] = f"/workflows/{row.workflow_id}"
            return item

    def execution_logs(
        self,
        execution_id: UUID,
        *,
        limit: int,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
    ) -> dict[str, Any] | None:
        with self.sessions() as session:
            execution = session.get(AgentExecution, execution_id)
            if execution is None:
                return None
            query = select(AgentLogChunk).where(AgentLogChunk.execution_id == execution_id)
            if after_sequence is not None:
                query = (
                    query.where(AgentLogChunk.sequence > after_sequence)
                    .order_by(AgentLogChunk.sequence)
                    .limit(limit)
                )
                rows = list(session.scalars(query).all())
            else:
                if before_sequence is not None:
                    query = query.where(AgentLogChunk.sequence < before_sequence)
                rows = list(
                    session.scalars(
                        query.order_by(AgentLogChunk.sequence.desc()).limit(limit)
                    ).all()
                )
                rows.reverse()
            return {
                "items": [self._log(row) for row in rows],
                "before_cursor": rows[0].sequence if rows else None,
                "last_sequence": rows[-1].sequence if rows else (after_sequence or 0),
                "source": "agent_log_chunks",
                "observed_at": utc_iso(datetime.now(UTC)),
            }

    def services(self) -> dict[str, Any]:
        with self.sessions() as session:
            return self._services(session)

    def root_blockers(
        self,
        *,
        project: str | None = None,
        campaign_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        with self.sessions() as session:
            query = select(Campaign).order_by(Campaign.updated_at.desc()).limit(500)
            if project:
                query = query.where(Campaign.project == project)
            if campaign_id:
                query = query.where(Campaign.id == campaign_id)
            items: list[dict[str, Any]] = []
            for campaign in session.scalars(query).all():
                metadata = (
                    campaign.metadata_json if isinstance(campaign.metadata_json, dict) else {}
                )
                raw_roots = metadata.get("root_blockers")
                roots = raw_roots if isinstance(raw_roots, dict) else {}
                cells = list(
                    session.scalars(
                        select(CoverageCell).where(CoverageCell.campaign_id == campaign.id)
                    ).all()
                )
                for identity, raw in roots.items():
                    value = raw if isinstance(raw, dict) else {}
                    code = str(value.get("code") or identity)[:120]
                    affected = [
                        cell
                        for cell in cells
                        if cell.blocker == code
                        or (
                            isinstance(cell.result_json, dict)
                            and isinstance(cell.result_json.get("root_blocker"), dict)
                            and cell.result_json["root_blocker"].get("root_blocker_id") == identity
                        )
                    ]
                    items.append(
                        {
                            "campaign_id": str(campaign.id),
                            "project": campaign.project,
                            "blocker_id": value.get("root_blocker_id")
                            or (identity if str(identity).startswith("rb-") else None),
                            "code": code,
                            "classification": value.get("classification"),
                            "summary": self._sanitize(
                                value.get("summary") or code.replace("_", " ").capitalize()
                            ),
                            "affected_personas": sorted({cell.persona for cell in affected}),
                            "affected_flows": sorted({cell.flow for cell in affected}),
                            "affected_domains": sorted({cell.domain for cell in affected}),
                            "dependent_cell_count": len(affected),
                            "first_occurrence": utc_iso(
                                min((cell.created_at for cell in affected), default=None)
                            ),
                            "last_occurrence": utc_iso(
                                max((cell.updated_at for cell in affected), default=None)
                            ),
                            "owner": value.get("owner"),
                            "related_finding": value.get("root_finding_id"),
                            "related_issue": value.get("issue_number"),
                            "resolution_state": value.get("resolution_state") or "open",
                        }
                    )
            items.sort(key=lambda item: (item["project"], item["code"], item["campaign_id"]))
            start = 0
            decoded = decode_cursor(cursor)
            if decoded is not None:
                cursor_id, _ = decoded
                for index, item in enumerate(items):
                    if f"{item['campaign_id']}:{item['code']}" == cursor_id:
                        start = index + 1
                        break
                else:
                    raise ValueError("cursor no longer identifies a result")
            selected = items[start : start + limit]
            next_cursor = (
                encode_cursor(f"{selected[-1]['campaign_id']}:{selected[-1]['code']}", None)
                if selected and start + limit < len(items)
                else None
            )
            return {
                "items": selected,
                "next_cursor": next_cursor,
                "page_size": limit,
                "source": "campaigns.metadata_json.root_blockers/coverage_cells",
                "observed_at": utc_iso(datetime.now(UTC)),
            }

    def _workflow(self, session: Session, run: Run, *, include_timeline: bool) -> dict[str, Any]:
        steps = list(
            session.scalars(
                select(Step).where(Step.run_id == run.id).order_by(Step.ordinal, Step.created_at)
            ).all()
        )
        executions = list(
            session.scalars(
                select(AgentExecution)
                .where(AgentExecution.workflow_id == run.workflow_id)
                .order_by(AgentExecution.updated_at.desc())
                .limit(100)
            ).all()
        )
        active_execution = next(
            (
                value
                for value in executions
                if map_status(value.status, phase=value.phase, entity_type="execution").normalized
                not in TERMINAL_STATUSES
            ),
            executions[0] if executions else None,
        )
        active_step = next(
            (
                value
                for value in reversed(steps)
                if map_status(value.status, phase=value.name, entity_type="step").normalized
                not in TERMINAL_STATUSES
            ),
            steps[-1] if steps else None,
        )
        events = list(
            session.scalars(
                select(DashboardEvent)
                .where(DashboardEvent.workflow_id == run.workflow_id)
                .order_by(DashboardEvent.id.desc())
                .limit(200)
            ).all()
        )
        event_phase = next(
            (event.current_phase for event in events if event.current_phase),
            None,
        )
        phase = (
            active_execution.phase
            if active_execution
            else active_step.name
            if active_step
            else event_phase
            if event_phase
            else run.status
        )
        mapped = map_status(run.status, phase=phase, entity_type=run.run_type)
        change_set = session.get(ChangeSet, run.change_set_id) if run.change_set_id else None
        issue = (
            session.get(Issue, change_set.issue_id)
            if change_set is not None and change_set.issue_id is not None
            else None
        )
        campaign = session.get(Campaign, run.campaign_id) if run.campaign_id else None
        deployments = (
            list(
                session.scalars(
                    select(Deployment)
                    .where(Deployment.change_set_id == change_set.id)
                    .order_by(Deployment.updated_at.desc())
                    .limit(20)
                ).all()
            )
            if change_set
            else []
        )
        verifications = list(
            session.scalars(
                select(VerificationResult)
                .where(VerificationResult.run_id == run.id)
                .order_by(VerificationResult.updated_at.desc())
                .limit(20)
            ).all()
        )
        temporal_run_id = next(
            (
                value
                for value in (
                    active_execution.temporal_run_id if active_execution else None,
                    *(event.temporal_run_id for event in events),
                )
                if value
            ),
            None,
        )
        last_activity = latest_time(
            run.updated_at,
            active_execution.last_heartbeat_at if active_execution else None,
            active_step.updated_at if active_step else None,
            events[0].occurred_at if events else None,
        )
        projection_state = classify_run_projection(
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            updated_at=run.updated_at,
            now=datetime.now(UTC),
        )
        normalized_status = (
            "blocked" if projection_state == "ORPHANED_START_INTENT" else mapped.normalized
        )
        blocker_code = (
            run.error_code
            or ("ORPHANED_START_INTENT" if projection_state == "ORPHANED_START_INTENT" else None)
            or (active_execution.blocker_code if active_execution else None)
            or mapped.blocker_code
        )
        start = run.started_at or run.created_at
        end = run.finished_at or datetime.now(UTC)
        item: dict[str, Any] = {
            "workflow_id": run.workflow_id,
            "temporal_run_id": temporal_run_id,
            "run_id": str(run.id),
            "project": run.project,
            "repository": (
                issue.repository if issue else change_set.source_repository if change_set else None
            ),
            "repository_pair": (
                {
                    "source": change_set.source_repository,
                    "deployment": change_set.deployment_repository,
                }
                if change_set
                else None
            ),
            "issue_number": issue.number if issue else None,
            "issue_url": self._safe_issue_url(issue.url if issue else None),
            "campaign_id": str(run.campaign_id) if run.campaign_id else None,
            "campaign_name": campaign.name if campaign else None,
            "change_set_id": str(run.change_set_id) if run.change_set_id else None,
            "deployment_id": str(deployments[0].id) if deployments else None,
            "workflow_type": run.run_type,
            "normalized_status": normalized_status,
            "raw_status": run.status,
            "projection_state": projection_state,
            "blocker_code": blocker_code,
            "blocker": self._sanitize(
                run.error_code or (active_execution.error_summary if active_execution else None)
            ),
            "current_phase": phase,
            "active_provider": active_execution.provider if active_execution else None,
            "active_role": active_execution.role if active_execution else None,
            "host": active_execution.host if active_execution else None,
            "worker": active_execution.worker_kind if active_execution else run.worker,
            "task_queue": active_execution.task_queue if active_execution else None,
            "started_at": utc_iso(start),
            "last_activity_at": utc_iso(last_activity),
            "updated_at": utc_iso(run.updated_at),
            "updated_datetime": current_utc(run.updated_at) or datetime.min.replace(tzinfo=UTC),
            "duration_seconds": duration_seconds(start, end),
            "last_heartbeat": utc_iso(
                active_execution.last_heartbeat_at if active_execution else None
            ),
            "attempt": active_execution.attempt if active_execution else 1,
            "max_attempts": active_execution.max_attempts if active_execution else 1,
            "retry_count": max(0, (active_execution.attempt if active_execution else 1) - 1),
            "completed_steps": [
                step.name
                for step in steps
                if map_status(step.status, phase=step.name, entity_type="step").normalized
                == "completed"
            ],
            "current_step": active_step.name if active_step else phase,
            "expected_next_step": (
                "Reconcile the durable start intent against Temporal before retrying."
                if projection_state == "ORPHANED_START_INTENT"
                else expected_next_step(mapped.normalized, blocker_code)
            ),
            "execution_id": str(active_execution.id) if active_execution else None,
            "source": "runs/steps/agent_executions/dashboard_events",
            **freshness(last_activity, stale_seconds=self.stale_seconds),
        }
        if include_timeline:
            timeline = [self._step(step) for step in steps]
            timeline.extend(self._event_timeline(event) for event in reversed(events[:100]))
            timeline.sort(key=lambda value: (value["started_at"] or "", value["phase"]))
            item["timeline"] = timeline
            item["executions"] = [
                self._execution(value, include_internal=False) for value in executions
            ]
            item["outcomes"] = {
                "source_pr": change_set.source_pr_url if change_set else None,
                "deployment_pr": change_set.deployment_pr_url if change_set else None,
                "source_commit": change_set.source_head_sha if change_set else None,
                "deployment_commit": change_set.deployment_head_sha if change_set else None,
                "change_set_state": change_set.state if change_set else None,
                "build": change_set.chart_version if change_set else None,
                "deployments": [
                    {
                        "id": str(value.id),
                        "environment": value.environment,
                        "status": value.status,
                        "started_at": utc_iso(value.started_at),
                        "ended_at": utc_iso(value.finished_at),
                    }
                    for value in deployments
                ],
                "verifications": [
                    {
                        "type": value.verification_type,
                        "status": value.status,
                        "provider": value.verifier_provider,
                        "evidence": value.evidence_json,
                    }
                    for value in verifications
                ],
            }
            item["blocker_explanation"] = {
                "code": item["blocker_code"],
                "summary": item["blocker"],
                "first_occurrence": utc_iso(events[-1].occurred_at) if events else None,
                "last_occurrence": utc_iso(events[0].occurred_at) if events else None,
                "affected_phase": phase,
                "retryable": active_execution.retryable if active_execution else None,
                "dependency": item["blocker_code"],
                "expected_next_event": item["expected_next_step"],
            }
        return item

    def _execution(self, row: AgentExecution, *, include_internal: bool = True) -> dict[str, Any]:
        mapped = map_status(row.status, phase=row.phase, entity_type="agent_execution")
        end = row.ended_at or datetime.now(UTC)
        start = row.started_at or row.created_at
        item = {
            "execution_id": str(row.id),
            "workflow_id": row.workflow_id,
            "temporal_run_id": row.temporal_run_id,
            "activity_id": row.activity_id,
            "project": row.project,
            "repository": row.repository_full_name,
            "issue_number": row.issue_number,
            "campaign_id": str(row.campaign_id) if row.campaign_id else None,
            "change_set_id": str(row.change_set_id) if row.change_set_id else None,
            "provider": row.provider,
            "role": row.role,
            "host": row.host,
            "worker_kind": row.worker_kind,
            "task_queue": row.task_queue,
            "normalized_status": mapped.normalized,
            "raw_status": row.status,
            "phase": row.phase,
            "selection_reason": row.selection_reason,
            "cli_path_identifier": row.cli_path_identifier,
            "cli_version": row.cli_version,
            "attempt": row.attempt,
            "max_attempts": row.max_attempts,
            "started_at": utc_iso(row.started_at),
            "last_heartbeat": utc_iso(row.last_heartbeat_at),
            "ended_at": utc_iso(row.ended_at),
            "duration_seconds": duration_seconds(start, end),
            "exit_code": row.exit_code,
            "retryable": row.retryable,
            "blocker_code": row.blocker_code or mapped.blocker_code,
            "error_summary": self._sanitize(row.error_summary),
            "output_truncated": row.output_truncated,
            "transcript_incomplete": row.transcript_incomplete,
            "transcript_complete": not row.transcript_incomplete,
            "total_log_bytes": row.total_log_bytes,
            "redaction_count": row.redaction_count,
            "updated_at": utc_iso(row.updated_at),
            "updated_datetime": current_utc(row.updated_at) or datetime.min.replace(tzinfo=UTC),
            "source": "agent_executions",
            **freshness(row.last_heartbeat_at or row.updated_at, stale_seconds=self.stale_seconds),
        }
        if not include_internal:
            item.pop("updated_datetime", None)
        return item

    def _services(self, session: Session) -> dict[str, Any]:
        heartbeats = list(session.scalars(select(WorkerHeartbeat)).all())
        services = [
            {
                "name": "PostgreSQL",
                "kind": "database",
                "status": "ready",
                "source": "dashboard database query",
                **freshness(datetime.now(UTC), stale_seconds=self.stale_seconds),
            },
            {
                "name": "engineering-dashboard",
                "kind": "controller",
                "status": "ready",
                "source": "current dashboard request",
                **freshness(datetime.now(UTC), stale_seconds=self.stale_seconds),
            },
        ]
        services.extend(
            {
                "name": value.identity,
                "kind": value.worker_kind,
                "host": value.hostname,
                "status": (
                    "stale"
                    if freshness(value.observed_at, stale_seconds=self.stale_seconds)["stale"]
                    else "ready"
                ),
                "source": "worker_heartbeats",
                **freshness(value.observed_at, stale_seconds=self.stale_seconds),
            }
            for value in heartbeats
        )
        represented = {str(item["name"]).lower() for item in services}
        expected_services = (
            ("engineering-api", "controller"),
            ("engineering-temporal-worker", "controller"),
            ("engineering-github-worker", "controller"),
            ("scheduler/notifier", "controller"),
            ("Temporal", "orchestrator"),
            ("Codex worker", "worker"),
            ("Claude worker", "worker"),
            ("browser worker", "worker"),
            ("scan worker", "worker"),
            ("delivery worker", "worker"),
            ("release worker", "worker"),
        )
        services.extend(
            {
                "name": name,
                "kind": kind,
                "status": "unknown",
                "source": "no persisted safe probe",
                **freshness(None, stale_seconds=self.stale_seconds),
            }
            for name, kind in expected_services
            if name.lower() not in represented
        )
        providers: list[dict[str, Any]] = []
        for provider in ("codex", "claude"):
            quota = session.scalar(
                select(QuotaSnapshot)
                .where(QuotaSnapshot.provider == provider)
                .order_by(QuotaSnapshot.captured_at.desc())
                .limit(1)
            )
            executions = list(
                session.scalars(
                    select(AgentExecution)
                    .where(AgentExecution.provider == provider)
                    .order_by(AgentExecution.updated_at.desc())
                    .limit(500)
                ).all()
            )
            active = [
                value
                for value in executions
                if map_status(value.status, phase=value.phase).normalized not in TERMINAL_STATUSES
            ]
            success = next((value for value in executions if value.status == "SUCCESS"), None)
            failure = next(
                (value for value in executions if value.status in {"FAILED", "TIMED_OUT"}),
                None,
            )
            observed = (
                quota.captured_at if quota else executions[0].updated_at if executions else None
            )
            provider_status = (
                provider_connection_status(
                    captured_at=quota.captured_at,
                    authenticated=quota.authenticated,
                    cli_present=quota.cli_present,
                    rate_limited=quota.rate_limited,
                    now=datetime.now(UTC),
                )
                if quota
                else "NOT_CONFIGURED"
            )
            providers.append(
                {
                    "provider": provider,
                    "available": provider_status == "CONNECTED",
                    "authentication_status": (
                        "stale"
                        if provider_status == "STALE"
                        else "authenticated"
                        if quota and quota.authenticated is True
                        else "required"
                        if quota and quota.authenticated is False
                        else "unknown"
                    ),
                    "capacity_status": (
                        "UNKNOWN"
                        if quota.total_capacity is None
                        else "STALE"
                        if provider_status == "STALE"
                        else quota.status
                    )
                    if quota
                    else "NOT_REPORTED",
                    "status": provider_status,
                    "rate_limited": quota.rate_limited if quota else None,
                    "active_executions": len(active),
                    "queue_depth": sum(value.status == "QUEUED" for value in executions),
                    "cli_version": next(
                        (value.cli_version for value in executions if value.cli_version), None
                    ),
                    "last_success": utc_iso(success.ended_at) if success else None,
                    "last_failure": utc_iso(failure.ended_at) if failure else None,
                    "source": "quota_snapshots/agent_executions",
                    **freshness(observed, stale_seconds=PROVIDER_FRESHNESS_SECONDS),
                }
            )
        return {
            "services": services,
            "providers": providers,
            "source": "worker_heartbeats/quota_snapshots/agent_executions",
            "observed_at": utc_iso(datetime.now(UTC)),
        }

    @staticmethod
    def _cursor_page(
        items: list[dict[str, Any]],
        cursor: str | None,
        limit: int,
        *,
        identity_key: str,
        observed_key: str,
    ) -> dict[str, Any]:
        start = 0
        decoded = decode_cursor(cursor)
        if decoded is not None:
            identity, observed = decoded
            for index, item in enumerate(items):
                if str(item[identity_key]) == identity and item.get(observed_key) == observed:
                    start = index + 1
                    break
            else:
                raise ValueError("cursor no longer identifies a result")
        selected = items[start : start + limit]
        next_cursor = (
            encode_cursor(str(selected[-1][identity_key]), selected[-1].get(observed_key))
            if start + limit < len(items) and selected
            else None
        )
        return {"items": selected, "next_cursor": next_cursor, "page_size": limit}

    def _step(self, row: Step) -> dict[str, Any]:
        mapped = map_status(row.status, phase=row.name, entity_type="step")
        evidence = None
        if isinstance(row.result_json, dict):
            for key in ("evidence_id", "artifact_id", "evidence_sha256"):
                value = row.result_json.get(key)
                if isinstance(value, (str, int)):
                    evidence = str(value)[:240]
                    break
        return {
            "phase": row.name,
            "status": mapped.normalized,
            "raw_status": row.status,
            "started_at": utc_iso(row.created_at),
            "ended_at": utc_iso(row.updated_at) if mapped.normalized in TERMINAL_STATUSES else None,
            "duration_seconds": duration_seconds(row.created_at, row.updated_at),
            "attempt": row.attempt,
            "provider": None,
            "evidence_reference": evidence,
            "blocker": mapped.blocker_code,
        }

    def _event_timeline(self, row: DashboardEvent) -> dict[str, Any]:
        mapped = map_status(
            str((row.metadata_json or {}).get("status") or row.current_phase or ""),
            phase=row.current_phase,
            entity_type=row.entity_type,
        )
        return {
            "phase": row.current_phase or row.event_type,
            "status": mapped.normalized,
            "raw_status": (row.metadata_json or {}).get("status"),
            "started_at": utc_iso(row.occurred_at),
            "ended_at": utc_iso(row.occurred_at),
            "duration_seconds": 0,
            "attempt": (row.metadata_json or {}).get("attempt"),
            "provider": (row.metadata_json or {}).get("provider"),
            "evidence_reference": None,
            "blocker": (row.metadata_json or {}).get("blocker_code"),
            "event_type": row.event_type,
        }

    @staticmethod
    def _log(row: AgentLogChunk) -> dict[str, Any]:
        return {
            "id": row.id,
            "sequence": row.sequence,
            "stream": row.stream,
            "timestamp": utc_iso(row.created_at),
            "text": row.redacted_text,
            "byte_count": row.byte_count,
            "redaction_count": row.redaction_count,
            "is_truncated": row.is_truncated,
        }

    def _sanitize(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        safe = str(self.redactor.redact_text(str(value)).value)
        return " ".join(safe.split())[:500] or None

    @staticmethod
    def _safe_issue_url(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        if value.startswith("https://github.com/") and "/issues/" in value:
            return value[:500]
        return None
