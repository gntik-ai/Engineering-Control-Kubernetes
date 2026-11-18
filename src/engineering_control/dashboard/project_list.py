"""The projects as the console lists them: what each is, what waits and what runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.dashboard.project_records import ProjectRecords, serialize
from engineering_control.db.models import ProjectRecord, SandboxRun
from engineering_control.sandbox.runs import ACTIVE, CONVERSATION


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def project_list(sessions: sessionmaker[Session]) -> dict[str, Any]:
    """One row per project: its repositories, the items still to do, its runs.

    Counts only, read from the records themselves, so the page can ask again and again.
    A run's state is the run's record: nothing else holds a copy of it.
    """
    projects = ProjectRecords(sessions)
    summaries: dict[str, dict[str, Any]] = {
        record.name: {
            **serialize(record),
            "open_items": 0,
            "runs": 0,
            "active_runs": 0,
            "last_activity_at": None,
        }
        for record in projects.active()
    }
    for name, count in BacklogItems(sessions).open_counts().items():
        if name in summaries:
            summaries[name]["open_items"] = count
    with sessions() as session:
        rows = session.execute(
            select(
                ProjectRecord.name, SandboxRun.state, func.count(), func.max(SandboxRun.updated_at)
            )
            .join(ProjectRecord, SandboxRun.project_id == ProjectRecord.id)
            .where(SandboxRun.kind != CONVERSATION)
            .group_by(ProjectRecord.name, SandboxRun.state)
        ).all()
    for name, state, count, latest in rows:
        summary = summaries.get(name)
        if summary is None:
            continue
        summary["runs"] += int(count)
        if state in ACTIVE:
            summary["active_runs"] += int(count)
        moment = _utc(latest)
        if moment and (summary["last_activity_at"] is None or moment > summary["last_activity_at"]):
            summary["last_activity_at"] = moment
    items = list(summaries.values())
    for item in items:
        latest = item["last_activity_at"]
        item["last_activity_at"] = latest.isoformat().replace("+00:00", "Z") if latest else None
    return {
        "items": items,
        "default_project": projects.default_name(),
        "refreshed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
