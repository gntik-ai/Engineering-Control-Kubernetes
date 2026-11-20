"""The backlog: the work wanted on a project, each item routed to a section or to none yet.

An item comes from an issue of the project's repository or is written by the
administrator. Who routed it is recorded: a label (the issue carries a label that belongs
to exactly one section of the project) or the administrator. An import never changes what
the administrator decided: not the section they chose, not an item they skipped.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session, object_session, sessionmaker

from engineering_control.dashboard.project_records import (
    NAME as PROJECT_NAME,
)
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.project_sections import SECTION_KEY
from engineering_control.db.models import (
    BacklogItem,
    BacklogSync,
    ProjectRecord,
    ProjectSection,
    SandboxRun,
    WorkQueueEntry,
    WorkspaceImport,
)
from engineering_control.engine.library import plan_for_item
from engineering_control.github_app import Issue

# proposed: seen, not decided. accepted: the administrator wants it done. skipped: the
# administrator does not. done: finished, or its issue was closed on GitHub.
STATES: Final = ("proposed", "accepted", "skipped", "done")
OPEN_STATES: Final = ("proposed", "accepted")
TITLE_MAX = 300
MAX_PAGE_SIZE = 200
MAX_BULK = 5000


def clean_title(value: str) -> str:
    text = " ".join(unicodedata.normalize("NFC", value or "").split())
    if not text:
        raise ProjectInvalid("The item needs a title")
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise ProjectInvalid("The title holds a control character")
    return text[:TITLE_MAX]


DESCRIPTION_MAX = 40_000


def normalized_description(value: str) -> str:
    return "".join(ch for ch in value if ch in "\n\t" or ch >= " ").strip()


def clean_description(value: str | None) -> str | None:
    """An item's own text: kept as written, without control characters, bounded."""
    return normalized_description(value or "")[:DESCRIPTION_MAX] or None


def route_by_labels(
    sections: Sequence[ProjectSection], labels: Sequence[str]
) -> tuple[ProjectSection | None, str | None]:
    """The one section a label of the item belongs to, and why. None when no label
    belongs to a section, or when the labels point to more than one."""
    wanted = {label.casefold() for label in labels}
    matches = [
        (section, sorted(wanted & {label.casefold() for label in section.labels or []}))
        for section in sections
    ]
    matches = [(section, shared) for section, shared in matches if shared]
    if not matches:
        return None, None
    if len(matches) > 1:
        names = ", ".join(section.name for section, _shared in matches)
        return None, f"Its labels point to more than one section: {names}"
    section, shared = matches[0]
    return section, f"Label {shared[0]}"


def _starts(item: BacklogItem, section: ProjectSection | None) -> str | None:
    """What starting the item does: "item" when it is worked on (delivered), "scope" when
    it asks its section's flow to look at what it names, None when nothing can run."""
    session = object_session(item)
    if item.workspace_review not in (None, "approved"):
        return None
    if session is None or section is None or not section.flow_key:
        return None
    produced = item.source_run_id is not None or bool((item.facts or {}).get("kind"))
    plan = plan_for_item(session, section.flow_key, produced=produced, version=section.flow_version)
    return plan[1] if plan else None


def serialize(
    item: BacklogItem, project: ProjectRecord, section: ProjectSection | None
) -> dict[str, Any]:
    updated = item.issue_updated_at or item.updated_at
    if updated is not None and updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    return {
        "id": str(item.id),
        "project": project.name,
        "project_name": project.display_name,
        "section": {
            "key": section.key,
            "name": section.name,
            "flow": section.flow_key,
            "flow_version": section.flow_version,
            "starts": _starts(item, section),
        }
        if section
        else None,
        "title": item.title,
        "source": item.source,
        "state": item.state,
        "routed_by": item.routed_by,
        "proposed_by": item.proposed_by,
        "description": item.description,
        "facts": dict(item.facts or {}),
        "source_run_id": str(item.source_run_id) if item.source_run_id else None,
        "routing_reason": item.routing_reason,
        "repository": item.repository,
        "issue_number": item.issue_number,
        "issue_url": item.issue_url,
        "issue_state": item.issue_state,
        "labels": list(item.issue_labels or []),
        "updated_at": updated.isoformat() if updated else None,
    }


