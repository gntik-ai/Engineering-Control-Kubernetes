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
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import planning_links
from engineering_control.dashboard.planning import PlanningRecords
from engineering_control.dashboard.project_records import active_project
from engineering_control.db.models import (
    BacklogItem,
    PlanningHealth,
    PlanningLink,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
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
    "bugs_linked",
    "stories_without_issue",
    "stories_under_several_epics",
    "largest_epic_stories",
    "largest_epic_primary_stories",
    "stories_with_closed_issues_only",
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
    listed = {
        state: _rows(planning, name, "issues", issue_state=state) for state in ("open", "closed")
    }
    # A bug stands apart from the plan: it is counted on its own, not with the issues.
    bugs = [row for rows in listed.values() for row in rows if row.get("story_optional")]
    issues = {
        state: [row for row in rows if not row.get("story_optional")]
        for state, rows in listed.items()
    }
    every_issue = [*issues["open"], *issues["closed"]]
    per_story = [len(row.get("epic_ids") or []) for row in stories]
    largest = max(epics, key=lambda row: int(row.get("story_count") or 0), default=None)
    largest_count = int(largest.get("story_count") or 0) if largest else 0
    share = largest_count / len(stories) if stories else 0.0
    waiting: dict[str, int] = {}
    for row in [*epics, *stories, *every_issue, *bugs]:
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
        as_primary = planning_links.primary_stories(db, project.id)
    # Each Story counted once, under its primary Epic.
    titles = {str(row["id"]): str(row["title"]) for row in epics}
    primary_largest = max(
        (key for key in as_primary if str(key) in titles),
        key=lambda key: (as_primary[key], str(key)),
        default=None,
    )
    primary_count = as_primary[primary_largest] if primary_largest else 0
    # Open Stories every issue of which is closed: the work under them is done.
    open_issues = {story for row in issues["open"] for story in row.get("story_ids") or []}
    closed_issues = {story for row in issues["closed"] for story in row.get("story_ids") or []}
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
        # Bugs, and those that still carry a link to a User Story from before they were
        # known as bugs.
        "bugs": len(bugs),
        "bugs_open": sum(1 for row in listed["open"] if row.get("story_optional")),
        "bugs_linked": sum(1 for row in bugs if row.get("story_ids")),
        "bugs_confirmed": sum(
            1 for row in bugs if row.get("type_confirmed") and row.get("execution_type") == "bug"
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
        "largest_epic_primary_stories": primary_count,
        "largest_epic_primary_title": (
            titles[str(primary_largest)][:200] if primary_largest and primary_count else None
        ),
        "largest_epic_primary_share": round(primary_count / len(stories), 3) if stories else 0.0,
        "stories_with_closed_issues_only": sum(
            1
            for row in stories
            if row.get("local_state") == "open"
            and str(row["id"]) in closed_issues
            and str(row["id"]) not in open_issues
        ),
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
        "primaries_chosen": said["primaries_chosen"],
        "reviews_queued": waiting.get("queued", 0),
        "reviews_needing_input": waiting.get("needs_input", 0),
        "reviews_failed": waiting.get("failed", 0),
        "runs_cut_off": int(cut_off or 0),
        "reviews_waiting": sum(waiting.values()) + int(cut_off or 0),
    }


def finished_stories(
    sessions: sessionmaker[Session], name: str, page: int, page_size: int
) -> dict[str, Any]:
    """The open User Stories every issue of which is closed, with those issues and
    whether a review found that one of them covers the whole Story. Closing a Story is
    the administrator's, in the User Stories table: this only says which may be done."""
    with sessions() as db:
        project = active_project(db, name)
        strengths = {
            (child, parent): strength
            for child, parent, strength in db.execute(
                select(PlanningLink.child_id, PlanningLink.parent_id, PlanningLink.strength).where(
                    PlanningLink.project_id == project.id,
                    PlanningLink.kind == "issue_story",
                    PlanningLink.removed_at.is_(None),
                )
            )
        }
        under: dict[UUID, list[dict[str, Any]]] = {}
        unfinished: set[UUID] = set()
        for story, item, number, title, state in db.execute(
            select(
                ProjectIssueStory.story_id,
                BacklogItem.id,
                BacklogItem.issue_number,
                BacklogItem.title,
                BacklogItem.issue_state,
            )
            .join(BacklogItem, BacklogItem.id == ProjectIssueStory.item_id)
            .where(ProjectIssueStory.project_id == project.id)
            .order_by(BacklogItem.issue_number, BacklogItem.id)
        ):
            if number is None or state != "closed":
                unfinished.add(story)
            under.setdefault(story, []).append(
                {
                    "id": str(item),
                    "number": number,
                    "title": title,
                    "strength": strengths.get((item, story)),
                }
            )
        found = [
            {
                "id": str(key),
                "title": title,
                "issues": under[key],
                # Whether a review found an issue that covers the whole Story.
                "covered": (
                    "full"
                    if any(issue["strength"] == "full" for issue in under[key])
                    else "partial"
                    if any(issue["strength"] == "partial" for issue in under[key])
                    else None
                ),
            }
            for key, title in db.execute(
                select(ProjectUserStory.id, ProjectUserStory.title)
                .where(
                    ProjectUserStory.project_id == project.id,
                    ProjectUserStory.archived_at.is_(None),
                    ProjectUserStory.closed_at.is_(None),
                )
                .order_by(ProjectUserStory.title, ProjectUserStory.id)
            )
            if key in under and key not in unfinished
        ]
    order = {"full": 0, "partial": 1, None: 2}
    found.sort(key=lambda row: (order[row["covered"]], row["title"]))
    pages = max(1, (len(found) + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    return {
        "items": found[(page - 1) * page_size : page * page_size],
        "total": len(found),
        "covered": sum(1 for row in found if row["covered"] == "full"),
        "page": page,
        "pages": pages,
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
