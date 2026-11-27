"""The bugs of a project in numbers, and what is told of them (B4 of
docs/design/bugs-section.md).

Every number is counted from the same records, in the same way, as the list of the Bugs
page, so that a number and the list it leads to cannot differ: a bug is open here exactly
when the list shows it as open, and has the severity, the area and the state the list
gives it.

- **Numbers**, for the Bugs page: the open bugs by severity and by area, how old they
  are, how many arrived and were fixed lately, how long a fix took from the report, how
  many came back after they had been closed, and how many are regressions.
- **In short**, for a project's dashboard and the table of all projects: the open bugs by
  severity, the new ones nobody looked at, and those a fix is under way for.
- **For the daily digest**: what arrived and what was fixed since the day before.
- **For the chat**: an open bug that a label on GitHub says is critical and that nobody
  gave a severity here. One the administrator called critical themselves is no news to
  them.

No model is asked, and nothing is kept: it is all read when it is asked for.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any, Final

from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.bug_section import (
    SEVERITIES,
    _bugs,
    _opened,
    _records,
    _reported,
    _runs,
    _stands,
    _when,
    area_of,
    fix_of,
    is_regression,
    reopened,
    severity_of,
    triage_of,
)
from engineering_control.dashboard.project_records import active_project
from engineering_control.db.models import BacklogItem, ProjectRecord

# Over how long what arrived and what was fixed is counted, and from how long ago the
# fixes are taken that say how long a fix takes.
LATELY: Final = timedelta(days=30)
FIXES_FROM: Final = timedelta(days=90)
# No more areas than this are named; the rest are counted together.
AREAS: Final = 12
# No more critical bugs than this are told in one pass of the chat.
TOLD_AT_ONCE: Final = 10


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _days(span: timedelta) -> float:
    return round(max(0.0, span.total_seconds()) / 86400, 1)


def _fixed(item: BacklogItem) -> datetime | None:
    """When the bug was fixed: its issue is closed as completed, at that moment."""
    if _stands(item) != "closed":
        return None
    if (item.facts or {}).get("github_state_reason") != "completed":
        return None
    closed = _when(item.github_closed_at)
    return _aware(closed) if closed is not None else None


def brief(db: Session, project: ProjectRecord) -> dict[str, Any]:
    """The project's open bugs in short: how many of each severity, how many are new and
    how many are in progress."""
    records = _records(db, project)
    runs = _runs(db, project)
    severities: Counter[str] = Counter()
    states: Counter[str] = Counter()
    for item in _bugs(db, project):
        if _stands(item) == "closed":
            continue
        record = records.get(item.id)
        severities[severity_of(item, record) or "none"] += 1
        states[triage_of(item, record, fix_of(item, runs.get(item.id)))] += 1
    return {
        "open": sum(severities.values()),
        "severities": {key: severities[key] for key in (*SEVERITIES, "none")},
        "new": states["new"],
        "confirmed": states["confirmed"],
        "in_progress": states["in_progress"],
    }


def said(numbers: dict[str, Any]) -> str:
    """The open bugs of a project in a few words: "7 open: 1 critical, 2 high"."""
    count = int(numbers["open"])
    if not count:
        return "no open bug"
    parts = [
        f"{numbers['severities'][key]} {key}" for key in SEVERITIES if numbers["severities"][key]
    ]
    unrated = int(numbers["severities"]["none"])
    if unrated:
        parts.append(f"{unrated} with no severity yet")
    return f"{count} open: " + ", ".join(parts)


def report(
    sessions: sessionmaker[Session], name: str, now: datetime | None = None
) -> dict[str, Any]:
    """The project's bugs in numbers, as the Bugs page shows them."""
    now = _aware(now or datetime.now(UTC))
    with sessions() as db:
        project = active_project(db, name)
        records = _records(db, project)
        opened = _opened(db, project)
        found = _bugs(db, project)
        numbers = brief(db, project)
        by_area: dict[str, Counter[str]] = {}
        ages: list[float] = []
        took: list[float] = []
        arrived = fixed = came_back = came_back_open = broke = broke_open = 0
        for item in found:
            record = records.get(item.id)
            reported = _aware(_reported(item, opened))
            is_open = _stands(item) != "closed"
            again, regression = reopened(item) > 0, is_regression(item, record)
            came_back += again
            broke += regression
            if reported > now - LATELY:
                arrived += 1
            if is_open:
                ages.append(_days(now - reported))
                area = area_of(item, record) or ""
                by_area.setdefault(area, Counter())[severity_of(item, record) or "none"] += 1
                came_back_open += again
                broke_open += regression
                continue
            done = _fixed(item)
            if done is None:
                continue
            if done > now - LATELY:
                fixed += 1
            if done > now - FIXES_FROM and done >= reported:
                took.append(_days(done - reported))
        areas: list[dict[str, Any]] = [
            {
                "area": area or None,
                "open": sum(counts.values()),
                "severities": {key: counts[key] for key in (*SEVERITIES, "none")},
            }
            # The areas that are named first, the fullest first; then what has none.
            for area, counts in sorted(
                by_area.items(),
                key=lambda entry: (not entry[0], -sum(entry[1].values()), entry[0]),
            )
        ]
        named = [entry for entry in areas if entry["area"] is not None]
        return {
            **numbers,
            "areas": [*named[:AREAS], *(entry for entry in areas if entry["area"] is None)],
            "areas_left_out": max(0, len(named) - AREAS),
            # How old the open bugs are, in days: the oldest, and the one in the middle.
            "age": {
                "oldest": max(ages) if ages else None,
                "median": round(median(ages), 1) if ages else None,
            },
            "days": LATELY.days,
            "arrived": arrived,
            "fixed": fixed,
            # From the report to the fix, in days, of the bugs fixed lately.
            "fix_time": {
                "days": FIXES_FROM.days,
                "bugs": len(took),
                "median": round(median(took), 1) if took else None,
                "longest": max(took) if took else None,
            },
            # Closed and opened again, since that is kept; and broken by a change.
            "reopened": {"open": came_back_open, "all": came_back},
            "regressions": {"open": broke_open, "all": broke},
            "measured_at": now.isoformat(),
        }


