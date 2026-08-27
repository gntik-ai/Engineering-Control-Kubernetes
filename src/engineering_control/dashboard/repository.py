from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import create_engine, func, or_, select
from sqlalchemy.orm import sessionmaker

from engineering_control.dashboard.auth import PasswordService
from engineering_control.dashboard.schemas import ActivityEventInput
from engineering_control.dashboard.status_mapping import TERMINAL_STATUSES, normalize_status
from engineering_control.db.models import (
    BacklogIssueRecord,
    Campaign,
    ChangeSet,
    CoverageCell,
    DashboardAuthentication,
    DashboardEvent,
    Finding,
    GitHubBacklogCampaign,
    Run,
    SolutionLoop,
)
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
        "result",
        "status",
        "step",
        "task_queue",
        "worker_identity",
    }
)


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
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.passwords = password_service or PasswordService()
        self.redactor = SecretRedactor()

    async def ping(self) -> bool:
        return await asyncio.to_thread(self._ping)

    def _ping(self) -> bool:
        with self.sessions() as session:
            return bool(session.scalar(select(1)) == 1)

    async def authentication_version(self) -> int | None:
        return await asyncio.to_thread(self._authentication_version)

    def _authentication_version(self) -> int | None:
        with self.sessions() as session:
            row = session.get(DashboardAuthentication, 1)
            return row.password_version if row else None

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
            row = session.get(DashboardAuthentication, 1, with_for_update=True)
            if row is None:
                raise ValueError("dashboard password is not initialized")
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
            row = session.get(DashboardAuthentication, 1, with_for_update=True)
            if row is None or not self.passwords.verify(row.password_hash, current_password):
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
            query = (
                select(BacklogIssueRecord, GitHubBacklogCampaign)
                .join(
                    GitHubBacklogCampaign,
                    BacklogIssueRecord.backlog_campaign_id == GitHubBacklogCampaign.id,
                )
                .order_by(BacklogIssueRecord.updated_at.desc())
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
                normalized = normalize_status(raw_status)
                if status and normalized != status:
                    continue
                blocker = None
                if normalized in {"blocked", "failed"}:
                    blocker = (run.error_code if run else None) or issue.eligibility_reason
                last = _latest(
                    issue.updated_at,
                    campaign.updated_at,
                    change_set.updated_at if change_set else None,
                    run.updated_at if run else None,
                )
                records.append(
                    {
                        "issue_number": issue.issue_number,
                        "issue_url": self._safe_issue_url(issue.issue_url_reference),
                        "title": issue.title,
                        "project": campaign.project,
                        "normalized_status": normalized,
                        "raw_status": raw_status,
                        "current_phase": change_set.state if change_set else issue.delivery_status,
                        "queued_at": _utc_iso(run.created_at if run else campaign.created_at),
                        "started_at": _utc_iso(run.started_at if run else None),
                        "ended_at": _utc_iso(run.finished_at if run else None),
                        "last_activity_at": _utc_iso(last),
                        "blocker": self._sanitize_text(blocker),
                        "workflow_id": run.workflow_id if run else campaign.workflow_id,
                    }
                )
            loop_query = select(SolutionLoop).order_by(SolutionLoop.updated_at.desc()).limit(1000)
            if project:
                loop_query = loop_query.where(SolutionLoop.project == project)
            for loop in session.scalars(loop_query).all():
                state = loop.state_json if isinstance(loop.state_json, dict) else {}
                number = state.get("issue_number")
                title = str(state.get("issue_title") or state.get("title") or "Solution loop")[:300]
                normalized = normalize_status(loop.status)
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
                        "current_phase": str(state.get("phase") or loop.status)[:120],
                        "queued_at": _utc_iso(loop.created_at),
                        "started_at": _utc_iso(loop.created_at if normalized != "queued" else None),
                        "ended_at": _utc_iso(
                            loop.updated_at if normalized in TERMINAL_STATUSES else None
                        ),
                        "last_activity_at": _utc_iso(loop.updated_at),
                        "blocker": self._sanitize_text(
                            state.get("blocker") or state.get("last_error")
                        ),
                        "workflow_id": loop.workflow_id,
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
                normalized = normalize_status(campaign.status)
                if status and normalized != status:
                    continue
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
                        "failed_cells": failed,
                        "skipped_cells": skipped,
                        "completion_percentage": completion,
                        "repository_surface": current.repository_surface if current else None,
                        "domain": current.domain if current else None,
                        "persona": current.persona if current else None,
                        "flow": current.flow if current else None,
                        "lens": current.lens if current else None,
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
                            blocker or (run.error_code if run else None)
                        ),
                        "last_activity_at": _utc_iso(last),
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
            rows = session.scalars(
                select(DashboardEvent).where(DashboardEvent.occurred_at < cutoff).limit(10_000)
            ).all()
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


def StringType() -> Any:
    from sqlalchemy import String

    return String()
