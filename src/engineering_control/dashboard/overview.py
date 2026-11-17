"""The numbers of the console's start page.

Day by day, for at most a month: how many items of the backlog were done, and what the
calls over an API cost. A day is a day where the administrator is: the console says how
far its clock is from UTC, and the days are counted from that.

What a step cost is known where it was paid by an API key whose model has a price; work
done on a subscription has no price of its own and adds nothing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import BacklogItem, ProjectRecord, SandboxStep

MAX_DAYS: Final = 31
# No clock on earth is further from UTC than this.
MAX_MINUTES: Final = 14 * 60


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def daily(
    sessions: sessionmaker[Session],
    *,
    days: int = 30,
    minutes: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The last ``days`` days, today included and oldest first: on each, the items that
    were done and what was spent. ``minutes`` is how far ahead of UTC the days are
    counted."""
    days = max(1, min(days, MAX_DAYS))
    zone = timezone(timedelta(minutes=max(-MAX_MINUTES, min(minutes, MAX_MINUTES))))
    today = _aware(now or datetime.now(UTC)).astimezone(zone).date()
    first = today - timedelta(days=days - 1)
    since = datetime.combine(first, datetime.min.time(), zone).astimezone(UTC)
    resolved: dict[date, int] = {}
    cost: dict[date, float] = {}
    with sessions() as session:
        done = session.scalars(
            select(BacklogItem.done_at)
            .join(ProjectRecord, BacklogItem.project_id == ProjectRecord.id)
            .where(
                BacklogItem.state == "done",
                BacklogItem.done_at >= since,
                ProjectRecord.archived_at.is_(None),
            )
        ).all()
        for at in done:
            if at is None:
                continue
            day = _aware(at).astimezone(zone).date()
            resolved[day] = resolved.get(day, 0) + 1
        # A step is dated by when it ended; one that is still at work, by its last change.
        ended = func.coalesce(SandboxStep.finished_at, SandboxStep.updated_at)
        steps = session.execute(
            select(SandboxStep.usage, ended).where(SandboxStep.usage.is_not(None), ended >= since)
        ).all()
        for usage, at in steps:
            try:
                spent = float((usage or {}).get("cost") or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            if spent <= 0 or at is None:
                continue
            day = _aware(at).astimezone(zone).date()
            cost[day] = cost.get(day, 0.0) + spent
    series: list[dict[str, Any]] = [
        {
            "day": day.isoformat(),
            "resolved": resolved.get(day, 0),
            "cost": round(cost.get(day, 0.0), 6),
        }
        for day in (first + timedelta(days=offset) for offset in range(days))
    ]
    return {
        "days": days,
        "series": series,
        "resolved": sum(entry["resolved"] for entry in series),
        "cost": round(sum(entry["cost"] for entry in series), 6),
    }