def _projects(db: Session) -> list[ProjectRecord]:
    return list(
        db.scalars(
            select(ProjectRecord)
            .where(ProjectRecord.archived_at.is_(None))
            .order_by(ProjectRecord.name)
        )
    )


def digest_lines(sessions: sessionmaker[Session], since: datetime) -> list[str]:
    """For the daily digest: for each project in which a bug arrived or was fixed since
    then, or a critical one is open, how many arrived and were fixed, and what is open.
    Nothing where there is nothing to say."""
    since = _aware(since)
    lines = []
    with sessions() as db:
        for project in _projects(db):
            opened = _opened(db, project)
            arrived = fixed = 0
            for item in _bugs(db, project):
                arrived += _aware(_reported(item, opened)) > since
                done = _fixed(item)
                fixed += done is not None and done > since
            numbers = brief(db, project)
            if not (arrived or fixed or numbers["severities"]["critical"]):
                continue
            moved = ", ".join(
                part
                for part in (
                    f"{arrived} arrived" if arrived else "",
                    f"{fixed} fixed" if fixed else "",
                )
                if part
            )
            lines.append(f"- **{project.name}**: {moved + '; ' if moved else ''}{said(numbers)}.")
    return ["Bugs:", *lines] if lines else []


def critical(
    sessions: sessionmaker[Session], since: datetime, told: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """The open bugs that a label on GitHub says are critical, that arrived or changed
    since then, and that nobody gave a severity here: what the chat tells at once.
    ``told`` are the ones that were told already.

    It is asked often, so it first asks cheaply whether any open issue with such a label
    is there at all, and looks at a project's bugs only where one is."""
    since = _aware(since)
    found: list[dict[str, Any]] = []
    with sessions() as db:
        candidates = {
            project
            for key, project in db.execute(
                select(BacklogItem.id, BacklogItem.project_id).where(
                    BacklogItem.issue_number.is_not(None),
                    BacklogItem.issue_state == "open",
                    cast(BacklogItem.issue_labels, String).ilike("%critical%"),
                    or_(BacklogItem.issue_updated_at > since, BacklogItem.created_at > since),
                )
            )
            if str(key) not in told
        }
        for project in _projects(db):
            if project.id not in candidates:
                continue
            records = _records(db, project)
            opened = _opened(db, project)
            for item in _bugs(db, project):
                record = records.get(item.id)
                if str(item.id) in told or _stands(item) != "open":
                    continue
                if record is not None and record.severity:
                    continue
                if severity_of(item, record) != "critical":
                    continue
                changed = _when(item.issue_updated_at)
                latest = max(_aware(_reported(item, opened)), _aware(changed) if changed else since)
                if latest <= since:
                    continue
                found.append(
                    {
                        "id": str(item.id),
                        "project": project.name,
                        "project_name": project.display_name,
                        "number": item.issue_number,
                        "title": item.title,
                        "url": item.issue_url,
                    }
                )
    return found[:TOLD_AT_ONCE]
