from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import structlog
from sqlalchemy import and_, create_engine, desc, or_, select
from sqlalchemy.orm import Session

from engineering_control.config import Settings, get_settings
from engineering_control.db.models import (
    AgentExecution,
    ConnectionSnapshot,
    NotificationOutbox,
    Run,
    WorkerHeartbeat,
)
from engineering_control.delivery_notifications import (
    LOOP_STATE_TOPIC,
    MILESTONE_TOPIC,
    WRITE_POLICY_TOPIC,
    bounded_code,
    parse_issue,
)
from engineering_control.logging import configure_logging
from engineering_control.notifier import NotificationSink
from engineering_control.operations.projection_health import projection_connection_status
from engineering_control.workflows.cleanup_scope import (
    CLEANUP_IDENTITY_BLOCKED_PROJECTS,
    identity_blocked_projects_from_summary,
)
from engineering_control.workflows.worker_registry import ROLE_WORKER_REGISTRY

logger = structlog.get_logger()

CONTROLLER_UNITS = (
    "engineering-api.service",
    "engineering-dashboard.service",
    "engineering-temporal-worker.service",
    "engineering-scheduler.service",
    "engineering-notifier.service",
    "engineering-github-worker.service",
)


# NEW-127: independent stall alarm for backlog loops (one probe key per loop).
LOOP_PROBE = "backlog-loop-progress"
LOOP_PROBE_PREFIX = f"{LOOP_PROBE}:"
LOOP_PAUSE_LIMIT = timedelta(minutes=15)
LOOP_PROGRESS_LIMIT = timedelta(minutes=45)
# Known long waits: the staging GitOps child polls CI, merges and waits for the Argo CD
# sync within its own 12 h execution bound; a write-approval wait re-checks every 5 min.
LOOP_STAGING_WAIT_BOUND = timedelta(hours=12)
LOOP_APPROVAL_WAIT_BOUND = timedelta(hours=24)
# Loop-state notices older than this are not read (they only name issue and blocker).
LOOP_HORIZON = timedelta(days=7)
LOOP_PAGE = 50
LOOP_LIMIT = 500
LOOP_REALERT_SECONDS = 3600
LOOP_LIVE_STATUSES = frozenset({"RUNNING", "PAUSED", "BLOCKED", "QUEUED"})


@dataclass(frozen=True)
class ProbeResult:
    key: str
    healthy: bool
    status: str
    summary: str
    fingerprint: str | None = None


ProbeProvider = Callable[[], list[ProbeResult]]