def _at_work(session: Session, item_ids: list[UUID]) -> set[UUID]:
    """Of those items, the ones a run that has not ended is working on."""
    busy: set[UUID] = set()
    for start in range(0, len(item_ids), 500):
        busy.update(
            found
            for found in session.scalars(
                select(SandboxRun.item_id).where(
                    SandboxRun.item_id.in_(item_ids[start : start + 500]),
                    SandboxRun.state.in_(("queued", "running", "waiting")),
                )
            )
            if found is not None
        )
    return busy


def _delete(session: Session, item_ids: list[UUID]) -> None:
    """Delete those items with their places in queues. Their runs stay, without them."""
    for start in range(0, len(item_ids), 500):
        some = item_ids[start : start + 500]
        session.execute(delete(WorkQueueEntry).where(WorkQueueEntry.item_id.in_(some)))
        session.execute(update(SandboxRun).where(SandboxRun.item_id.in_(some)).values(item_id=None))
        session.execute(delete(BacklogItem).where(BacklogItem.id.in_(some)))


class BacklogItems:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def listing(
        self,
        *,
        project: str | None = None,
        section: str | None = None,
        state: str | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Items of the active projects, newest activity first. ``section`` is a section
        key, or ``none`` for the items not routed yet."""
        page = max(1, page)
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        query = self._filtered(project=project, section=section, state=state, search=search)
        with self.sessions() as session:
            total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
            rows = session.execute(
                query.order_by(
                    func.coalesce(BacklogItem.issue_updated_at, BacklogItem.updated_at).desc(),
                    BacklogItem.created_at.desc(),
                    BacklogItem.id,
                )
                .limit(page_size)
                .offset((page - 1) * page_size)
            ).all()
            return {
                # A list shows the start of an item's text; the item itself has it whole.
                "items": [
                    {
                        **(row := serialize(item, record, found)),
                        "description": (row["description"] or "")[:400] or None,
                    }
                    for item, record, found in rows
                ],
                "total": int(total),
                "page": page,
                "page_size": page_size,
                "pages": max(1, -(-int(total) // page_size)),
            }

    @staticmethod
    def _filtered(
        *, project: str | None, section: str | None, state: str | None, search: str | None
    ) -> Any:
        """Items with their project and section, narrowed the way the page's filters are."""
        query = (
            select(BacklogItem, ProjectRecord, ProjectSection)
            .join(ProjectRecord, BacklogItem.project_id == ProjectRecord.id)
            .outerjoin(ProjectSection, BacklogItem.section_id == ProjectSection.id)
            .where(ProjectRecord.archived_at.is_(None))
        )
        if project is not None:
            query = query.where(ProjectRecord.name == project)
        if section == "none":
            query = query.where(BacklogItem.section_id.is_(None))
        elif section is not None:
            query = query.where(ProjectSection.key == section)
        if state is not None:
            query = query.where(BacklogItem.state == state)
        text = (search or "").strip()
        if text:
            number = text.removeprefix("#")
            conditions = [func.lower(BacklogItem.title).contains(text.lower(), autoescape=True)]
            if number.isdigit():
                conditions.append(BacklogItem.issue_number == int(number))
            query = query.where(or_(*conditions))
        return query

    def bulk(
        self,
        project: str,
        *,
        section: str | None,
        state: str | None,
        search: str | None,
        expected: int,
        route_to: str | None = None,
        clear_route: bool = False,
        state_to: str | None = None,
        remove: bool = False,
    ) -> dict[str, Any]:
        """One change to every item of ``project`` the filters match: another section,
        another state, or the items deleted.

        ``expected`` is the number of items the administrator was shown. If the filters
        match another number by now, nothing is changed: they decide again on what is there.
        """
        if (route_to is not None) + clear_route + (state_to is not None) + remove != 1:
            raise ProjectInvalid(
                "Choose exactly one change: a section, no section, a state, or deleting them"
            )
        if state_to is not None and state_to not in STATES:
            raise ProjectInvalid("That is not a state an item can have")
        with self.sessions() as session:
            record = self._project(session, project)
            target = self._section(session, record, route_to) if route_to else None
            rows = session.execute(
                self._filtered(project=project, section=section, state=state, search=search).limit(
                    MAX_BULK + 1
                )
            ).all()
            if len(rows) > MAX_BULK:
                raise ProjectConflict(
                    f"More than {MAX_BULK} items match; narrow the filter and do it in parts"
                )
            if len(rows) != expected:
                raise ProjectConflict(
                    f"The filter now matches {len(rows)} items, not {expected}; look at the "
                    "list again before changing them"
                )
            changed = unchanged = 0
            if remove:
                # An item a run is working on stays, with its run.
                busy = _at_work(session, [item.id for item, _record, _section in rows])
                busy.update(item.id for item, _, _ in rows if item.workspace_review is not None)
                gone = [item.id for item, _record, _section in rows if item.id not in busy]
                _delete(session, gone)
                session.commit()
                return {"project": record.name, "changed": len(gone), "unchanged": len(busy)}
            for item, _record, _section in rows:
                if state_to is not None:
                    if item.workspace_review not in (None, "approved"):
                        unchanged += 1
                        continue
                    if state_to in OPEN_STATES and item.issue_state == "closed":
                        unchanged += 1  # its issue is closed on GitHub
                        continue
                    if item.state == state_to:
                        unchanged += 1
                        continue
                    item.state = state_to
                else:
                    wanted = target.id if target else None
                    if item.section_id == wanted and item.routed_by == "administrator":
                        unchanged += 1
                        continue
                    item.section_id = wanted
                    item.routed_by = "administrator"
                    item.routing_reason = None
                changed += 1
            session.commit()
            return {"project": record.name, "changed": changed, "unchanged": unchanged}

    def remove(self, item_id: UUID) -> dict[str, Any]:
        """Delete an item: it leaves the backlog and every queue it is in. The runs that
        worked on it stay in the list of runs. An item that is an issue still open on
        GitHub comes back the next time the backlog is brought in step."""
        with self.sessions.begin() as session:
            item, record = self._item(session, item_id)
            if item.workspace_review is not None:
                raise ProjectConflict("This item is managed in the project workspace")
            if _at_work(session, [item.id]):
                raise ProjectConflict(
                    "A run of the item has not ended: stop it or wait, then delete the item"
                )
            gone = {
                "id": str(item.id),
                "item": item.title,
                "title": item.title,
                "project": record.name,
                "returns": item.source == "github" and item.issue_state == "open",
            }
            _delete(session, [item.id])
            return gone

    def project_of(self, item_id: UUID) -> str | None:
        """The name of the project an item belongs to."""
        with self.sessions() as session:
            return session.scalar(
                select(ProjectRecord.name)
                .join(BacklogItem, BacklogItem.project_id == ProjectRecord.id)
                .where(BacklogItem.id == item_id)
            )

    def inbox(self) -> list[dict[str, Any]]:
        """What waits for the administrator, most pressing kind first.

        Each entry says what it is about, how many items it covers and where to act on it.
        Kinds: a backlog that could not be read, a section with work and no flow, items
        without a section, items not decided on.
        """
        entries: list[dict[str, Any]] = []
        with self.sessions() as session:
            active = ProjectRecord.archived_at.is_(None)
            for name, display, error in session.execute(
                select(ProjectRecord.name, ProjectRecord.display_name, BacklogSync.error)
                .join(BacklogSync, BacklogSync.project_id == ProjectRecord.id)
                .where(active, BacklogSync.error.is_not(None))
                .order_by(ProjectRecord.name)
            ):
                entries.append(
                    {
                        "kind": "sync_failed",
                        "project": name,
                        "project_name": display,
                        "section": None,
                        "count": 1,
                        "detail": error,
                    }
                )
            for name, display, key, section_name, count in session.execute(
                select(
                    ProjectRecord.name,
                    ProjectRecord.display_name,
                    ProjectSection.key,
                    ProjectSection.name,
                    func.count(),
                )
                .join(BacklogItem, BacklogItem.section_id == ProjectSection.id)
                .join(ProjectRecord, ProjectSection.project_id == ProjectRecord.id)
                .where(
                    active, ProjectSection.flow_key.is_(None), BacklogItem.state.in_(OPEN_STATES)
                )
                .group_by(
                    ProjectRecord.name,
                    ProjectRecord.display_name,
                    ProjectSection.key,
                    ProjectSection.name,
                )
                .order_by(func.count().desc(), ProjectRecord.name, ProjectSection.key)
            ):
                entries.append(
                    {
                        "kind": "section_without_flow",
                        "project": name,
                        "project_name": display,
                        "section": {"key": key, "name": section_name},
                        "count": int(count),
                        "detail": None,
                    }
                )
            for kind, condition in (
                ("unrouted", BacklogItem.section_id.is_(None) & BacklogItem.state.in_(OPEN_STATES)),
                ("undecided", BacklogItem.state == "proposed"),
            ):
                for name, display, count in session.execute(
                    select(ProjectRecord.name, ProjectRecord.display_name, func.count())
                    .join(BacklogItem, BacklogItem.project_id == ProjectRecord.id)
                    .where(active, condition)
                    .group_by(ProjectRecord.name, ProjectRecord.display_name)
                    .order_by(func.count().desc(), ProjectRecord.name)
                ):
                    entries.append(
                        {
                            "kind": kind,
                            "project": name,
                            "project_name": display,
                            "section": None,
                            "count": int(count),
                            "detail": None,
                        }
                    )
        return entries

    def open_counts(self) -> dict[str, int]:
        """Project name -> items that are still to do."""
        with self.sessions() as session:
            rows = session.execute(
                select(ProjectRecord.name, func.count())
                .join(BacklogItem, BacklogItem.project_id == ProjectRecord.id)
                .where(BacklogItem.state.in_(OPEN_STATES))
                .group_by(ProjectRecord.name)
            ).all()
            return {str(name): int(count) for name, count in rows}

    def sync_status(self, project: str) -> dict[str, Any] | None:
        with self.sessions() as session:
            record = self._project(session, project)
            status = session.get(BacklogSync, record.id)
            if status is None:
                return None
            synced = status.synced_at
            if synced.tzinfo is None:
                synced = synced.replace(tzinfo=UTC)
            return {
                "synced_at": synced.isoformat(),
                "open_issues": status.open_issues,
                "error": status.error,
            }

    def repository(self, project: str) -> str:
        """The repository whose issues are the project's backlog."""
        with self.sessions() as session:
            return self._project(session, project).source_repository

    def add(
        self, project: str, *, title: str, section: str | None, description: str | None = None
    ) -> dict[str, Any]:
        """An item the administrator wrote. It is accepted: they would not write it otherwise."""
        clean = clean_title(title)
        with self.sessions() as session:
            record = self._project(session, project)
            found = self._section(session, record, section) if section else None
            item = BacklogItem(
                project_id=record.id,
                section_id=found.id if found else None,
                title=clean,
                source="manual",
                state="accepted",
                routed_by="administrator" if found else None,
                routing_reason=None,
                description=clean_description(description),
                issue_labels=[],
            )
            session.add(item)
            session.commit()
            return serialize(item, record, found)

    def edit(
        self, item_id: UUID, *, title: str | None = None, description: str | None = None
    ) -> dict[str, Any]:
        """Change the words of an item that is the platform's own. An issue of the
        repository is edited on GitHub: the next import would undo a change made here."""
        with self.sessions() as session:
            item, record = self._item(session, item_id)
            if item.issue_number is not None:
                raise ProjectInvalid("This item is an issue of the repository; edit it on GitHub")
            if title is not None:
                item.title = clean_title(title)
            if description is not None:
                item.description = clean_description(description)
            session.commit()
            found = session.get(ProjectSection, item.section_id) if item.section_id else None
            return serialize(item, record, found)

    def produce(
        self,
        project: str,
        *,
        title: str,
        section: str | None,
        description: str,
        facts: dict[str, Any],
        proposed_by: str,
        origin_ref: str,
        reason: str,
        run_id: UUID | None = None,
        state: str = "proposed",
    ) -> dict[str, Any] | None:
        """An item a run produced, or one brought over from the original system: a finding
        of an audit, a proposed enhancement. It waits, as proposed, for the administrator.

        ``origin_ref`` names what it came from. None when the project already has an item
        from there, or an open item of the same title: producing it again would repeat it.
        """
        clean = clean_title(title)
        with self.sessions() as session:
            record = self._project(session, project)
            found = None
            if section:
                found = session.scalar(
                    select(ProjectSection).where(
                        ProjectSection.project_id == record.id, ProjectSection.key == section
                    )
                )
            same = session.scalar(
                select(func.count())
                .select_from(BacklogItem)
                .where(
                    BacklogItem.project_id == record.id,
                    (BacklogItem.origin_ref == origin_ref)
                    | (
                        (func.lower(BacklogItem.title) == clean.lower())
                        & BacklogItem.state.in_(OPEN_STATES)
                    ),
                )
            )
            if same:
                return None
            item = BacklogItem(
                project_id=record.id,
                section_id=found.id if found else None,
                title=clean,
                source="manual",
                state=state,
                proposed_by=proposed_by[:16],
                routed_by=proposed_by[:16] if found else None,
                routing_reason=" ".join(reason.split())[:300] or None,
                description=clean_description(description),
                facts=facts,
                source_run_id=run_id,
                origin_ref=origin_ref[:160],
                issue_labels=[],
            )
            session.add(item)
            session.commit()
            return serialize(item, record, found)

    def open_titles(self, project: str, *, limit: int = 400) -> list[str]:
        """The titles of the project's open items, newest first: what is already known."""
        with self.sessions() as session:
            record = self._project(session, project)
            rows = session.execute(
                select(BacklogItem.issue_number, BacklogItem.title)
                .where(BacklogItem.project_id == record.id, BacklogItem.state.in_(OPEN_STATES))
                .order_by(BacklogItem.created_at.desc())
                .limit(limit)
            ).all()
            return [f"#{number} {title}" if number else title for number, title in rows]

    def note_facts(self, item_id: UUID, facts: dict[str, Any], *, state: str | None = None) -> None:
        """Add to what is known about an item, and optionally close it."""
        with self.sessions() as session:
            item = session.get(BacklogItem, item_id)
            if item is None:
                return
            item.facts = {**(item.facts or {}), **facts}
            if state is not None and item.state in OPEN_STATES:
                item.state = state
            session.commit()

    def get(self, item_id: UUID) -> dict[str, Any]:
        with self.sessions() as session:
            item, record = self._item(session, item_id)
            found = session.get(ProjectSection, item.section_id) if item.section_id else None
            return {**serialize(item, record, found), "project_id": str(record.id)}

    def propose(
        self, project: str, *, title: str, section: str | None, reason: str
    ) -> dict[str, Any] | None:
        """An item the supervisor proposes. It waits, as proposed, for the administrator to
        accept, edit or reject it. None when the project already has an open item of that
        title: proposing it again would only repeat it."""
        clean = clean_title(title)
        with self.sessions() as session:
            record = self._project(session, project)
            found = self._section(session, record, section) if section else None
            same = session.scalar(
                select(func.count())
                .select_from(BacklogItem)
                .where(
                    BacklogItem.project_id == record.id,
                    func.lower(BacklogItem.title) == clean.lower(),
                    BacklogItem.state.in_(OPEN_STATES),
                )
            )
            if same:
                return None
            item = BacklogItem(
                project_id=record.id,
                section_id=found.id if found else None,
                title=clean,
                source="manual",
                state="proposed",
                proposed_by="supervisor",
                routed_by="supervisor" if found else None,
                routing_reason=" ".join(reason.split())[:300] or None,
                issue_labels=[],
            )
            session.add(item)
            session.commit()
            return serialize(item, record, found)

    def route_as_supervisor(self, item_id: UUID, section: str, reason: str) -> dict[str, Any]:
        """The supervisor's choice of section, with its reason. A label of the issue that
        names a section still decides at the next import; the administrator's own choice
        is changed only by them."""
        with self.sessions() as session:
            item, record = self._item(session, item_id)
            found = self._section(session, record, section)
            item.section_id = found.id
            item.routed_by = "supervisor"
            item.routing_reason = " ".join(reason.split())[:300] or None
            session.commit()
            return serialize(item, record, found)

    def route(self, item_id: UUID, section: str | None) -> dict[str, Any]:
        """The administrator's choice of section, or of none. An import keeps it."""
        with self.sessions() as session:
            item, record = self._item(session, item_id)
            found = self._section(session, record, section) if section else None
            item.section_id = found.id if found else None
            item.routed_by = "administrator"
            item.routing_reason = None
            session.commit()
            return serialize(item, record, found)

    def set_state(self, item_id: UUID, state: str) -> dict[str, Any]:
        if state not in STATES:
            raise ProjectInvalid("That is not a state an item can have")
        with self.sessions() as session:
            item, record = self._item(session, item_id)
            if item.workspace_review not in (None, "approved"):
                raise ProjectConflict("This workspace item is under review")
            if state in OPEN_STATES and item.issue_state == "closed":
                raise ProjectConflict("Its issue is closed on GitHub; reopen the issue there first")
            item.state = state
            session.commit()
            found = session.get(ProjectSection, item.section_id) if item.section_id else None
            return serialize(item, record, found)

    def missing_workspace_issues(self, project: str, seen: set[int]) -> list[int]:
        with self.sessions() as session:
            record = self._project(session, project)
            return [
                n
                for n in session.scalars(
                    select(BacklogItem.issue_number).where(
                        BacklogItem.project_id == record.id,
                        BacklogItem.workspace_review == "approved",
                        BacklogItem.issue_state == "open",
                        BacklogItem.issue_number.is_not(None),
                    )
                )
                if n is not None and n not in seen
            ]

    def apply_import(
        self,
        project: str,
        issues: Sequence[Issue],
        *,
        complete: bool,
        closed: dict[int, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Bring the project's items in step with the open issues GitHub gave.

        ``complete`` is false when GitHub had more open issues than were read; then an
        item whose issue was not among them is left alone instead of being taken as closed.
        """
        counts = {"imported": 0, "updated": 0, "closed": 0, "reopened": 0, "routed": 0}
        with self.sessions() as session:
            record = active_project(session, project, lock=True)
            reviewed_import = session.get(WorkspaceImport, record.id) is not None
            repository = record.source_repository
            sections = list(
                session.scalars(
                    select(ProjectSection)
                    .where(ProjectSection.project_id == record.id)
                    .order_by(ProjectSection.position)
                )
            )
            existing = {
                item.issue_number: item
                for item in session.scalars(
                    select(BacklogItem).where(
                        BacklogItem.project_id == record.id,
                        func.lower(BacklogItem.repository) == repository.lower(),
                    )
                )
            }
            seen: set[int] = set()
            for issue in issues:
                seen.add(issue.number)
                labels = [label for label in issue.labels if label][:50]
                item = existing.get(issue.number)
                # The wizard owns newly discovered issues on an enrolled project.
                # The legacy open-issue scanner must not create an executable copy.
                if reviewed_import and (
                    item is None or item.workspace_review not in (None, "approved")
                ):
                    continue
                if item is None:
                    item = BacklogItem(
                        project_id=record.id,
                        title=clean_title(issue.title or f"Issue {issue.number}"),
                        source="github",
                        state="proposed",
                        repository=repository,
                        issue_number=issue.number,
                    )
                    session.add(item)
                    counts["imported"] += 1
                else:
                    counts["updated"] += 1
                    if item.issue_state == "closed":
                        # Reopened on GitHub: it is to be decided on again.
                        item.state = "proposed"
                        counts["reopened"] += 1
                item.title = clean_title(issue.title or f"Issue {issue.number}")
                item.issue_url = issue.url[:300] or None
                item.issue_state = "open"
                item.github_closed_at = None
                item.issue_labels = labels
                item.issue_updated_at = issue.updated_at
                if item.routed_by != "administrator":
                    section, reason = route_by_labels(sections, labels)
                    if section is None and item.routed_by == "supervisor":
                        # No label decides: what the supervisor chose stands.
                        continue
                    if section is not None and item.section_id != section.id:
                        counts["routed"] += 1
                    item.section_id = section.id if section else None
                    item.routed_by = "label" if section else None
                    item.routing_reason = reason
            if complete:
                for number, item in existing.items():
                    if (
                        number in seen
                        or item.issue_state != "open"
                        or item.workspace_review not in (None, "approved")
                    ):
                        continue
                    snapshot = (closed or {}).get(number) if number else None
                    if item.workspace_review == "approved" and (
                        snapshot is None or snapshot["state"] != "closed"
                    ):
                        continue
                    item.issue_state = "closed"
                    if snapshot:
                        timestamp = snapshot.get("closed_at")
                        item.github_closed_at = (
                            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                            if timestamp
                            else None
                        )
                        item.facts = {
                            **(item.facts or {}),
                            "github_state_reason": snapshot.get("state_reason"),
                        }
                    if item.state in OPEN_STATES:
                        item.state = "done"
                        if snapshot:
                            item.done_at = item.github_closed_at
                    counts["closed"] += 1
            now = datetime.now(UTC)
            status = session.get(BacklogSync, record.id)
            if status is None:
                status = BacklogSync(project_id=record.id, synced_at=now)
                session.add(status)
            status.synced_at = now
            status.open_issues = len(issues)
            status.error = None
            session.commit()
            return {
                **counts,
                "open_issues": len(issues),
                "complete": complete,
                "synced_at": now.isoformat(),
            }

    def record_failure(self, project: str, error: str) -> None:
        with self.sessions() as session:
            record = self._project(session, project)
            status = session.get(BacklogSync, record.id)
            if status is None:
                status = BacklogSync(project_id=record.id, synced_at=datetime.now(UTC))
                session.add(status)
            status.error = error[:300]
            session.commit()

    @staticmethod
    def _project(session: Session, name: str) -> ProjectRecord:
        record = (
            session.scalar(
                select(ProjectRecord).where(
                    ProjectRecord.name == name, ProjectRecord.archived_at.is_(None)
                )
            )
            if PROJECT_NAME.fullmatch(name)
            else None
        )
        if record is None:
            raise ProjectNotFound(name)
        return record

    @staticmethod
    def _section(session: Session, record: ProjectRecord, key: str) -> ProjectSection:
        section = (
            session.scalar(
                select(ProjectSection).where(
                    ProjectSection.project_id == record.id, ProjectSection.key == key
                )
            )
            if SECTION_KEY.fullmatch(key)
            else None
        )
        if section is None:
            raise ProjectInvalid(f"The project {record.name} has no section {key}")
        return section

    @staticmethod
    def _item(session: Session, item_id: UUID) -> tuple[BacklogItem, ProjectRecord]:
        row = session.execute(
            select(BacklogItem, ProjectRecord)
            .join(ProjectRecord, BacklogItem.project_id == ProjectRecord.id)
            .where(BacklogItem.id == item_id, ProjectRecord.archived_at.is_(None))
        ).first()
        if row is None:
            raise ProjectNotFound(str(item_id))
        return row[0], row[1]
