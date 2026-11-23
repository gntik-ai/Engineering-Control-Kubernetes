"""How well a project's Epics, User Stories and technical Issues hang together.

A measurement counts what the planning tables already say of each record, so that its
numbers are the ones the administrator sees there: an issue is an orphan here exactly
when its row says so. One measurement is kept per project and UTC day, the last one of
the day, so that the numbers can be followed over time and a change to how records are
connected can be judged by what it did to them.

Nothing is asked of a model. A measurement first brings the records of the project's links
in step with the links (``planning_links``), so that it counts what each link says of itself.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import planning_links
from engineering_control.dashboard.planning import PlanningRecords
from engineering_control.dashboard.project_records import active_project
from engineering_control.db.models import (
    PlanningHealth,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ReviewEvidenceJob,
)

PAGE: Final = 100
# More pages than any project has by far: a listing that never ends is not followed.
MAX_PAGES: Final = 400
# A measurement is used again for this long, so that opening the page twice counts once.
FRESH: Final = timedelta(minutes=10)
MAX_DAYS: Final = 120
# An Epic that holds more than this share of all Stories is no initiative any more: it
# is where Stories go that fit nowhere. Counted only where there are Stories enough.
CATCH_ALL_SHARE: Final = 0.25
CATCH_ALL_FROM: Final = 20
# The numbers that are followed over time, in the order the page shows them.
FOLLOWED: Final = (
    "orphans_open",
    "orphans_closed",
    "bugs_without_story",
    "stories_without_issue",
    "stories_under_several_epics",
    "largest_epic_stories",
    "links_without_reason",
    "primaries_suggested",
    "reviews_waiting",
)


def _rows(planning: PlanningRecords, name: str, kind: str, **view: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    page = 1
    while page <= MAX_PAGES:
        listed = planning.page(name, kind, page, PAGE, **view)  # type: ignore[arg-type]
        found.extend(listed["items"])
        if page >= int(listed.get("pages") or 1):
            break
        page += 1
    # What is still a proposal is no record yet: it is decided in the tables first.
    return [row for row in found if not row.get("proposal")]


def measure(sessions: sessionmaker[Session], name: str) -> dict[str, Any]:
    """The project's numbers as they are now."""
    planning = PlanningRecords(sessions)
    planning_links.PlanningLinks(sessions).reconcile_project(name)
    epics = [
        row for state in ("open", "closed") for row in _rows(planning, name, "epics", state=state)
    ]
    stories = [
        row for state in ("open", "closed") for row in _rows(planning, name, "stories", state=state)
    ]
    issues = {
        state: _rows(planning, name, "issues", issue_state=state) for state in ("open", "closed")
    }
    every_issue = [*issues["open"], *issues["closed"]]
    per_story = [len(row.get("epic_ids") or []) for row in stories]
    largest = max(epics, key=lambda row: int(row.get("story_count") or 0), default=None)
    largest_count = int(largest.get("story_count") or 0) if largest else 0
    share = largest_count / len(stories) if stories else 0.0
    waiting: dict[str, int] = {}
    for row in [*epics, *stories, *every_issue]:
        review = row.get("automatic_review")
        status = review.get("status") if isinstance(review, dict) else None
        if status in ("queued", "needs_input", "failed"):
            waiting[status] = waiting.get(status, 0) + 1
    with sessions() as db:
        project = active_project(db, name)
        issue_links = db.scalar(
            select(func.count())
            .select_from(ProjectIssueStory)
            .where(ProjectIssueStory.project_id == project.id)
        )
        story_links = db.scalar(
            select(func.count())
            .select_from(ProjectStoryEpic)
            .where(ProjectStoryEpic.project_id == project.id)
        )
        cut_off = db.scalar(
            select(func.count())
            .select_from(ReviewEvidenceJob)
            .where(
                ReviewEvidenceJob.project_id == project.id, ReviewEvidenceJob.state == "uncertain"
            )
        )
        said = planning_links.numbers(db, project.id)
    links = int(issue_links or 0) + int(story_links or 0)
    return {
        "epics": len(epics),
        "stories": len(stories),
        "issues": len(every_issue),
        "issues_open": len(issues["open"]),
        "issues_closed": len(issues["closed"]),
        # Issues that should have a User Story and have none.
        "orphans_open": sum(1 for row in issues["open"] if row.get("orphan")),
        "orphans_closed": sum(1 for row in issues["closed"] if row.get("orphan")),
        # Bugs, for which a User Story is optional, that have none.
        "bugs_without_story": sum(1 for row in every_issue if row.get("standalone_bug")),
        "bugs_confirmed": sum(
            1
            for row in every_issue
            if row.get("type_confirmed") and row.get("execution_type") == "bug"
        ),
        "issues_under_several_stories": sum(
            1 for row in every_issue if len(row.get("story_ids") or []) > 1
        ),
        "stories_without_issue": sum(1 for row in stories if not row.get("issue_count")),
        "stories_under_several_epics": sum(1 for count in per_story if count > 1),
        "epics_per_story": round(sum(per_story) / len(per_story), 1) if per_story else 0.0,
        "most_epics_of_a_story": max(per_story, default=0),
        "epics_without_story": sum(1 for row in epics if not row.get("story_count")),
        "largest_epic_stories": largest_count,
        "largest_epic_title": str(largest["title"])[:200] if largest and largest_count else None,
        "largest_epic_share": round(share, 3),
        "catch_all_epics": sum(
            1
            for row in epics
            if len(stories) >= CATCH_ALL_FROM
            and int(row.get("story_count") or 0) > CATCH_ALL_SHARE * len(stories)
        ),
        "issue_story_links": int(issue_links or 0),
        "story_epic_links": int(story_links or 0),
        # A link says why when its child's text names the parent, the administrator
        # chose it, or the review that made it gave a reason.
        "links_with_reason": said["links_with_reason"],
        "links_without_reason": max(0, links - said["links_with_reason"]),
        "links_declared": said["links_declared"],
        "links_by_administrator": said["links_by_administrator"],
        "links_by_model": said["links_by_model"],
        "links_unknown": said["links_unknown"],
        # Children of several parents whose primary one nobody chose: it is suggested.
        "primaries_suggested": said["primaries_suggested"],
        "reviews_queued": waiting.get("queued", 0),
        "reviews_needing_input": waiting.get("needs_input", 0),
        "reviews_failed": waiting.get("failed", 0),
        "runs_cut_off": int(cut_off or 0),
        "reviews_waiting": sum(waiting.values()) + int(cut_off or 0),
    }


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class PlanningHealthRecords:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def record(self, name: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Measure the project now and keep it as its day's measurement."""
        at = _aware(now or datetime.now(UTC)).astimezone(UTC)
        numbers = measure(self.sessions, name)
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            row = db.get(PlanningHealth, (project.id, at.date()))
            if row is None:
                row = PlanningHealth(project_id=project.id, day=at.date())
                db.add(row)
            row.measured_at, row.numbers = at, numbers
        return numbers

    def report(
        self, name: str, *, days: int = 30, now: datetime | None = None, refresh: bool = False
    ) -> dict[str, Any]:
        """The project's numbers now, the first measurement there is of it, and the days
        between. A measurement that is a few minutes old is used again."""
        at = _aware(now or datetime.now(UTC)).astimezone(UTC)
        days = max(1, min(days, MAX_DAYS))
        with self.sessions() as db:
            project = active_project(db, name)
            today = db.get(PlanningHealth, (project.id, at.date()))
            fresh = today is not None and not refresh and at - _aware(today.measured_at) < FRESH
        if not fresh:
            self.record(name, now=at)
        with self.sessions() as db:
            project = active_project(db, name)
            rows = db.scalars(
                select(PlanningHealth)
                .where(
                    PlanningHealth.project_id == project.id,
                    PlanningHealth.day > at.date() - timedelta(days=days),
                )
                .order_by(PlanningHealth.day)
            ).all()
            first = db.scalar(
                select(PlanningHealth)
                .where(PlanningHealth.project_id == project.id)
                .order_by(PlanningHealth.day)
                .limit(1)
            )
            latest = rows[-1]
            return {
                "project": name,
                "measured_at": _aware(latest.measured_at).isoformat(),
                "now": dict(latest.numbers),
                # What the numbers were when they were first measured: what every later
                # change is compared with.
                "baseline": {"day": first.day.isoformat(), "numbers": dict(first.numbers)}
                if first is not None and first.day != latest.day
                else None,
                "history": [
                    {
                        "day": row.day.isoformat(),
                        **{key: row.numbers.get(key) for key in FOLLOWED},
                    }
                    for row in rows
                ],
            }

    def missing_today(self, *, now: datetime | None = None) -> list[str]:
        """The projects that have planning records and no measurement of today yet."""
        today: date = _aware(now or datetime.now(UTC)).astimezone(UTC).date()
        with self.sessions() as db:
            measured = select(PlanningHealth.project_id).where(PlanningHealth.day == today)
            linked = select(ProjectIssueStory.project_id).union(select(ProjectStoryEpic.project_id))
            return list(
                db.scalars(
                    select(ProjectRecord.name)
                    .where(
                        ProjectRecord.archived_at.is_(None),
                        ProjectRecord.id.in_(linked),
                        ProjectRecord.id.not_in(measured),
                    )
                    .order_by(ProjectRecord.name)
                )
            )
