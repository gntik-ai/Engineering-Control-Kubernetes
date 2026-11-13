"""What the supervisor's tools do, and the checks every call goes through.

The supervisor's prompt and skills shape how it behaves; this module decides what it can
do. Every call is checked here, on the platform's side, before it has any effect:

- the tool must be switched on in the definition the turn runs with;
- its arguments must name things that exist: a section of the item's own project, an item
  that is still open;
- a change is carried out at once only when the definition says so and nothing calls for
  the administrator: otherwise it is recorded as pending and waits in their inbox;
- automatic changes are limited per turn and per hour; past the limit the next one waits.

Every call is recorded with its arguments and what became of it. That record is the
supervisor's history of decisions.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.accounts import Accounts
from engineering_control.dashboard.backlog_items import OPEN_STATES, BacklogItems, clean_title
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    ProjectRecords,
    serialize,
)
from engineering_control.dashboard.project_sections import SectionRecords
from engineering_control.db.models import ProjectRecord, SupervisorAction
from engineering_control.github_app import GitHubApp, GitHubAppError
from engineering_control.sandbox.deliver import toolchain_for
from engineering_control.sandbox.runs import Runs
from engineering_control.supervisor.conversation import Conversation
from engineering_control.supervisor.definition import TOOLS

# Changes carried out without asking, per turn and per hour. Past either, the next waits.
AUTOMATIC_PER_TURN: Final = 40
AUTOMATIC_PER_HOUR: Final = 200
MAX_ANSWER: Final = 24_000
ISSUE_CHARACTERS: Final = 6_000
LOG_CHARACTERS: Final = 6_000
REFUSALS: Final = (ProjectInvalid, ProjectConflict, ProjectNotFound)


@dataclass(frozen=True)
class Outcome:
    """What the supervisor is told about a call."""

    text: str
    error: bool = False


@dataclass(frozen=True)
class Intent:
    """A change the supervisor asked for, checked and put into words."""

    summary: str
    project: str | None = None
    # Set when the change has to be confirmed whatever the definition says, with why.
    confirm: str | None = None


def _text(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) > MAX_ANSWER:
        return text[:MAX_ANSWER] + " […cut: narrow the question]"
    return text


def _uuid(value: Any, what: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise ProjectInvalid(f"That is not the id of {what}") from None


def _words(value: Any, limit: int = 600) -> str:
    return " ".join(str(value or "").split())[:limit]


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


class Toolbox:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        projects: ProjectRecords,
        sections: SectionRecords,
        backlog: BacklogItems,
        accounts: Accounts,
        runs: Runs,
        conversation: Conversation,
        github: GitHubApp | None = None,
    ) -> None:
        self.sessions = sessions
        self.projects = projects
        self.sections = sections
        self.backlog = backlog
        self.accounts = accounts
        self.runs = runs
        self.conversation = conversation
        self.github = github

    # --- a call from the supervisor ------------------------------------------------------

    async def call(
        self, run_id: UUID, definition: dict[str, Any], name: str, arguments: dict[str, Any]
    ) -> Outcome:
        tool = TOOLS.get(name)
        choice = (definition.get("tools") or {}).get(name) or {}
        if tool is None or not choice.get("enabled"):
            await asyncio.to_thread(
                lambda: self._record(
                    run_id,
                    name[:40],
                    "read",
                    arguments,
                    "refused",
                    detail="The tool is not available to the supervisor.",
                )
            )
            return Outcome("Refused: you have no such tool.", error=True)
        try:
            if tool.kind == "read":
                answer = await self._read(name, arguments)
                await asyncio.to_thread(
                    lambda: self._record(run_id, name, "read", arguments, "read")
                )
                return Outcome(_text(answer))
            return await asyncio.to_thread(
                lambda: self._change(run_id, name, tool.kind, choice, arguments)
            )
        except REFUSALS as error:
            detail = (
                "Nothing with that name or id exists."
                if isinstance(error, ProjectNotFound)
                else str(error)
            )
            await asyncio.to_thread(
                lambda: self._record(run_id, name, tool.kind, arguments, "refused", detail=detail)
            )
            return Outcome(f"Refused: {detail}", error=True)
        except GitHubAppError as error:
            said = str(error)
            await asyncio.to_thread(
                lambda: self._record(run_id, name, tool.kind, arguments, "failed", detail=said)
            )
            return Outcome(f"Failed: GitHub did not answer ({said}).", error=True)

    def _change(
        self, run_id: UUID, name: str, kind: str, choice: dict[str, Any], arguments: dict[str, Any]
    ) -> Outcome:
        intent = self._intent(name, arguments)
        reason = _words(arguments.get("reason"))
        why = intent.confirm
        if why is None and choice.get("mode") != "automatic":
            why = "You asked to confirm this kind of action yourself."
        if why is None and self._over_limit(run_id):
            why = "The supervisor reached its limit of actions taken without asking."
        if why is not None:
            self._record(
                run_id,
                name,
                kind,
                arguments,
                "pending",
                summary=intent.summary,
                reason=reason,
                detail=why,
                project=intent.project,
            )
            return Outcome(
                "Not done: this waits for the administrator's confirmation and is now in "
                f"their inbox ({why}) Do not call it again; tell them it waits for them."
            )
        result = self._perform(name, arguments)
        self._record(
            run_id,
            name,
            kind,
            arguments,
            "applied",
            summary=intent.summary,
            reason=reason,
            project=intent.project,
        )
        return Outcome(f"Done: {intent.summary} {_text(result)}")

    def _over_limit(self, run_id: UUID) -> bool:
        hour_ago = datetime.now(UTC) - timedelta(hours=1)
        with self.sessions() as session:
            in_turn = session.scalar(
                select(func.count())
                .select_from(SupervisorAction)
                .where(SupervisorAction.run_id == run_id, SupervisorAction.outcome == "applied")
            )
            in_hour = session.scalar(
                select(func.count())
                .select_from(SupervisorAction)
                .where(
                    SupervisorAction.outcome == "applied", SupervisorAction.created_at > hour_ago
                )
            )
        return (in_turn or 0) >= AUTOMATIC_PER_TURN or (in_hour or 0) >= AUTOMATIC_PER_HOUR

    def _record(
        self,
        run_id: UUID | None,
        tool: str,
        kind: str,
        arguments: dict[str, Any],
        outcome: str,
        *,
        summary: str | None = None,
        reason: str | None = None,
        detail: str | None = None,
        project: str | None = None,
    ) -> None:
        with self.sessions.begin() as session:
            project_id = (
                session.scalar(select(ProjectRecord.id).where(ProjectRecord.name == project))
                if project
                else None
            )
            session.add(
                SupervisorAction(
                    run_id=run_id,
                    tool=tool,
                    kind=kind,
                    arguments=json.loads(_text(arguments))
                    if len(_text(arguments)) < 20_000
                    else {},
                    outcome=outcome,
                    summary=summary[:600] if summary else None,
                    reason=reason[:600] if reason else None,
                    detail=detail[:600] if detail else None,
                    project_id=project_id,
                )
            )

    # --- reading -------------------------------------------------------------------------

    async def _read(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "get_item":
            item = await asyncio.to_thread(self._item, arguments)
            if self.github is not None and item.get("repository") and item.get("issue_number"):
                issue = await self.github.issue(item["repository"], int(item["issue_number"]))
                body = issue.body or ""
                item["issue"] = {
                    "state": issue.state,
                    "labels": list(issue.labels),
                    "text": body[:ISSUE_CHARACTERS]
                    + (" […cut]" if len(body) > ISSUE_CHARACTERS else ""),
                }
            return item
        return await asyncio.to_thread(lambda: self._read_records(name, arguments))

    def _item(self, arguments: dict[str, Any]) -> dict[str, Any]:
        item_id = _uuid(arguments.get("item"), "an item")
        item = self.backlog.get(item_id)
        item.pop("project_id", None)
        item["run"] = self.runs.for_items([str(item_id)]).get(str(item_id))
        return item

    def _read_records(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "list_projects":
            counts = self.backlog.open_counts()
            return [
                {
                    "project": record.name,
                    "name": record.display_name,
                    "repository": record.source_repository,
                    "open_items": counts.get(record.name, 0),
                }
                for record in self.projects.active()
            ]
        if name == "get_project":
            project = str(arguments.get("project") or "")
            sections = self.sections.of_project(project)
            record = next(r for r in self.projects.active() if r.name == project)
            return {
                **serialize(record),
                "sections": [
                    {
                        "key": section["key"],
                        "name": section["name"],
                        "flow": section["flow"]["name"] if section["flow"] else None,
                    }
                    for section in sections
                ],
                "routing_brief": self.sections.brief(project),
            }
        if name == "list_items":
            listing = self.backlog.listing(
                project=arguments.get("project") or None,
                section=arguments.get("section") or None,
                state=arguments.get("state") or None,
                search=arguments.get("search") or None,
                page=int(arguments.get("page") or 1),
                page_size=min(int(arguments.get("page_size") or 25), 50),
            )
            runs = self.runs.for_items([item["id"] for item in listing["items"]])
            return {
                "total": listing["total"],
                "page": listing["page"],
                "pages": listing["pages"],
                "items": [
                    {
                        "id": item["id"],
                        "project": item["project"],
                        "title": item["title"],
                        "issue": item["issue_number"],
                        "section": item["section"]["key"] if item["section"] else None,
                        "state": item["state"],
                        "routed_by": item["routed_by"],
                        "proposed_by": item["proposed_by"],
                        "labels": item["labels"],
                        "run": runs.get(item["id"]),
                    }
                    for item in listing["items"]
                ],
            }
        if name == "list_runs":
            listing = self.runs.listing(
                limit=min(int(arguments.get("limit") or 20), 50),
                state=arguments.get("state") or None,
                project=arguments.get("project") or None,
            )
            return [
                {
                    key: run[key]
                    for key in (
                        "id",
                        "kind",
                        "title",
                        "state",
                        "outcome",
                        "detail",
                        "project",
                        "created_at",
                        "finished_at",
                    )
                }
                for run in listing["items"]
            ]
        if name == "get_run":
            run = self.runs.get(_uuid(arguments.get("run"), "a run"))
            wanted = arguments.get("log_of_step")
            steps = []
            for step in run["steps"]:
                row = {
                    key: step[key]
                    for key in ("position", "name", "state", "provider", "outcome", "detail")
                }
                if wanted is not None and step["position"] == int(wanted):
                    row["log_end"] = str(step.get("log") or "")[-LOG_CHARACTERS:]
                steps.append(row)
            return {
                **{
                    key: run[key]
                    for key in ("id", "kind", "title", "state", "outcome", "detail", "project")
                },
                "pull_request": (run.get("data") or {}).get("pull_request"),
                "steps": steps,
            }
        if name == "list_accounts":
            return [
                {
                    key: account[key]
                    for key in (
                        "name",
                        "provider",
                        "state",
                        "state_reason",
                        "paused",
                        "max_concurrent",
                        "limited_until",
                    )
                }
                for account in self.accounts.listing()["items"]
            ]
        if name == "get_inbox":
            return self.inbox_entries()
        raise ProjectInvalid("That tool reads nothing")

    def inbox_entries(self) -> list[dict[str, Any]]:
        """Everything that waits for the administrator, most pressing first."""
        return self.runs.inbox() + self.accounts.inbox() + self.pending() + self.backlog.inbox()

    # --- changing ------------------------------------------------------------------------

    def _intent(self, name: str, arguments: dict[str, Any]) -> Intent:
        """Check a change against what exists and put it into words. Raises when it cannot
        be done at all."""
        if not _words(arguments.get("reason")) and name != "propose_items":
            raise ProjectInvalid("Say why: a reason is required")
        if name == "propose_items":
            project = str(arguments.get("project") or "")
            keys = {section["key"] for section in self.sections.of_project(project)}
            items = arguments.get("items")
            if not isinstance(items, list) or not 1 <= len(items) <= 20:
                raise ProjectInvalid("Propose between 1 and 20 items at a time")
            for item in items:
                if not isinstance(item, dict):
                    raise ProjectInvalid("Each item has a title and a reason")
                clean_title(str(item.get("title") or ""))
                if not _words(item.get("reason")):
                    raise ProjectInvalid("Each item needs a reason")
                section = item.get("section")
                if section and section not in keys:
                    raise ProjectInvalid(f"The project {project} has no section {section}")
            titles = "; ".join(_words(item.get("title"), 80) for item in items[:5])
            more = f" and {len(items) - 5} more" if len(items) > 5 else ""
            return Intent(
                f"Propose {len(items)} item{'s' if len(items) != 1 else ''} for {project}: "
                f"{titles}{more}.",
                project=project,
            )
        if name == "route_item":
            item = self.backlog.get(_uuid(arguments.get("item"), "an item"))
            if item["state"] not in OPEN_STATES:
                raise ProjectInvalid("The item is not open any more")
            wanted = str(arguments.get("section") or "")
            sections = {s["key"]: s for s in self.sections.of_project(item["project"])}
            if wanted not in sections:
                raise ProjectInvalid(
                    f"The project {item['project']} has no section {wanted}. Its sections: "
                    f"{', '.join(sections)}."
                )
            if item["section"] and item["section"]["key"] == wanted:
                raise ProjectConflict("The item is already in that section")
            confidence = arguments.get("confidence")
            if confidence not in ("high", "medium", "low"):
                raise ProjectInvalid("Say how confident you are: high, medium or low")
            confirm = None
            if item["routed_by"] == "administrator":
                confirm = "It would change a section you chose yourself."
            elif confidence != "high":
                confirm = f"The supervisor is not sure (confidence: {confidence})."
            return Intent(
                f"Route “{_words(item['title'], 120)}” to {sections[wanted]['name']}.",
                project=item["project"],
                confirm=confirm,
            )
        if name == "start_item":
            item = self.backlog.get(_uuid(arguments.get("item"), "an item"))
            if item["state"] not in OPEN_STATES:
                raise ProjectInvalid("The item is not open any more")
            if item["section"] is None:
                raise ProjectInvalid("The item has no section yet: route it first")
            if item["section"]["flow"] != "deliver-issue":
                raise ProjectInvalid(
                    f"The section {item['section']['name']} has no flow that can run yet"
                )
            return Intent(
                f"Start work on “{_words(item['title'], 120)}” ({item['project']}).",
                project=item["project"],
            )
        raise ProjectInvalid("That tool changes nothing")

    def _perform(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "propose_items":
            project = str(arguments["project"])
            proposed, repeated = [], []
            for item in arguments["items"]:
                made = self.backlog.propose(
                    project,
                    title=str(item["title"]),
                    section=item.get("section") or None,
                    reason=str(item["reason"]),
                )
                if made is None:
                    repeated.append(_words(item["title"], 120))
                else:
                    proposed.append({"id": made["id"], "title": made["title"]})
            return {"proposed": proposed, "already_in_the_backlog": repeated}
        if name == "route_item":
            routed = self.backlog.route_as_supervisor(
                UUID(str(arguments["item"])), str(arguments["section"]), str(arguments["reason"])
            )
            return {"item": routed["id"], "section": routed["section"]["key"]}
        if name == "start_item":
            item_id = UUID(str(arguments["item"]))
            of = self.backlog.project_of(item_id)
            toolchain = toolchain_for(of).as_dict() if of else {}
            run = self.runs.start_delivery(item_id, toolchain=toolchain)
            return {"run": run["id"], "state": run["state"]}
        raise ProjectInvalid("That tool changes nothing")

    # --- the administrator's side --------------------------------------------------------

    def pending(self) -> list[dict[str, Any]]:
        """Actions that wait for the administrator, as inbox entries, oldest first."""
        with self.sessions() as session:
            rows = session.execute(
                select(SupervisorAction, ProjectRecord)
                .outerjoin(ProjectRecord, SupervisorAction.project_id == ProjectRecord.id)
                .where(SupervisorAction.outcome == "pending")
                .order_by(SupervisorAction.created_at)
                .limit(100)
            ).all()
            return [
                {
                    "kind": "supervisor_action",
                    "project": project.name if project else None,
                    "project_name": project.display_name if project else None,
                    "section": None,
                    "count": 1,
                    "detail": action.detail,
                    "action_id": str(action.id),
                    "tool": action.tool,
                    "summary": action.summary,
                    "reason": action.reason,
                    "at": _when(action.created_at),
                }
                for action, project in rows
            ]

    def record(self, *, limit: int = 100, decisions_only: bool = True) -> dict[str, Any]:
        """What the supervisor did, newest first. Reading is left out unless asked for."""
        with self.sessions() as session:
            query = (
                select(SupervisorAction, ProjectRecord)
                .outerjoin(ProjectRecord, SupervisorAction.project_id == ProjectRecord.id)
                .order_by(SupervisorAction.created_at.desc())
            )
            if decisions_only:
                query = query.where(SupervisorAction.outcome != "read")
            rows = session.execute(query.limit(max(1, min(limit, 300)))).all()
            return {
                "items": [
                    {
                        "id": str(action.id),
                        "tool": action.tool,
                        "title": TOOLS[action.tool].title if action.tool in TOOLS else action.tool,
                        "kind": action.kind,
                        "outcome": action.outcome,
                        "summary": action.summary,
                        "reason": action.reason,
                        "detail": action.detail,
                        "project": project.name if project else None,
                        "project_name": project.display_name if project else None,
                        "at": _when(action.created_at),
                        "decided_at": _when(action.decided_at),
                        "decided_via": action.decided_via,
                    }
                    for action, project in rows
                ],
                "total": len(rows),
            }

    def decide(self, action_id: UUID, *, accept: bool, via: str) -> dict[str, Any]:
        """The administrator's answer to an action that waits: carry it out, or not."""
        with self.sessions.begin() as session:
            action = session.scalar(
                select(SupervisorAction).where(SupervisorAction.id == action_id).with_for_update()
            )
            if action is None:
                raise ProjectNotFound(str(action_id))
            if action.outcome != "pending":
                raise ProjectConflict("That has already been decided")
            summary = action.summary or TOOLS[action.tool].title
            action.decided_at = datetime.now(UTC)
            action.decided_via = via[:16]
            if not accept:
                action.outcome = "rejected"
                said = f"The administrator rejected the supervisor's proposal: {summary}"
            else:
                try:
                    self._intent(action.tool, dict(action.arguments or {}))
                    self._perform(action.tool, dict(action.arguments or {}))
                except REFUSALS as error:
                    detail = (
                        "What it was about is no longer there."
                        if isinstance(error, ProjectNotFound)
                        else str(error)
                    )
                    action.outcome = "failed"
                    action.detail = detail[:600]
                    said = (
                        f"The administrator accepted the supervisor's proposal, but it could "
                        f"not be carried out ({detail}): {summary}"
                    )
                else:
                    action.outcome = "accepted"
                    said = f"The administrator accepted the supervisor's proposal: {summary}"
            row: dict[str, Any] = {
                "id": str(action.id),
                "outcome": action.outcome,
                "summary": summary,
                "detail": action.detail,
                "action": summary,
            }
        self.conversation.note(said)
        if row["outcome"] == "failed":
            raise ProjectConflict(str(row["detail"]))
        return row
