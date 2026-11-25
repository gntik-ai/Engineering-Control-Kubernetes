"""What moved in a project's planning between two days' measurements.

Planning health is measured once a day for every project that has a plan. This compares
the latest measurement with the one before it and says, in a few words, which of the
numbers that matter moved. It reads those measurements and nothing else, so that the chat
channel can say it in its daily digest without loading anything heavy.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import PlanningHealth, ProjectRecord

# The numbers that are watched, with what each counts. All are better when lower.
WATCHED: Final = {
    "changes_ready": ("change waits for approval", "changes wait for approval"),
    "cleanup_proposed": ("record is proposed for closing", "records are proposed for closing"),
    "structure_proposals": ("proposal for the structure", "proposals for the structure"),
    "orphans_open": ("open issue has no User Story", "open issues have no User Story"),
    "issues_unplaced": (
        "issue looks like the work of no Story",
        "issues look like the work of no Story",
    ),
    "stories_delivered_open": (
        "delivered User Story is still open",
        "delivered User Stories are still open",
    ),
    "stories_with_leads": (
        "User Story has work that is not linked",
        "User Stories have work that is not linked",
    ),
    "stories_unsupported": (
        "User Story has nothing behind it",
        "User Stories have nothing behind them",
    ),
    "bugs_linked": ("bug is still linked to a User Story", "bugs are still linked to a User Story"),
    "scorer_abstains": ("record the scorer abstains on", "records the scorer abstains on"),
    "epic_links_to_judge": (
        "link of a User Story to an Epic beyond its primary one is not judged",
        "links of User Stories to Epics beyond their primary one are not judged",
    ),
}
# What waits for the administrator's word, of the numbers above.
WAITING: Final = ("changes_ready", "cleanup_proposed", "structure_proposals")


def said(key: str, count: int) -> str:
    one, many = WATCHED[key]
    return f"{count:,} {one if count == 1 else many}"


def drift(db: Session, project: UUID) -> dict[str, Any] | None:
    """The project's latest measurement beside the one before: what moved, and what
    waits. None where it was never measured."""
    rows = db.scalars(
        select(PlanningHealth)
        .where(PlanningHealth.project_id == project)
        .order_by(PlanningHealth.day.desc())
        .limit(2)
    ).all()
    if not rows:
        return None
    now = rows[0].numbers or {}
    was = (rows[1].numbers or {}) if len(rows) > 1 else {}
    moved = []
    for key in WATCHED:
        if key in now and key in was and now[key] != was[key]:
            moved.append(
                {
                    "key": key,
                    "now": int(now[key]),
                    "was": int(was[key]),
                    "says": said(key, int(now[key])),
                }
            )
    return {
        "day": rows[0].day.isoformat(),
        "before": rows[1].day.isoformat() if len(rows) > 1 else None,
        "numbers": {key: int(now[key]) for key in WATCHED if key in now},
        "waiting": [said(key, int(now[key])) for key in WAITING if int(now.get(key) or 0) > 0],
        "moved": moved,
    }


def digest_lines(sessions: sessionmaker[Session]) -> list[str]:
    """For the daily digest: one line for each project where something waits for the
    administrator or moved since the day before. None where nothing did."""
    lines = []
    with sessions() as db:
        for key, name in db.execute(
            select(ProjectRecord.id, ProjectRecord.name)
            .where(ProjectRecord.archived_at.is_(None))
            .order_by(ProjectRecord.name)
        ):
            found = drift(db, key)
            if found is None:
                continue
            parts = list(found["waiting"])
            for entry in found["moved"]:
                if entry["key"] in WAITING:
                    continue
                change = entry["now"] - entry["was"]
                parts.append(f"{entry['says']} ({'+' if change > 0 else ''}{change:,})")
            if parts:
                lines.append(f"- **{name}**: {'; '.join(parts)}.")
    return ["Planning:", *lines] if lines else []