def _like_prefix(prefix: str) -> str:
    """A LIKE pattern matching ``prefix`` literally (escape character: backslash)."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class OperationalWatchdog:
    def __init__(
        self,
        settings: Settings,
        *,
        sink: NotificationSink | None = None,
        probe_provider: ProbeProvider | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
        self.sink = sink or NotificationSink(settings)
        self.probe_provider = probe_provider or self._collect
        self.now = now or (lambda: datetime.now(UTC))
        self._http_client: httpx.Client | None = None
        self._closed = False

    def _release(self) -> str:
        target = Path("/opt/engineering-control/source")
        try:
            release = target.resolve(strict=True).parent.name
        except OSError:
            return "unresolved"
        if len(release) == 40 and all(character in "0123456789abcdef" for character in release):
            return release
        return "unresolved"

    def _http(self, key: str, url: str) -> ProbeResult:
        if self._closed:
            raise RuntimeError("watchdog is closed")
        try:
            if self._http_client is None:
                # Keep TLS/pool construction out of the repeated probe path.
                # Initialization remains inside the retry boundary: a temporary
                # construction failure must not permanently disable monitoring.
                self._http_client = httpx.Client(
                    timeout=5,
                    trust_env=False,
                    limits=httpx.Limits(max_connections=3, max_keepalive_connections=3),
                )
            client = self._http_client
            # All fixed services use loopback, but different ports are different
            # trust boundaries. HTTP cookies are not port scoped; never carry
            # one service's cookie into another service's health request.
            client.cookies.clear()
            try:
                response = client.get(url, follow_redirects=False)
            finally:
                client.cookies.clear()
            healthy = response.status_code == 200
        except (httpx.HTTPError, OSError):
            return ProbeResult(key, False, "UNREACHABLE", "bounded loopback HTTP probe failed")
        return ProbeResult(
            key,
            healthy,
            "READY" if healthy else "DEGRADED",
            "bounded loopback HTTP probe passed"
            if healthy
            else f"loopback endpoint returned HTTP {response.status_code}",
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        client, self._http_client = self._http_client, None
        try:
            if client is not None:
                client.close()
        finally:
            self.engine.dispose()

    @staticmethod
    def _tcp(key: str, host: str, port: int) -> ProbeResult:
        try:
            with socket.create_connection((host, port), timeout=3):
                pass
        except OSError:
            return ProbeResult(key, False, "UNREACHABLE", "bounded loopback TCP probe failed")
        return ProbeResult(key, True, "REACHABLE", "bounded loopback TCP probe passed")

    @staticmethod
    def _unit(unit: str) -> ProbeResult:
        try:
            result = subprocess.run(  # noqa: S603 - unit is selected only from fixed constants
                [
                    "/usr/bin/systemctl",
                    "show",
                    "--property=ActiveState",
                    "--property=SubState",
                    "--property=InvocationID",
                    unit,
                ],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return ProbeResult(
                f"unit:{unit}",
                False,
                "UNKNOWN",
                "fixed system unit metadata probe failed",
            )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        active = (
            result.returncode == 0
            and values.get("ActiveState") == "active"
            and values.get("SubState") == "running"
        )
        if unit == "engineering-scheduler.service":
            active = (
                result.returncode == 0
                and values.get("ActiveState") == "active"
                and values.get("SubState") == "exited"
            )
        return ProbeResult(
            f"unit:{unit}",
            active,
            "ACTIVE" if active else "INACTIVE",
            "fixed system unit is active" if active else "fixed system unit is not active",
            values.get("InvocationID") or None,
        )

    def _backup(self, now: datetime) -> ProbeResult:
        path = self.settings.watchdog_backup_status_file
        try:
            metadata = path.lstat()
            parent = path.parent.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o022
                or metadata.st_uid not in {0, os.geteuid()}
                or not stat.S_ISDIR(parent.st_mode)
                or parent.st_mode & 0o022
                or parent.st_uid not in {0, os.geteuid()}
            ):
                raise PermissionError
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = os.read(descriptor, 32769)
            finally:
                os.close(descriptor)
            if len(raw) > 32768:
                raise ValueError
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("version") != 1:
                raise ValueError
            # The fixed backup writer creates this identifier before pg_basebackup.
            # Its UTC timestamp is a conservative recovery-point lower bound;
            # later transfer/restore completion must not make old data look fresh.
            identifier = value.get("identifier")
            if not isinstance(identifier, str) or not re.fullmatch(
                r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}", identifier
            ):
                raise ValueError
            recovery_at = datetime.strptime(identifier[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            backup_at = datetime.fromisoformat(str(value["last_backup_completed_at_utc"]))
            transfer_at = datetime.fromisoformat(str(value["last_transfer_verified_at_utc"]))
            restore_at = datetime.fromisoformat(str(value["last_restore_verified_at_utc"]))
            observed_times = (recovery_at, backup_at, transfer_at, restore_at)
            if (
                now.utcoffset() is None
                or any(observed.utcoffset() is None for observed in observed_times)
                or any(observed > now for observed in observed_times)
                or recovery_at > min(backup_at, transfer_at)
            ):
                raise ValueError
            backup_age = (now - recovery_at).total_seconds()
            transfer_age = (now - transfer_at).total_seconds()
            restore_age = (now - restore_at).total_seconds()
            healthy = (
                value.get("status") == "PASS"
                and value.get("worker_mirror_verified") is True
                and value.get("worker_receipt_signature_verified") is True
                and backup_age <= self.settings.watchdog_backup_max_age_seconds
                and transfer_age <= self.settings.watchdog_backup_max_age_seconds
                and restore_age <= self.settings.watchdog_restore_max_age_seconds
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return ProbeResult(
                "backup-recovery",
                False,
                "MISSING_OR_INVALID",
                "verified backup/transfer/restore status is missing or invalid",
            )
        return ProbeResult(
            "backup-recovery",
            healthy,
            "CURRENT" if healthy else "STALE_OR_FAILED",
            "recovery-point lower bound, signed mirror, and restore are within approved bounds"
            if healthy
            else "recovery-point age, signed mirror, or restore failed its approved requirement",
        )

    def _projection_reconciliation(
        self, row: ConnectionSnapshot | None, now: datetime
    ) -> ProbeResult:
        if row is None:
            return ProbeResult(
                "projection-reconciliation",
                False,
                "MISSING",
                "enabled projection reconciler has no completed observation",
            )
        healthy = (
            projection_connection_status(
                status=row.status,
                checked_at=row.checked_at,
                last_success_at=row.last_success_at,
                detail=row.detail_json,
                now=now,
                interval_seconds=self.settings.projection_reconciliation_interval_seconds,
                cycle_seconds=self.settings.projection_reconciliation_cycle_seconds,
                max_sweep_age_seconds=self.settings.projection_reconciliation_max_sweep_age_seconds,
            )
            == "CONNECTED"
        )
        return ProbeResult(
            "projection-reconciliation",
            healthy,
            "CURRENT" if healthy else "FAILED_OR_STALE",
            "execution-bound projection sweep is current"
            if healthy
            else "projection sweep failed, has unresolved rows, or exceeded freshness",
        )

    def _database(self, now: datetime) -> list[ProbeResult]:
        try:
            with Session(self.engine) as session:
                cleanup = session.scalar(
                    select(Run)
                    .where(Run.run_type == "cleanup")
                    .order_by(desc(Run.updated_at))
                    .limit(1)
                )
                latest_cleanup_success = session.scalar(
                    select(Run)
                    .where(
                        Run.run_type == "cleanup",
                        or_(
                            Run.status == "SUCCESS",
                            # NEW-119: identity-blocked projects only; the rest was cleaned.
                            and_(
                                Run.status == "PARTIAL",
                                Run.error_code == CLEANUP_IDENTITY_BLOCKED_PROJECTS,
                            ),
                        ),
                    )
                    .order_by(desc(Run.finished_at), desc(Run.updated_at))
                    .limit(1)
                )
                heartbeats = list(session.scalars(select(WorkerHeartbeat)).all())
                pending = list(
                    session.scalars(
                        select(NotificationOutbox)
                        .where(NotificationOutbox.status == "PENDING")
                        .order_by(NotificationOutbox.created_at)
                        .limit(21)
                    ).all()
                )
                projection = (
                    session.scalar(
                        select(ConnectionSnapshot).where(
                            ConnectionSnapshot.component == "projection-reconciler"
                        )
                    )
                    if self.settings.projection_reconciliation_enabled
                    else None
                )
        except Exception:  # noqa: BLE001 - independent probe exposes only a fixed safe status
            return [
                ProbeResult(
                    "database",
                    False,
                    "UNREACHABLE",
                    "bounded application database probe failed",
                )
            ]

        results = [ProbeResult("database", True, "REACHABLE", "application database probe passed")]
        if self.settings.projection_reconciliation_enabled:
            results.append(self._projection_reconciliation(projection, now))
        if cleanup is None:
            results.append(
                ProbeResult("cleanup-schedule", False, "MISSING", "no cleanup run is recorded")
            )
        else:
            observed = (
                cleanup.started_at or cleanup.updated_at
                if cleanup.status == "RUNNING"
                else cleanup.finished_at or cleanup.updated_at
            )
            age = max(0, int((now - _utc(observed)).total_seconds()))
            # NEW-119: a PARTIAL run whose only reason is identity-blocked projects is a
            # success for freshness; the blocked projects are still reported below.
            blocked_projects = (
                identity_blocked_projects_from_summary(cleanup.summary)
                if cleanup.status == "PARTIAL"
                and cleanup.error_code == CLEANUP_IDENTITY_BLOCKED_PROJECTS
                else {}
            )
            if latest_cleanup_success is not None and latest_cleanup_success.status == "PARTIAL":
                if not identity_blocked_projects_from_summary(latest_cleanup_success.summary):
                    latest_cleanup_success = None
            success_observed = (
                latest_cleanup_success.finished_at or latest_cleanup_success.updated_at
                if latest_cleanup_success is not None
                else None
            )
            success_current = (
                success_observed is not None
                and max(0, int((now - _utc(success_observed)).total_seconds()))
                <= self.settings.watchdog_cleanup_max_age_seconds
            )
            cleanup_ok = (
                (cleanup.status == "SUCCESS" or bool(blocked_projects))
                and age <= self.settings.watchdog_cleanup_max_age_seconds
            ) or (
                cleanup.status == "RUNNING"
                and cleanup.started_at is not None
                and age <= self.settings.watchdog_cleanup_running_max_age_seconds
                and success_current
            )
            results.append(
                ProbeResult(
                    "cleanup-schedule",
                    cleanup_ok,
                    "CURRENT_OR_RUNNING" if cleanup_ok else "FAILED_OR_MISSED",
                    (
                        "latest cleanup run is current with a recent successful baseline"
                        + (
                            "; identity-blocked projects retained: "
                            + ", ".join(
                                f"{project}={reason}"
                                for project, reason in sorted(blocked_projects.items())
                            )
                            if blocked_projects
                            else ""
                        )
                    )
                    if cleanup_ok
                    else "cleanup failed, lacks a recent success, or exceeded its freshness bound",
                )
            )

        heartbeat_by_kind: dict[str, list[WorkerHeartbeat]] = {}
        for heartbeat in heartbeats:
            heartbeat_by_kind.setdefault(heartbeat.worker_kind, []).append(heartbeat)
        missing: list[str] = []
        stale: list[str] = []
        duplicate: list[str] = []
        for kind in sorted(ROLE_WORKER_REGISTRY):
            candidates = heartbeat_by_kind.get(kind, [])
            fresh = [
                heartbeat
                for heartbeat in candidates
                if max(0, int((now - _utc(heartbeat.observed_at)).total_seconds()))
                <= self.settings.watchdog_heartbeat_max_age_seconds
            ]
            if not candidates:
                missing.append(kind)
                continue
            if not fresh:
                stale.append(kind)
                continue
            if len(fresh) != 1:
                duplicate.append(kind)
                continue
        workers_ok = not missing and not stale and not duplicate
        results.append(
            ProbeResult(
                "worker-capacity",
                workers_ok,
                "CURRENT" if workers_ok else "MISSING_OR_STALE",
                "all fixed role-worker heartbeats are current"
                if workers_ok
                else "one or more fixed role-worker heartbeats are missing or stale",
            )
        )

        exhausted = len(pending) > 20 or any(
            row.attempts >= 10
            or max(0, int((now - _utc(row.created_at)).total_seconds()))
            > self.settings.watchdog_notification_max_age_seconds
            for row in pending
        )
        results.append(
            ProbeResult(
                "notification-outbox",
                not exhausted,
                "BOUNDED" if not exhausted else "DELIVERY_STALLED",
                "notification outbox is within its retry bound"
                if not exhausted
                else "notification outbox delivery is stalled",
            )
        )
        return results

    def _backlog_loop_progress(self, now: datetime) -> list[ProbeResult]:
        """NEW-127: a running backlog loop paused >15 min or without progress >45 min."""
        try:
            with Session(self.engine) as session:
                # Page through every unfinished loop, most stale first, so no bound on
                # the number of rows can hide the loop that most needs attention.
                loops: list[Run] = []
                while len(loops) < LOOP_LIMIT:
                    page = list(
                        session.scalars(
                            select(Run)
                            .where(
                                Run.run_type == "existing-backlog-loop",
                                Run.finished_at.is_(None),
                                Run.status.in_(("RUNNING", "PAUSED", "BLOCKED")),
                            )
                            .order_by(Run.updated_at, Run.id)
                            .offset(len(loops))
                            .limit(LOOP_PAGE)
                        ).all()
                    )
                    loops.extend(page)
                    if len(page) < LOOP_PAGE:
                        break
                truncated = len(loops) >= LOOP_LIMIT
                loops = loops[:LOOP_LIMIT]
                notices = list(
                    session.scalars(
                        select(NotificationOutbox)
                        .where(
                            NotificationOutbox.topic.in_(
                                (LOOP_STATE_TOPIC, MILESTONE_TOPIC, WRITE_POLICY_TOPIC)
                            ),
                            NotificationOutbox.created_at >= now - LOOP_HORIZON,
                        )
                        .order_by(desc(NotificationOutbox.created_at))
                        .limit(2000)
                    ).all()
                )
                activity: dict[str, tuple[list[Run], datetime | None]] = {}
                for loop in loops:
                    prefix = f"{loop.workflow_id}-"
                    pattern = _like_prefix(prefix)
                    # LIKE narrows; the literal prefix check decides (ids may hold _ or %).
                    children = [
                        child
                        for child in session.scalars(
                            select(Run).where(Run.workflow_id.like(pattern, escape="\\")).limit(500)
                        ).all()
                        if child.workflow_id.startswith(prefix)
                    ]
                    agent_times = [
                        _utc(value)
                        for workflow_id, updated, heartbeat in session.execute(
                            select(
                                AgentExecution.workflow_id,
                                AgentExecution.updated_at,
                                AgentExecution.last_heartbeat_at,
                            )
                            .where(AgentExecution.workflow_id.like(pattern, escape="\\"))
                            .order_by(desc(AgentExecution.updated_at))
                            .limit(200)
                        ).all()
                        if workflow_id.startswith(prefix)
                        for value in (updated, heartbeat)
                        if value is not None
                    ]
                    activity[loop.workflow_id] = (
                        children,
                        max(agent_times) if agent_times else None,
                    )
        except Exception:  # noqa: BLE001 - independent probe exposes only a fixed safe status
            return [
                ProbeResult(
                    LOOP_PROBE, False, "UNREADABLE", "bounded backlog-loop progress probe failed"
                )
            ]
        results = [
            ProbeResult(
                LOOP_PROBE,
                not truncated,
                "LOOP_LIMIT_EXCEEDED" if truncated else "READABLE",
                f"more than {LOOP_LIMIT} unfinished backlog loops; the most stale are checked"
                if truncated
                else f"{len(loops)} running backlog loop(s) checked for progress",
            )
        ]
        for loop in loops:
            children, agent_at = activity[loop.workflow_id]
            results.append(self._loop_probe(now, loop, children, agent_at, notices))
        return results

    @staticmethod
    def _loop_probe(
        now: datetime,
        loop: Run,
        children: list[Run],
        agent_at: datetime | None,
        notices: list[NotificationOutbox],
    ) -> ProbeResult:
        workflow_id = loop.workflow_id
        prefix = f"{workflow_id}-"

        def owned(row: NotificationOutbox) -> bool:
            value = row.payload_json if isinstance(row.payload_json, dict) else {}
            owner = str(value.get("workflow_id") or "")
            return owner == workflow_id or owner.startswith(prefix)

        state_row = next(
            (row for row in notices if row.topic == LOOP_STATE_TOPIC and owned(row)), None
        )
        state_value = state_row.payload_json if state_row is not None else {}
        # Only validated shapes reach the alert text (no free text from any payload).
        issue = str(state_value.get("issue") or "")
        issue = issue if parse_issue(issue) else "unknown"
        blocker = bounded_code(state_value.get("blocker")) or "none"
        if issue == "unknown":
            milestone = next(
                (row for row in notices if row.topic == MILESTONE_TOPIC and owned(row)), None
            )
            candidate = str(milestone.payload_json.get("issue") or "") if milestone else ""
            if parse_issue(candidate):
                issue = candidate
        # Progress: the loop's own record, its child runs and their agents' heartbeats.
        child_times = [_utc(child.updated_at) for child in children]
        work_at = max([*child_times, *([agent_at] if agent_at else [])], default=None)
        progress_at = max(
            [_utc(loop.updated_at), *([work_at] if work_at else [])],
        )
        paused_since: datetime | None = None
        state = loop.status
        if loop.status in {"PAUSED", "BLOCKED"}:
            paused_since = _utc(loop.updated_at)
        elif state_row is not None and state_value.get("loop_state") == "PAUSED":
            paused_since = _utc(state_row.created_at)
            state = "PAUSED"
        if paused_since is not None and work_at is not None and work_at > paused_since:
            paused_since = None  # work continued after the recorded pause
            state = "RUNNING"

        def stuck(key_status: str, since: datetime, what: str) -> ProbeResult:
            minutes = int((now - since).total_seconds() // 60)
            return ProbeResult(
                f"{LOOP_PROBE_PREFIX}{workflow_id}",
                False,
                key_status,
                f"backlog loop {workflow_id} issue {issue} state {state} blocker {blocker}: "
                f"{what} for {minutes} min",
            )

        if paused_since is not None:
            if now - paused_since > LOOP_PAUSE_LIMIT:
                return stuck("PAUSED_TOO_LONG", paused_since, "paused")
            status = "PAUSED"
        elif now - progress_at > LOOP_PROGRESS_LIMIT:
            staging = [
                child
                for child in children
                if child.run_type == "staging-gitops"
                and child.finished_at is None
                and now - _utc(child.started_at or child.created_at) <= LOOP_STAGING_WAIT_BOUND
            ]
            approval = any(
                row.topic == WRITE_POLICY_TOPIC
                and owned(row)
                and _utc(row.created_at) >= progress_at
                and now - _utc(row.created_at) <= LOOP_APPROVAL_WAIT_BOUND
                for row in notices
            )
            if staging:
                status = "WAITING_STAGING_GITOPS"
            elif approval:
                status = "AWAITING_APPROVAL"
            else:
                return stuck("NO_PROGRESS", progress_at, "no activity progress")
        else:
            status = "PROGRESSING"
        return ProbeResult(
            f"{LOOP_PROBE_PREFIX}{workflow_id}",
            True,
            status,
            f"backlog loop {workflow_id} issue {issue} is {status.lower().replace('_', ' ')}",
        )

    def _collect(self) -> list[ProbeResult]:
        now = self.now()
        values = [self._unit(unit) for unit in CONTROLLER_UNITS]
        values.extend(
            [
                self._http("engineering-api", "http://127.0.0.1:8740/health/ready"),
                self._http("engineering-dashboard", "http://127.0.0.1:8750/health/ready"),
                self._tcp("temporal", "127.0.0.1", 7233),
            ]
        )
        values.append(self._backup(now))
        if self.settings.watchdog_gateway_required:
            values.append(self._http("hermes-gateway", "http://127.0.0.1:8644/health"))
        values.extend(self._database(now))
        values.extend(self._backlog_loop_progress(now))
        return values

    def _load_state(self) -> dict[str, Any]:
        path = self.settings.watchdog_state_file
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return {"version": 1, "probes": {}}
        try:
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise PermissionError(
                    "watchdog state must be an owner-only non-symlink regular file"
                )
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("version") != 1:
                raise ValueError("watchdog state version is unsupported")
            return value
        except (OSError, ValueError, TypeError):
            quarantine = path.with_name(f"{path.name}.invalid-{uuid4().hex}")
            quarantine_failed = False
            try:
                os.replace(path, quarantine)
                if stat.S_ISREG(metadata.st_mode):
                    quarantine.chmod(0o600)
                quarantined = sorted(
                    path.parent.glob(f"{path.name}.invalid-*"),
                    key=lambda candidate: candidate.lstat().st_mtime,
                    reverse=True,
                )
                for stale in quarantined[3:]:
                    stale.unlink(missing_ok=True)
            except OSError:
                quarantine_failed = True
            return {
                "version": 1,
                "probes": {},
                "state_recovered": True,
                "state_quarantine_failed": quarantine_failed,
            }

    def _save_state(self, value: dict[str, Any]) -> None:
        path = self.settings.watchdog_state_file
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("watchdog state write made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, path)
            parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _close_ended_loops(self, results: list[ProbeResult], previous: dict[str, Any]) -> None:
        """NEW-127: recover a loop's alert only when its Run durably ended.

        Absence from the probe is not proof: only a finished/terminal loop Run is. A loop
        that is missing without that proof keeps its state (and its alert) untouched.
        """
        if not any(probe.key == LOOP_PROBE for probe in results):
            return
        present = {probe.key for probe in results}
        missing = {
            key.removeprefix(LOOP_PROBE_PREFIX): key
            for key in previous
            if key.startswith(LOOP_PROBE_PREFIX) and key not in present
        }
        if not missing:
            return
        try:
            with Session(self.engine) as session:
                runs = session.scalars(
                    select(Run).where(
                        Run.run_type == "existing-backlog-loop",
                        Run.workflow_id.in_(list(missing)),
                    )
                ).all()
        except Exception:  # noqa: BLE001 - unverifiable: keep the state unchanged
            return
        ended = {
            run.workflow_id
            for run in runs
            if run.finished_at is not None or run.status not in LOOP_LIVE_STATUSES
        }
        for workflow_id, key in missing.items():
            if workflow_id not in ended:
                continue
            prior = previous[key]
            if isinstance(prior, dict) and (prior.get("active") or prior.get("pending_event")):
                results.append(ProbeResult(key, True, "ENDED", "backlog loop Run has ended"))
            else:
                del previous[key]

    def run_once(self) -> dict[str, Any]:
        now = self.now()
        state = self._load_state()
        state_recovered = state.pop("state_recovered", False) is True
        state_quarantine_failed = state.pop("state_quarantine_failed", False) is True
        previous = state.setdefault("probes", {})
        if not isinstance(previous, dict):
            raise ValueError("watchdog probe state is malformed")
        delivered = 0
        failures = 0
        unverifiable_pending = 0
        # Probe providers may reuse a list (including in dependency-injected
        # fixtures).  Keep our synthetic state probe local so one cycle cannot
        # mutate and duplicate the provider's next cycle.
        try:
            results = list(self.probe_provider())
        except Exception:  # noqa: BLE001 - preserve independent failure notification
            results = [
                ProbeResult(
                    "probe-provider",
                    False,
                    "FAILED",
                    "one or more bounded operational probes raised unexpectedly",
                )
            ]
        results.insert(
            0,
            ProbeResult(
                "watchdog-state",
                not state_recovered,
                (
                    "RESET_WITHOUT_QUARANTINE"
                    if state_quarantine_failed
                    else "RECOVERED_CORRUPTION"
                    if state_recovered
                    else "VALID"
                ),
                (
                    "invalid watchdog state was reset but could not be quarantined"
                    if state_quarantine_failed
                    else "invalid watchdog state was quarantined and reset"
                    if state_recovered
                    else "watchdog state is valid"
                ),
            ),
        )
        self._close_ended_loops(results, previous)
        for probe in results:
            prior = previous.get(probe.key, {})
            prior_active = bool(prior.get("active", False)) if isinstance(prior, dict) else False
            prior_alerted = prior.get("last_alerted_at") if isinstance(prior, dict) else None
            prior_episode = prior.get("episode_started_at") if isinstance(prior, dict) else None
            prior_pending_id = prior.get("pending_request_id") if isinstance(prior, dict) else None
            prior_pending_event = prior.get("pending_event") if isinstance(prior, dict) else None
            prior_pending_status = prior.get("pending_status") if isinstance(prior, dict) else None
            prior_pending_summary = (
                prior.get("pending_summary") if isinstance(prior, dict) else None
            )
            prior_pending_fingerprint = (
                prior.get("pending_fingerprint") if isinstance(prior, dict) else None
            )
            prior_pending_observed = (
                prior.get("pending_observed_at") if isinstance(prior, dict) else None
            )
            prior_fingerprint = prior.get("fingerprint") if isinstance(prior, dict) else None
            event: str | None = None
            if isinstance(prior_pending_id, str) and prior_pending_event in {
                "ALERT",
                "RECOVERY",
                "CHANGE",
            }:
                # Resolve the durable event before observing a newer transition.
                # In particular, a new generation must not overwrite an uncertain
                # delivery for the preceding generation.
                event = str(prior_pending_event)
            elif not probe.healthy:
                if not prior_active or prior_pending_event == "ALERT":
                    event = "ALERT"
                elif prior_active and isinstance(prior_alerted, str):
                    try:
                        alerted_at = datetime.fromisoformat(prior_alerted)
                    except ValueError:
                        event = "ALERT"
                    else:
                        repeat = (
                            LOOP_REALERT_SECONDS
                            if probe.key.startswith(LOOP_PROBE_PREFIX)
                            else self.settings.watchdog_repeat_seconds
                        )
                        if (now - _utc(alerted_at)).total_seconds() >= repeat:
                            event = "ALERT"
            elif prior_active and isinstance(prior_alerted, str):
                event = "RECOVERY"
            elif (
                isinstance(prior_fingerprint, str)
                and probe.fingerprint is not None
                and probe.fingerprint != prior_fingerprint
            ):
                event = "CHANGE"

            alert_time = prior_alerted if isinstance(prior_alerted, str) else None
            if not probe.healthy:
                episode_time = (
                    prior_episode
                    if prior_active and isinstance(prior_episode, str)
                    else now.isoformat()
                )
            else:
                episode_time = None
            same_pending_event = False
            delivery_payload: dict[str, Any] = {}
            if event is not None:
                same_pending_event = (
                    isinstance(prior_pending_id, str) and prior_pending_event == event
                )
                if same_pending_event:
                    request_id = str(prior_pending_id)
                else:
                    request_id = str(
                        uuid5(
                            NAMESPACE_URL,
                            f"engineering-watchdog:{probe.key}:{event}:{now.isoformat()}",
                        )
                    )
                if same_pending_event:
                    saved_payload = prior.get("pending_payload")
                    if (
                        not isinstance(saved_payload, dict)
                        or saved_payload.get("event_type") != event
                        or saved_payload.get("probe") != probe.key
                        or saved_payload.get("source") != "engineering-independent-watchdog"
                        or saved_payload.get("synthetic") is not False
                        or any(
                            not isinstance(saved_payload.get(key), str)
                            for key in ("status", "summary", "observed_at", "release")
                        )
                    ):
                        # Older releases did not retain the release-bound payload.
                        # Reconstructing it could reuse an ID with different bytes.
                        # Preserve the unresolved legacy row without sending it.
                        failures += 1
                        unverifiable_pending += 1
                        continue
                    delivery_payload = dict(saved_payload)
                else:
                    delivery_payload = {
                        "event_type": event,
                        "probe": probe.key,
                        "status": probe.status,
                        "summary": probe.summary,
                        "observed_at": now.isoformat(),
                        "release": self._release(),
                        "source": "engineering-independent-watchdog",
                        "synthetic": False,
                    }
                # This durable write is outside the delivery exception handler:
                # if storage fails there must be no external send at all.
                previous[probe.key] = {
                    **prior,
                    "active": prior_active or event == "ALERT",
                    "episode_started_at": episode_time or prior_episode,
                    "pending_request_id": request_id,
                    "pending_event": event,
                    "pending_payload": delivery_payload,
                    "pending_status": delivery_payload.get("status"),
                    "pending_summary": delivery_payload.get("summary"),
                    "pending_observed_at": delivery_payload.get("observed_at"),
                    "pending_fingerprint": (
                        prior_pending_fingerprint if same_pending_event else probe.fingerprint
                    ),
                }
                self._save_state(state)
                try:
                    self.sink.deliver(
                        topic="telegram.engineering-operational-watchdog",
                        request_id=request_id,
                        payload=delivery_payload,
                    )
                except Exception:  # noqa: BLE001 - caller receives a bounded failure count
                    failures += 1
                    pending_request_id: str | None = request_id
                    pending_event: str | None = event
                    pending_status: str | None = (
                        prior_pending_status
                        if same_pending_event and isinstance(prior_pending_status, str)
                        else probe.status
                    )
                    pending_summary: str | None = (
                        prior_pending_summary
                        if same_pending_event and isinstance(prior_pending_summary, str)
                        else probe.summary
                    )
                    pending_fingerprint: str | None = (
                        prior_pending_fingerprint if same_pending_event else probe.fingerprint
                    )
                    pending_observed_at: str | None = (
                        prior_pending_observed
                        if same_pending_event and isinstance(prior_pending_observed, str)
                        else now.isoformat()
                    )
                else:
                    delivered += 1
                    alert_time = now.isoformat()
                    pending_request_id = None
                    pending_event = None
                    pending_status = None
                    pending_summary = None
                    pending_fingerprint = None
                    pending_observed_at = None
            else:
                pending_request_id = None
                pending_event = None
                pending_status = None
                pending_summary = None
                pending_fingerprint = None
                pending_observed_at = None
            missed_alert_delivered = (
                probe.healthy and prior_active and event == "ALERT" and pending_event is None
            )
            if event is not None and pending_event is None:
                # Use the acknowledged event, not an observation that may have
                # overtaken it. The next cycle then reports the newer transition.
                active = event == "ALERT"
            else:
                active = not probe.healthy or missed_alert_delivered or pending_event == "ALERT"
            previous[probe.key] = {
                "active": active,
                "episode_started_at": prior_episode if missed_alert_delivered else episode_time,
                "last_alerted_at": alert_time,
                "last_observed_at": now.isoformat(),
                "pending_event": pending_event,
                "pending_status": pending_status,
                "pending_summary": pending_summary,
                "pending_fingerprint": pending_fingerprint,
                "pending_observed_at": pending_observed_at,
                "pending_request_id": pending_request_id,
                "pending_payload": delivery_payload if pending_event is not None else None,
                "fingerprint": (
                    prior_pending_fingerprint
                    if event is not None and same_pending_event
                    else probe.fingerprint
                ),
                "status": probe.status,
            }
            if event is not None:
                # An acknowledgement-save failure leaves the write-ahead intent
                # intact, so restart retries the same ID and complete payload.
                self._save_state(state)
        state["updated_at"] = now.isoformat()
        self._save_state(state)
        return {
            "status": "PASS" if failures == 0 else "DEGRADED",
            "probe_count": len(results),
            "unhealthy_count": sum(not result.healthy for result in results),
            "delivered_count": delivered,
            "delivery_failure_count": failures,
            "unverifiable_pending_count": unverifiable_pending,
            "values_emitted": False,
        }


def serve() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    watchdog = OperationalWatchdog(settings)
    try:
        _systemd_notify("READY=1\nSTATUS=operational watchdog initialized")
        while True:
            started = time.monotonic()
            try:
                result = watchdog.run_once()
                logger.info("operational_watchdog_completed", **result)
                _systemd_notify("WATCHDOG=1\nSTATUS=operational watchdog cycle completed")
            except Exception as error:  # noqa: BLE001 - persistent process must remain observable
                logger.error("operational_watchdog_failed", error_type=type(error).__name__)
            elapsed = time.monotonic() - started
            time.sleep(max(1, settings.watchdog_probe_seconds - elapsed))
    finally:
        watchdog.close()


def _systemd_notify(value: str) -> None:
    address = os.getenv("NOTIFY_SOCKET")
    if not address:
        return
    target = f"\0{address[1:]}" if address.startswith("@") else address
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.sendto(value.encode("utf-8"), target)
    except OSError:
        return


def run() -> None:
    serve()


if __name__ == "__main__":
    run()
