from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from engineering_control.dashboard.accounts import Accounts
from engineering_control.dashboard.auth import PasswordService
from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.dashboard.bring_over import BringOver
from engineering_control.dashboard.planning import PlanningRecords
from engineering_control.dashboard.project_records import ProjectRecords
from engineering_control.dashboard.project_sections import SectionRecords
from engineering_control.dashboard.schemas import ActivityEventInput
from engineering_control.dashboard.session_store import (
    SessionAuthorization,
    lock_authentication,
    retire_session,
)
from engineering_control.dashboard.workspace_import import WorkspaceImports
from engineering_control.dashboard.workspace_metrics import WorkspaceMetrics
from engineering_control.dashboard.workspace_publishing import WorkspacePublishing
from engineering_control.db.models import (
    DashboardAuthentication,
    DashboardEvent,
    DashboardSessionRevocation,
)
from engineering_control.engine.agents import Agents
from engineering_control.engine.evaluations import Evaluations
from engineering_control.engine.records import FlowRecords
from engineering_control.engine.summaries import Summaries
from engineering_control.engine.task_library import TaskLibrary
from engineering_control.sandbox.queues import Budgets, Queues
from engineering_control.sandbox.runs import Runs
from engineering_control.sandbox.triggers import Triggers
from engineering_control.security.redaction import SecretRedactor
from engineering_control.supervisor.conversation import Conversation
from engineering_control.supervisor.definition import Definitions
from engineering_control.supervisor.operations import Operations
from engineering_control.supervisor.tools import Toolbox

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
        self.projects = ProjectRecords(self.sessions)
        self.planning = PlanningRecords(self.sessions)
        self.workspace_imports = WorkspaceImports(self.sessions)
        self.publishing = WorkspacePublishing(self.sessions)
        self.workspace_metrics = WorkspaceMetrics(self.sessions)
        self.sections = SectionRecords(self.sessions)
        self.backlog = BacklogItems(self.sessions)
        self.bring_over = BringOver(self.sessions, self.backlog)
        self.accounts = Accounts(self.sessions)
        self.runs = Runs(self.sessions)
        self.flows = FlowRecords(self.sessions)
        self.tasks = TaskLibrary(self.sessions, self.flows)
        self.agents = Agents(self.sessions, self.flows)
        self.summaries = Summaries(self.sessions)
        self.evaluations = Evaluations(self.sessions, self.runs)
        self.budgets = Budgets(self.sessions)
        self.queues = Queues(self.sessions, self.runs, self.budgets)
        self.triggers = Triggers(self.sessions, self.runs, self.budgets)
        self.definitions = Definitions(self.sessions)
        self.conversation = Conversation(self.sessions, self.definitions)
        self.operations = Operations(self.sessions, self.conversation)
        # The console decides on what the supervisor proposed and reads its record; the
        # supervisor's own calls go through the tool service.
        self.toolbox = Toolbox(
            self.sessions,
            projects=self.projects,
            sections=self.sections,
            backlog=self.backlog,
            accounts=self.accounts,
            runs=self.runs,
            conversation=self.conversation,
            queues=self.queues,
            operations=self.operations,
        )

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

    async def recent_events(self, *, limit: int) -> list[dict[str, Any]]:
        """The latest entries of the record of what was done in the console, oldest first."""
        return await asyncio.to_thread(self._recent_events, limit)

    def _recent_events(self, limit: int) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = list(
                session.scalars(
                    select(DashboardEvent).order_by(DashboardEvent.id.desc()).limit(limit)
                ).all()
            )
            rows.reverse()
            return [self._serialize_event(row) for row in rows]

    async def delete_expired_events(self, *, retention_days: int) -> int:
        return await asyncio.to_thread(self._delete_expired_events, retention_days)

    def _delete_expired_events(self, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        with self.sessions.begin() as session:
            rows = list(
                session.scalars(
                    select(DashboardEvent).where(DashboardEvent.occurred_at < cutoff).limit(10_000)
                ).all()
            )
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
