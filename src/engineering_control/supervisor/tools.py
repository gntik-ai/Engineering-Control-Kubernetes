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
import difflib
import functools
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.accounts import Accounts
from engineering_control.dashboard.backlog_items import OPEN_STATES, BacklogItems, clean_title
from engineering_control.dashboard.bug_section import (
    PARTS as BUG_PARTS,
)
from engineering_control.dashboard.bug_section import (
    alike_open,
    check_report,
    draft_report,
)
from engineering_control.dashboard.execution_plans import ExecutionPlans
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    ProjectRecords,
    active_project,
    serialize,
)
from engineering_control.dashboard.project_sections import SectionRecords
from engineering_control.dashboard.workspace_publishing import confirm_classification
from engineering_control.db.models import (
    BacklogItem,
    ProjectRecord,
    SandboxRun,
    SupervisorAction,
)
from engineering_control.engine import validate
from engineering_control.engine.catalogue import BUDGETS
from engineering_control.engine.catalogue import catalogue as task_catalogue
from engineering_control.engine.conditions import catalogue as condition_catalogue
from engineering_control.engine.document import FORMAT as FLOW_FORMAT
from engineering_control.engine.document import Document, FlowInvalid
from engineering_control.engine.document import parse as parse_flow
from engineering_control.engine.library import plan_for_item, traits
from engineering_control.engine.prompts import CONTRACTS
from engineering_control.engine.records import FlowRecords, described
from engineering_control.engine.simulate import simulate
from engineering_control.flow_library import DELIVER, WORK_KINDS
from engineering_control.github_app import GitHubApp, GitHubAppError
from engineering_control.sandbox.deliver import ROUNDS_CEILING, rounds_of, toolchain_for
from engineering_control.sandbox.queues import Queues
from engineering_control.sandbox.runs import Runs
from engineering_control.supervisor import advice
from engineering_control.supervisor.conversation import Conversation
from engineering_control.supervisor.definition import TOOLS
from engineering_control.supervisor.learning import ProcessLearning, delivery_brief, draft_issue
from engineering_control.supervisor.operations import Operations
from engineering_control.supervisor.reviews import ReviewHistory

# How often the same work may run within a day before trying it again needs the
# administrator, and the outcomes that trying again does not change.
TRIES_A_DAY: Final = 3
WILL_REPEAT: Final = frozenset(
    {"blocked", "rejected", "protected_files", "secret_found", "no_change", "changes_requested"}
)
# The outcomes of a delivery whose rounds of correction ran out, and how many runs of a
# project have to fail within a day before the supervisor may pause it by itself.
OUT_OF_ROUNDS: Final = frozenset({"changes_requested", "tests_failed"})
FAILURES_TO_PAUSE: Final = 3
# Changes carried out without asking, per turn and per hour. Past either, the next waits.
AUTOMATIC_PER_TURN: Final = 40
AUTOMATIC_PER_HOUR: Final = 200
MAX_ANSWER: Final = 40_000
# Settings many tasks have: the supervisor is told what they are once, not per task.
SHARED_BY: Final = 3


@functools.cache
def shared_settings() -> dict[str, str]:
    """The settings three tasks or more have in the same words: the supervisor is told
    what they are once, not with every task."""
    seen: dict[tuple[str, str], int] = {}
    for task in task_catalogue():
        for param in task["params"]:
            said = f"{param['doc']} Usual: {json.dumps(param['default'])}."
            seen[(param["name"], said)] = seen.get((param["name"], said), 0) + 1
    names = [name for name, _said in seen]
    return {
        name: said
        for (name, said), count in seen.items()
        # A name that means two things is said with each task, so that neither is lost.
        if count >= SHARED_BY and names.count(name) == 1
    }


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


def _text(value: Any, *, bounded: bool = True) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if bounded and len(text) > MAX_ANSWER:
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


def _checked(document: Document) -> dict[str, Any]:
    """What validation says about a draft, and how a run would go through it on paper."""
    went = simulate(document)
    return {
        "problems": [
            f"{problem['level']}: {problem['text']}" for problem in validate.problems(document)
        ],
        "on_paper": {
            "tasks": [
                f"{step['task']}"
                + (f" ({step['provider']})" if step["provider"] else "")
                + f" ends with {step['outcome']}"
                for step in went["steps"]
            ],
            "ends": went["ends"]["text"],
        },
    }


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
        queues: Queues | None = None,
        github: GitHubApp | None = None,
        operations: Operations | None = None,
    ) -> None:
        self.operations = operations
        self.sessions = sessions
        self.projects = projects
        self.sections = sections
        self.backlog = backlog
        self.accounts = accounts
        self.runs = runs
        self.conversation = conversation
        self.queues = queues
        self.github = github
        self.flows = FlowRecords(sessions)
        self.reviews = ReviewHistory(sessions)
        self.learning = ProcessLearning(sessions)
        self.plans = ExecutionPlans(sessions)

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
            scope = await asyncio.to_thread(self._scope, run_id)
            arguments = dict(arguments)
            if tool.kind == "read":
                answer = await self._scoped_read(name, arguments, scope)
                await asyncio.to_thread(
                    lambda: self._record(run_id, name, "read", arguments, "read")
                )
                # This finite, platform-defined catalogue must remain valid JSON as tasks grow.
                return Outcome(_text(answer, bounded=name != "list_tasks"))
            return await asyncio.to_thread(
                lambda: self._change(run_id, name, tool.kind, choice, arguments, scope)
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

    def _scope(self, run_id: UUID) -> dict[str, Any]:
        with self.sessions() as db:
            run = db.get(SandboxRun, run_id)
            return (
                dict((run.data or {}).get("scope") or {}, thread=(run.data or {}).get("thread"))
                if run
                else {}
            )

    @staticmethod
    def _target(scope: dict[str, Any], project: str | None) -> None:
        if scope.get("thread") and project not in scope.get("projects", []):
            raise ProjectInvalid("Select this project in the conversation targets first.")

    async def _scoped_read(
        self, name: str, arguments: dict[str, Any], scope: dict[str, Any]
    ) -> Any:
        thread = scope.get("thread")
        projects = scope.get("projects") if thread else None
        project = arguments.get("project")
        review = name in ("list_reviews", "get_review", "pending_review_findings")
        if project and not (review and scope.get("all_reviews")):
            self._target(scope, project)
        if name == "get_process_learning":
            return await asyncio.to_thread(
                self.learning.summary, str(project), page=arguments.get("page", 1)
            )
        if name == "list_reviews":
            names = [project] if project else None if scope.get("all_reviews") else projects
            return await asyncio.to_thread(
                self.reviews.listing,
                names,
                page=arguments.get("page", 1),
                state=arguments.get("state", ""),
                action=arguments.get("action", ""),
            )
        if name == "get_review":
            return await asyncio.to_thread(
                self.reviews.detail,
                str(project),
                str(arguments.get("source")),
                _uuid(arguments.get("id"), "a review"),
                page=arguments.get("page", 1),
            )
        if name == "pending_review_findings":
            return await asyncio.to_thread(
                self.reviews.findings, str(project), page=arguments.get("page", 1)
            )
        if name == "get_execution_plan":
            if arguments.get("queue"):
                return await asyncio.to_thread(
                    self.plans.detail, str(project), _uuid(arguments["queue"], "a queue")
                )
            return await asyncio.to_thread(self.plans.overview, str(project))
        if thread and name in ("list_items", "list_runs", "list_queues"):
            arguments.setdefault("project", thread)
        if thread and name in ("get_flow", "flow_impact"):
            owner = await asyncio.to_thread(self.flows.owner, str(arguments.get("flow")))
            if owner:
                self._target(scope, owner)
        if name == "flow_impact":
            return await asyncio.to_thread(self.flows.impact, str(arguments.get("flow")))
        if thread and name == "get_item":
            item = await asyncio.to_thread(self._item, arguments)
            self._target(scope, item.get("project"))
        if thread and name == "list_incidents":
            return [
                i
                for p in projects or []
                for i in (await asyncio.to_thread(self._operations().listing, project=p))["items"]
            ]
        answer = await self._read(name, arguments)
        if thread and name in ("get_run", "get_queue"):
            self._target(scope, answer.get("project"))
        elif thread and name in ("get_inbox", "list_projects"):
            return [row for row in answer if row.get("project") in (projects or [])]
        elif thread and name == "list_flows":
            owners = await asyncio.to_thread(self.flows.owners)
            return [
                row
                for row in answer
                if not owners.get(row["flow"]) or owners[row["flow"]] in (projects or [])
            ]
        return answer

    def _change(
        self,
        run_id: UUID,
        name: str,
        kind: str,
        choice: dict[str, Any],
        arguments: dict[str, Any],
        scope: dict[str, Any],
    ) -> Outcome:
        if name in ("propose_flow", "retire_flow"):
            if arguments.get("global_scope") and arguments.get("project"):
                raise ProjectInvalid("Choose one project or the global flow library.")
            if scope.get("thread") and not arguments.get("global_scope"):
                arguments.setdefault("project", scope["thread"])
            flow = str((arguments.get("document") or {}).get("key") or arguments.get("flow") or "")
            target = arguments.get("project")
            if name == "propose_flow" and target:
                local = self.flows.import_key(flow, target)
                if local:
                    arguments["document"] = {**arguments["document"], "key": local}
                    flow = local
            exists = self.flows.exists(flow)
            if exists and self.flows.owner(flow) != target:
                raise ProjectInvalid("The selected flow belongs to another library.")
            arguments["_revision"] = self.flows.version_in_force(flow) if exists else None
            arguments["_exists"] = exists
            arguments["_definition"] = self.flows.versions(flow)[0]["sha256"] if exists else None
        if name in ("remember_approach", "propose_process_issue"):
            self._target(scope, arguments.get("project"))
        if name == "propose_process_issue":
            issue = self.learning.issue(arguments)
            if not issue["register"]:
                self._target(scope, issue["project"])
            arguments["_destination"] = [
                issue["project"],
                issue["repository"],
                issue["branch"],
                issue["project_id"],
            ]
        if name == "classify_issue":
            arguments["_previous"] = self._issue_type(arguments)
        if len(_text(arguments, bounded=False)) >= 20000:
            raise ProjectInvalid("Split this proposed change into smaller actions.")
        intent = self._intent(name, arguments)
        if not (
            name in ("propose_flow", "retire_flow")
            and arguments.get("global_scope")
            and intent.project is None
        ):
            self._target(scope, intent.project)
        reason = _words(arguments.get("reason"))
        why = intent.confirm
        if why is None and choice.get("mode") != "automatic":
            why = "You asked to confirm this kind of action yourself."
        if why is None and self._over_limit(run_id):
            why = "The supervisor reached its limit of actions taken without asking."
        if why is not None:
            if name == "propose_flow":
                # A flow proposed again replaces the proposal that still waits.
                self._withdraw_flow(
                    str((arguments.get("document") or {}).get("key") or ""),
                    arguments.get("project"),
                )
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
        result = self._perform(name, arguments, run_id)
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

    def _withdraw_flow(self, key: str, project: str | None) -> None:
        with self.sessions.begin() as session:
            for action in session.scalars(
                select(SupervisorAction).where(
                    SupervisorAction.tool == "propose_flow", SupervisorAction.outcome == "pending"
                )
            ):
                if (action.arguments or {}).get("project") == project and (
                    (action.arguments or {}).get("document") or {}
                ).get("key") == key:
                    action.outcome = "rejected"
                    action.detail = "Replaced by a later proposal of the same flow."
                    action.decided_at = datetime.now(UTC)
                    action.decided_via = "supervisor"

    def _flow_document(self, arguments: dict[str, Any]) -> Document:
        try:
            return parse_flow(arguments.get("document"))
        except FlowInvalid as error:
            raise ProjectInvalid(str(error)) from error

    def proposed_flow(self, action_id: UUID) -> dict[str, Any]:
        """A flow the supervisor proposed, as the console shows it before it is decided."""
        with self.sessions() as session:
            action = session.get(SupervisorAction, action_id)
            if action is None or action.tool != "propose_flow":
                raise ProjectNotFound(str(action_id))
            arguments = dict(action.arguments or {})
            row = {
                "action_id": str(action.id),
                "outcome": action.outcome,
                "summary": action.summary,
                "reason": action.reason,
                "detail": action.detail,
                "at": _when(action.created_at),
            }
        document = self._flow_document(arguments)
        current = (
            self.flows.version_in_force(document.key) if self.flows.exists(document.key) else None
        )
        prior = self.flows.document(document.key, current) if current is not None else None
        baseline = (
            json.dumps(prior.as_dict(layout=False), sort_keys=True, indent=2).splitlines()
            if prior
            else []
        )
        revised = json.dumps(document.as_dict(layout=False), sort_keys=True, indent=2).splitlines()
        return {
            **row,
            **described(document),
            "project": arguments.get("project"),
            "diff": "\n".join(
                difflib.unified_diff(
                    baseline, revised, fromfile="Current", tofile="Proposed", lineterm=""
                )
            ),
            "impact": self.flows.impact(document.key) if prior else None,
            "exists": self.flows.exists(document.key),
            "in_force": current,
            "on_paper": simulate(document),
        }

    def _read_records(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "list_tasks":
            condition_facts = condition_catalogue()
            return {
                "format": FLOW_FORMAT,
                "tasks": [
                    {
                        "task": task["key"],
                        "does": f"{task['title']}. {task['summary']}",
                        "runs": {
                            "sandbox": "in a sandbox",
                            "bench": "in the sandbox the run keeps, in under a second",
                            "model": "by a model asked over a provider's API, without a "
                            "sandbox and without tools",
                            "platform": "by the platform itself, after the sandboxes' tasks",
                            "control": "nowhere: it only chooses the way",
                            "review": "by the project review worker",
                            "part": "as the tasks of the part it names, in its place",
                        }[task["where"]],
                        "agent": task["account"],
                        "outcomes": task["outcomes"],
                        "settings": {
                            param["name"]: "As settings_tasks_share says."
                            if param["name"] in shared_settings()
                            else f"{param['doc']} Usual: {json.dumps(param['default'])}."
                            for param in task["params"]
                        },
                        "needs": task["needs"],
                        "leaves": task["produces"],
                        "prompt": task["prompt"],
                    }
                    for task in task_catalogue()
                    # A kind that is kept only for the flows that have it is not one to
                    # build a new flow from.
                    if not task["retired"]
                ],
                # Said once, since many tasks have them.
                "settings_tasks_share": shared_settings(),
                "prompts": {
                    contract.name: {
                        "parts_a_flow_may_word": [
                            part["name"]
                            for part in contract.describe({})["parts"]
                            if part["kind"] == "editable"
                        ],
                        "may_name": [f"{{{{{name}}}}}" for name in contract.variables],
                    }
                    for contract in CONTRACTS.values()
                },
                "budgets": {
                    param.name: f"{param.doc} Between {param.low} and {param.high}."
                    for param in BUDGETS.values()
                },
                # What an If (control.when) can ask, for its setting ``condition``.
                "conditions": {
                    "shape": {
                        "match": "all or any",
                        "tests": [{"fact": "change.paths", "is": "all_match", "value": "docs/**"}],
                    },
                    "facts": {
                        fact["key"]: {
                            "is": f"{fact['title']}. {fact['doc']}",
                            "compared": [
                                way["key"] for way in condition_facts["operators"][fact["kind"]]
                            ],
                            **({"values": fact["choices"]} if fact["choices"] else {}),
                            **(
                                {"also": "of: the task whose outcome is asked about"}
                                if fact["kind"] == "outcome"
                                else {}
                            ),
                        }
                        for fact in condition_facts["facts"]
                    },
                    "at_most_tests": condition_facts["max_tests"],
                },
                "rules": [
                    "After the last task that can change the checkout, a change is reviewed "
                    "by the other subscription (agent.review, by its outcome pass), tested "
                    "(check.tests, by passed or failed, never by regression), scanned for "
                    "secrets (check.secrets), "
                    "collected (change.collect) and decided by the administrator "
                    "(people.approval), in that order, before github.pull_request.",
                    "A task that judges another's work names it in other_than and is given "
                    "to the other subscription.",
                    "The platform's own tasks come last: nothing in a sandbox follows them.",
                    "A task needs what it needs from a task before it on every way there.",
                ],
            }
        if name == "list_flows":
            return [
                {
                    "flow": row["key"],
                    "name": row["name"],
                    "summary": row["summary"],
                    "version_in_force": row["version"],
                    "draft": row["draft"],
                    "tasks": row["tasks"],
                    "started_for_a_section_as": row["section_action"],
                }
                for row in self.flows.summary()
            ]
        if name == "get_flow":
            key = str(arguments.get("flow") or "")
            wanted = arguments.get("version")
            version = self.flows.version_in_force(key) if wanted is None else int(wanted)
            found = self.flows.document(
                key, version if version is not None else self.flows.draft(key) or 0
            )
            if found is None:
                raise ProjectNotFound(key)
            return {
                "version": version,
                "document": found.as_dict(layout=False),
                "problems": [p["text"] for p in validate.problems(found)],
            }
        if name == "check_flow":
            document = self._flow_document(arguments)
            return _checked(document)
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
            ids = [item["id"] for item in listing["items"]]
            runs = self.runs.for_items(ids)
            queued = self.queues.for_items(ids) if self.queues is not None else {}
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
                        # What kind of thing it is, when a run produced it: a finding of
                        # an audit, an enhancement, with its severity or risk.
                        "facts": {
                            key: value
                            for key, value in item["facts"].items()
                            if key in ("kind", "severity", "risk", "pull_request")
                        },
                        "run": runs.get(item["id"]),
                        # The queue under way it is in, by name; such an item is not
                        # proposed for another.
                        "queue": (queued.get(item["id"]) or {}).get("name"),
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
                # What an audit or a search for enhancements ended with.
                "result": (run.get("data") or {}).get("result"),
                "steps": steps,
            }
        if name == "list_queues":
            return [
                {
                    key: queue[key]
                    for key in ("id", "name", "project", "state", "proposed_by", "counts")
                }
                for queue in self._queues().listing(project=arguments.get("project") or None)[
                    "items"
                ]
            ]
        if name == "get_queue":
            queue = self._queues().get(_uuid(arguments.get("queue"), "a queue"))
            return {
                **{
                    key: queue[key]
                    for key in (
                        "id",
                        "name",
                        "project",
                        "state",
                        "at_once",
                        "proposed_by",
                        "reason",
                        "pause_reason",
                        "waiting_for",
                        "counts",
                    )
                },
                "items": [
                    {
                        "item": entry["item_id"],
                        "title": entry["title"],
                        "issue": entry["issue_number"],
                        "state": entry["state"],
                        "reason": entry["reason"],
                        "detail": entry["detail"],
                        "run": entry["run"],
                    }
                    for entry in queue["entries"]
                ],
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
        if name == "list_incidents":
            return self._operations().listing()["items"]
        raise ProjectInvalid("That tool reads nothing")

    def _queues(self) -> Queues:
        if self.queues is None:
            raise ProjectInvalid("Queues are not available here")
        return self.queues

    def _operations(self) -> Operations:
        if self.operations is None:
            raise ProjectInvalid("Incidents are not available here")
        return self.operations

    def inbox_entries(self) -> list[dict[str, Any]]:
        """Everything that waits for the administrator, most pressing first."""
        queued = self.queues.inbox() if self.queues is not None else []
        trouble = self.operations.inbox() if self.operations is not None else []
        # A project the supervisor paused waits for the administrator to resume it.
        stopped = [
            {
                "kind": "project_paused",
                "project": record.name,
                "project_name": record.display_name,
                "section": None,
                "count": 1,
                "detail": record.pause_reason,
            }
            for record in self.projects.active()
            if record.paused_at is not None and record.paused_by == "supervisor"
        ]
        return (
            self.runs.inbox()
            + self.accounts.inbox()
            + stopped
            + trouble
            + self.pending()
            + queued
            + self.backlog.inbox()
        )

    # --- changing ------------------------------------------------------------------------

    def _issue_type(self, arguments: dict[str, Any]) -> list[Any]:
        with self.sessions() as db:
            p = active_project(db, str(arguments.get("project")))
            item = db.get(BacklogItem, _uuid(arguments.get("item"), "an issue"))
            if not item or item.project_id != p.id:
                raise ProjectNotFound("Issue")
            return [item.execution_type, item.type_confirmed]

    def _intent(self, name: str, arguments: dict[str, Any]) -> Intent:
        """Check a change against what exists and put it into words. Raises when it cannot
        be done at all."""
        if not _words(arguments.get("reason")) and name not in (
            "propose_items",
            "conclude_incident",
        ):
            raise ProjectInvalid("Say why: a reason is required")
        project = arguments.get("project")
        if name == "remember_approach":
            lesson = self.learning.approach(arguments)
            return Intent(
                f"Remember an approach for {project} / {lesson['flow']}: {lesson['approach']}",
                project=project,
                confirm="Accept to use this guidance in future Supervisor turns.",
            )
        if name == "propose_process_issue":
            issue = self.learning.issue(arguments)
            registration = (
                f"Register Engineering Control on {issue['branch']} with its Issues flow and "
                if issue["register"]
                else "Accept to "
            )
            return Intent(
                f"Draft “{issue['title']}” in {issue['repository']} from {project} evidence.",
                project=project,
                confirm=f"{registration}save a draft. Publication requires issue approval.",
            )
        if name == "ask_specialist":
            candidates = arguments.get("issues")
            if (
                not isinstance(candidates, list)
                or not (1 <= len(candidates) <= 8)
                or not isinstance(arguments.get("request"), str)
            ):
                raise ProjectInvalid("Supply a request and one to eight technical issue IDs.")
            for key in candidates:
                if self.backlog.get(_uuid(key, "an issue"))["project"] != project:
                    raise ProjectInvalid("Select issues from the named project.")
            return Intent(
                f"Request a specialist opinion for {len(candidates)} issues in {project}.",
                project,
                "A new comparison may use the project’s API allowance.",
            )
        if name == "classify_issue":
            category = arguments.get("type")
            if category not in ("bug", "enhancement") or self._issue_type(
                arguments
            ) != arguments.get("_previous"):
                raise ProjectConflict("Refresh the issue classification before applying it.")
            item = self.backlog.get(_uuid(arguments.get("item"), "an issue"))
            return Intent(
                f"Classify {project} issue {item['title']} as {category}.",
                project,
                "Confirmation applies its GitHub label once published.",
            )
        if name == "prepare_epic":
            self.plans.preview(str(project), _uuid(arguments.get("epic"), "an Epic"))
            return Intent(
                f"Prepare Epic {arguments['epic']} in {project}.",
                project,
                "This starts project execution planning.",
            )
        if name == "change_execution_plan":
            execution = self.plans.detail(str(project), _uuid(arguments.get("queue"), "a queue"))
            if (
                arguments.get("operation") not in ("classify", "save", "start")
                or type(arguments.get("version")) is not int
                or arguments["version"] != execution["version"]
            ):
                raise ProjectConflict("Read the current execution plan and its version first.")
            if not isinstance(arguments.get("types", {}), dict) or any(
                v not in ("bug", "enhancement") for v in arguments.get("types", {}).values()
            ):
                raise ProjectInvalid("Choose bug or enhancement for each issue.")
            for field in ("stories", "order"):
                value = arguments.get(field)
                if value is not None and (
                    not isinstance(value, list) or not all(isinstance(v, str) for v in value)
                ):
                    raise ProjectInvalid("Order contains invalid issue or story identifiers.")
            req = arguments.get("requires", {})
            if not isinstance(req, dict) or not all(
                isinstance(v, list) and all(isinstance(i, str) for i in v) for v in req.values()
            ):
                raise ProjectInvalid("Prerequisites must be lists of issue identifiers.")
            return Intent(
                f"{arguments['operation'].capitalize()} execution plan {execution['name']} "
                f"in {project} (revision {execution['version']}).",
                project,
                "Review the requested changes to the project queue.",
            )
        if name in ("propose_flow", "retire_flow"):
            key = str((arguments.get("document") or {}).get("key") or arguments.get("flow") or "")
            exists = self.flows.exists(key)
            if exists and self.flows.owner(key) != project:
                raise ProjectInvalid("The flow belongs to another library.")
            if "_revision" in arguments and (
                exists != arguments.get("_exists")
                or (exists and self.flows.version_in_force(key) != arguments["_revision"])
                or (
                    exists
                    and "_definition" in arguments
                    and self.flows.versions(key)[0]["sha256"] != arguments["_definition"]
                )
            ):
                raise ProjectConflict("The flow has changed since it was proposed.")
        if name == "retire_flow":
            impact = self.flows.impact(key)
            operation = arguments.get("operation")
            if operation not in ("archive", "delete"):
                raise ProjectInvalid("Choose archive or delete.")
            if any(impact[k] for k in ("sections", "review_assignments", "callers", "active_runs")):
                raise ProjectConflict(
                    "Resolve the flow's assignments, callers and active runs first."
                )
            return Intent(
                f"{operation.capitalize()} {impact['flow']} in {project or 'Global Settings'} "
                f"({len(impact['versions'])} versions).",
                project,
                "Confirm the workflow retirement.",
            )
        if name == "propose_flow":
            document = self._flow_document(arguments)
            if len(_text(arguments)) >= 20_000:
                raise ProjectInvalid(
                    "The flow is too long to propose in one piece: word the prompts more briefly"
                )
            wrong = validate.errors(document)
            if wrong:
                raise ProjectInvalid(
                    "The draft breaks a rule, so it is not proposed: "
                    + " ".join(problem["text"] for problem in wrong[:4])
                    + " Correct it, check it with check_flow, and propose it again."
                )
            exists = self.flows.exists(document.key)
            if exists and self.flows.draft(document.key) is not None:
                raise ProjectConflict(
                    f"The flow {document.key} has a draft that is being worked on. The "
                    "administrator puts it in force or discards it first."
                )
            current = self.flows.version_in_force(document.key) if exists else None
            if current is not None:
                same = self.flows.document(document.key, current)
                if same is not None and same.sha256() == document.sha256():
                    raise ProjectInvalid("That is the flow as it is in force: nothing would change")
            went = simulate(document)
            path = " → ".join(step["task"] for step in went["steps"] if step["where"] != "control")[
                :240
            ]
            kind = traits(document)
            after = (
                "Accepting saves it as a draft; it is put in force on the Flows page, with "
                "your password, because it can change code."
                if kind["writes"] or kind["publishes"]
                else "Accepting puts it in force."
            )
            what = (
                f"A new version of the flow {document.name} ({document.key})"
                if exists
                else f"A new flow, {document.name} ({document.key})"
            )
            if project:
                what += f" in {project}"
            return Intent(
                f"{what}: {path}. On paper: {went['ends']['text']} {after}",
                project=project,
                confirm="A flow takes effect only when you accept it.",
            )
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
                if section == "issues":
                    brief = item.get("delivery")
                    delivery_brief(brief if isinstance(brief, dict) else {})
                if section and section not in keys:
                    raise ProjectInvalid(f"The project {project} has no section {section}")
            titles = "; ".join(_words(item.get("title"), 80) for item in items[:5])
            more = f" and {len(items) - 5} more" if len(items) > 5 else ""
            return Intent(
                f"Propose {len(items)} item{'s' if len(items) != 1 else ''} for {project}: "
                f"{titles}{more}.",
                project=project,
            )
        if name == "report_bug":
            target = str(project or "")
            parts = self._bug_parts(arguments)
            severity = str(arguments.get("severity") or "") or None
            with self.sessions() as db:
                reported_in = active_project(db, target)
                title = check_report(
                    db, reported_in, str(arguments.get("title") or ""), parts, severity
                )
                # By the title alone: a long report shares few of its words with any bug.
                alike = alike_open(db, reported_in, title, "")
            # What was understood, for the administrator to read before anything is
            # created: each part that was said, and the open bugs that look alike.
            said = [
                f"{heading}: {_words(parts[key], 150)}"
                for key, heading in BUG_PARTS
                if key in parts
            ]
            if alike:
                said.append(
                    "Open bugs that look alike: "
                    + "; ".join(
                        (f"#{bug.issue_number} " if bug.issue_number else "")
                        + _words(bug.title, 60)
                        for bug in alike
                    )
                )
            return Intent(
                f"Report a bug in {target}"
                + (f", severity {severity}" if severity else "")
                + f": {title}",
                project=target,
                confirm=_words(
                    "Accepting creates the issue on GitHub, with the bug label. "
                    + " | ".join(said),
                    600,
                ),
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
            with self.sessions() as session:
                plan = plan_for_item(
                    session,
                    item["section"]["flow"],
                    produced=bool(item["facts"].get("kind")),
                    version=item["section"].get("flow_version"),
                )
            if plan is None:
                raise ProjectInvalid(
                    f"The section {item['section']['name']} has no flow that can run yet"
                )
            if self.queues is not None:
                refusal = self.queues.budgets.may_start(item["project"])
                if refusal is not None:
                    raise ProjectConflict(f"Nothing more can start right now. {refusal}")
            return Intent(
                f"Start work on “{_words(item['title'], 120)}” ({item['project']}).",
                project=item["project"],
            )
        if name == "propose_queue":
            project = str(arguments.get("project") or "")
            self.sections.of_project(project)  # refuses a project that does not exist
            items = arguments.get("items")
            if not isinstance(items, list) or not 1 <= len(items) <= 50:
                raise ProjectInvalid("A queue is proposed with between 1 and 50 items")
            queued: list[str] = []
            for entry in items:
                if not isinstance(entry, dict) or not _words(entry.get("reason")):
                    raise ProjectInvalid("Each item of a queue needs a reason for its place")
                found = self.backlog.get(_uuid(entry.get("item"), "an item"))
                if found["project"] != project:
                    raise ProjectInvalid("A queue holds items of its own project only")
                queued.append(_words(found["title"], 60))
            if not _words(arguments.get("name")):
                raise ProjectInvalid("Give the queue a name")
            more = f" and {len(queued) - 3} more" if len(queued) > 3 else ""
            return Intent(
                f"Propose the queue “{_words(arguments.get('name'), 120)}” for {project} with "
                f"{len(queued)} item{'s' if len(queued) != 1 else ''}: "
                f"{'; '.join(queued[:3])}{more}.",
                project=project,
            )
        if name == "start_section_run":
            project = str(arguments.get("project") or "")
            wanted = str(arguments.get("section") or "")
            section = next(
                (s for s in self.sections.of_project(project) if s["key"] == wanted), None
            )
            if section is None:
                raise ProjectInvalid(f"The project has no section {wanted}")
            action = (section["flow"] or {}).get("section_action")
            if not action:
                raise ProjectInvalid(
                    f"The section {section['name']} has no flow that produces items by itself"
                )
            self._room(project)
            scope = _words(arguments.get("scope"), 200) or "the whole repository"
            return Intent(f"{action} for {project}: {scope}.", project=project)
        if name == "retry_run":
            run = self.runs.get(_uuid(arguments.get("run"), "a run"))
            if run["kind"] not in WORK_KINDS:
                raise ProjectInvalid("Only a run of work on a project can be tried again")
            if run["state"] != "failed":
                raise ProjectConflict(f"The run is {run['state']}, not failed")
            self._room(run["project"])
            tries = self.runs.tries(UUID(run["id"]))
            confirm = self._unsure(arguments)
            if confirm is None and tries >= TRIES_A_DAY:
                confirm = f"This work already ran {tries} times within a day."
            if confirm is None and run["outcome"] in WILL_REPEAT:
                confirm = "The run failed for a reason that trying again does not remove."
            return Intent(
                f"Try “{_words(run['title'], 160)}” again.", project=run["project"], confirm=confirm
            )
        if name == "extend_correction":
            run = self.runs.get(_uuid(arguments.get("run"), "a run"))
            if run["kind"] != DELIVER or not run["item_id"]:
                raise ProjectInvalid("Only the delivery of an item has rounds of correction")
            if run["state"] != "failed" or run["outcome"] not in OUT_OF_ROUNDS:
                raise ProjectConflict("The run did not fail for lack of rounds of correction")
            had = rounds_of(run["data"])
            if had >= ROUNDS_CEILING:
                raise ProjectConflict(
                    f"The run already had {had} rounds, the most the platform allows"
                )
            self._room(run["project"])
            tries = self.runs.tries(UUID(run["id"]))
            confirm = self._unsure(arguments)
            if confirm is None and tries >= TRIES_A_DAY:
                confirm = f"This work already ran {tries} times within a day."
            return Intent(
                f"Try “{_words(run['title'], 140)}” again with {ROUNDS_CEILING} rounds of "
                f"correction instead of {had}.",
                project=run["project"],
                confirm=confirm,
            )
        if name == "pause_project":
            project = str(arguments.get("project") or "")
            record = next((r for r in self.projects.active() if r.name == project), None)
            if record is None:
                raise ProjectNotFound(project)
            if record.paused_at is not None:
                raise ProjectConflict("The project is paused already")
            failed = self.runs.failures(project)
            confirm = self._unsure(arguments)
            if confirm is None and failed < FAILURES_TO_PAUSE:
                confirm = (
                    f"Only {failed} run(s) of the project failed within a day; pausing it "
                    "for that is yours to decide."
                )
            return Intent(
                f"Pause the project {record.display_name}: {_words(arguments.get('reason'), 240)}",
                project=project,
                confirm=confirm,
            )
        if name == "cancel_run":
            run = self.runs.get(_uuid(arguments.get("run"), "a run"))
            if run["kind"] not in WORK_KINDS:
                raise ProjectInvalid("Only a run of work on a project can be stopped this way")
            if run["state"] not in ("queued", "running", "waiting"):
                raise ProjectConflict(f"The run has already ended ({run['state']})")
            holds = run["state"] == "waiting" or any(
                step["name"] == "implement" and step["state"] == "succeeded"
                for step in run["steps"]
            )
            return Intent(
                f"Stop the run “{_words(run['title'], 160)}”.",
                project=run["project"],
                confirm="The run holds work that would be discarded." if holds else None,
            )
        if name == "skip_item":
            item = self._item(arguments)
            if item["state"] not in OPEN_STATES:
                raise ProjectInvalid("The item is not open any more")
            if (item.get("run") or {}).get("state") in ("queued", "running", "waiting"):
                raise ProjectConflict("A run for this item is under way: stop it first")
            return Intent(
                f"Skip “{_words(item['title'], 120)}”: {_words(arguments.get('reason'), 240)}",
                project=item["project"],
                confirm=self._unsure(arguments),
            )
        if name == "requeue_item":
            queue = self._queues().get(_uuid(arguments.get("queue"), "a queue"))
            wanted = str(_uuid(arguments.get("item"), "an item"))
            entry = next((e for e in queue["entries"] if e["item_id"] == wanted), None)
            if entry is None:
                raise ProjectInvalid("The item is not in that queue")
            if entry["state"] != "failed":
                raise ProjectConflict(f"The item is {entry['state']} in the queue, not failed")
            return Intent(
                f"Return “{_words(entry['title'], 120)}” to the queue “"
                f"{_words(queue['name'], 120)}”.",
                project=queue["project"],
                confirm="It was returned to the queue once already."
                if self._requeued(queue["id"], wanted)
                else None,
            )
        if name == "conclude_incident":
            incident = self._operations().get(_uuid(arguments.get("incident"), "an incident"))
            if incident["state"] not in ("open", "looking", "waiting"):
                raise ProjectConflict("The incident is closed already")
            if not _words(arguments.get("diagnosis")):
                raise ProjectInvalid("Say what the cause is")
            return Intent(
                f"Close the incident: {_words(incident['summary'], 200)}",
                project=incident["project"],
            )
        if name in ("reorder_queue", "start_queue", "pause_queue"):
            queue = self._queues().get(_uuid(arguments.get("queue"), "a queue"))
            label = f"“{_words(queue['name'], 120)}” ({queue['project']})"
            if name == "reorder_queue":
                if queue["state"] == "finished":
                    raise ProjectConflict("The queue has finished")
                waiting = {e["item_id"] for e in queue["entries"] if e["state"] == "waiting"}
                given = [str(item) for item in arguments.get("items") or []]
                if set(given) != waiting or len(given) != len(waiting):
                    raise ProjectInvalid(
                        "Name every item of the queue that still waits, once, in the new order"
                    )
                own_draft = queue["proposed_by"] == "supervisor" and queue["state"] == "draft"
                return Intent(
                    f"Reorder the queue {label}.",
                    project=queue["project"],
                    confirm=None
                    if own_draft
                    else "It would reorder a queue you made, or one that is under way.",
                )
            if name == "start_queue":
                if queue["state"] not in ("draft", "paused"):
                    raise ProjectConflict(f"The queue is {queue['state']}")
                if not queue["counts"]["waiting"]:
                    raise ProjectInvalid("No item of the queue waits to be worked on")
                verb = "Start" if queue["state"] == "draft" else "Resume"
                return Intent(f"{verb} the queue {label}.", project=queue["project"])
            if queue["state"] != "running":
                raise ProjectConflict(f"The queue is {queue['state']}, not running")
            return Intent(f"Pause the queue {label}.", project=queue["project"])
        raise ProjectInvalid("That tool changes nothing")

    def _accept_flow(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Take a proposed flow into the library. One that only reads and produces items
        is put in force; one that can change code stays a draft until the administrator
        puts it in force with the password."""
        document = self._flow_document(arguments)
        raw = document.as_dict()
        note = "Drafted by the supervisor"
        if self.flows.exists(document.key):
            saved = self.flows.save_draft(document.key, raw, note=note)
        else:
            saved = self.flows.create(raw, note=note, project=arguments.get("project"))
        kind = traits(document)
        if kind["writes"] or kind["publishes"]:
            return {
                "flow": saved.get("flow", document.key),
                "version": saved["version"],
                "state": "draft",
                "next": "Put it in force on the Flows page.",
            }
        published = self.flows.publish(
            saved.get("flow", document.key),
            note="Drafted by the supervisor, accepted by the administrator",
        )
        return {
            "flow": saved.get("flow", document.key),
            "version": published["version"],
            "state": "in force",
        }

    @staticmethod
    def _bug_parts(arguments: dict[str, Any]) -> dict[str, str]:
        """The parts of a bug's report that were said, as its issue's text is made of."""
        return {
            key: str(arguments[key]).strip()
            for key, _heading in BUG_PARTS
            if isinstance(arguments.get(key), str) and arguments[key].strip()
        }

    def _perform(self, name: str, arguments: dict[str, Any], run_id: UUID | None = None) -> Any:
        if name == "report_bug":
            # Kept here and queued: the worker that writes to GitHub creates the issue,
            # with the bug label and the severity, within half a minute.
            reported, _action = draft_report(
                self.sessions,
                str(arguments["project"]),
                str(arguments["title"]),
                self._bug_parts(arguments),
                str(arguments.get("severity") or "") or None,
            )
            return {
                "bug": str(reported),
                "created": "Its issue is being created on GitHub; it has a number within "
                "a minute, on the project's Bugs page.",
            }
        if name == "remember_approach":
            return self.learning.approach(arguments)
        if name == "propose_process_issue":
            return self.learning.issue(arguments, create=True)
        if name == "ask_specialist":
            return advice.enqueue(
                self.sessions, arguments["project"], arguments["request"], arguments["issues"]
            )
        if name == "classify_issue":
            with self.sessions.begin() as db:
                p = active_project(db, str(arguments["project"]), lock=True)
                item = db.get(
                    BacklogItem, _uuid(arguments["item"], "an issue"), with_for_update=True
                )
                if (
                    not item
                    or item.project_id != p.id
                    or [item.execution_type, item.type_confirmed] != arguments["_previous"]
                ):
                    raise ProjectConflict("The issue classification changed.")
                confirm_classification(db, p, item, arguments["type"])
                return {"issue": str(item.id), "type": arguments["type"]}
        if name == "prepare_epic":
            return self.plans.create(arguments["project"], _uuid(arguments["epic"], "an Epic"))
        if name == "change_execution_plan":
            return self.plans.change(
                arguments["project"],
                _uuid(arguments["queue"], "a queue"),
                arguments["version"],
                arguments["operation"],
                types=arguments.get("types", {}),
                stories=arguments.get("stories"),
                order=arguments.get("order"),
                requires=arguments.get("requires"),
            )
        if name == "retire_flow":
            return self.flows.remove(
                str(arguments["flow"]),
                archive=arguments["operation"] == "archive",
                require_unassigned=True,
            )
        if name == "propose_flow":
            return self._accept_flow(arguments)
        if name == "propose_items":
            project = str(arguments["project"])
            proposed, repeated = [], []
            for item in arguments["items"]:
                if item.get("section") == "issues":
                    title = clean_title(str(item["title"]))
                    with self.sessions.begin() as db:
                        result = draft_issue(
                            db,
                            active_project(db, project),
                            title=title,
                            body=delivery_brief(item["delivery"]),
                            origin="supervisor-issue:"
                            + hashlib.sha256(title.casefold().encode()).hexdigest(),
                            category=None,
                            facts={},
                        )
                    if result["existing"]:
                        repeated.append(result["title"])
                    else:
                        proposed.append(result)
                    continue
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
            run = self.runs.start_item(item_id, toolchain=toolchain)
            return {"run": run["id"], "state": run["state"]}
        if name == "start_section_run":
            project = str(arguments["project"])
            run = self.runs.start_for_section(
                project,
                str(arguments["section"]),
                scope=str(arguments.get("scope") or ""),
                toolchain=toolchain_for(project).as_dict(),
            )
            return {"run": run["id"], "state": run["state"]}
        if name == "retry_run":
            failed = self.runs.get(UUID(str(arguments["run"])))
            project = str(failed["project"])
            toolchain = toolchain_for(project).as_dict()
            if failed["item_id"]:
                run = self.runs.start_item(UUID(failed["item_id"]), toolchain=toolchain)
            else:
                data = failed["data"]
                run = self.runs.start_for_section(
                    project,
                    str(data.get("section") or ""),
                    scope=str((data.get("scope") or {}).get("text") or ""),
                    toolchain=toolchain,
                )
            return {"run": run["id"], "state": run["state"]}
        if name == "extend_correction":
            failed = self.runs.get(UUID(str(arguments["run"])))
            run = self.runs.start_item(
                UUID(failed["item_id"]),
                toolchain=toolchain_for(str(failed["project"])).as_dict(),
                max_rounds=ROUNDS_CEILING,
            )
            return {"run": run["id"], "state": run["state"], "rounds": ROUNDS_CEILING}
        if name == "pause_project":
            paused = self.projects.pause(
                str(arguments["project"]),
                reason=_words(arguments.get("reason"), 300),
                by="supervisor",
            )
            return {"project": paused["project"], "paused": True}
        if name == "cancel_run":
            run_to_stop = UUID(str(arguments["run"]))
            if self.runs.get(run_to_stop)["state"] == "waiting":
                # A run that waits with its change is ended by discarding the change.
                stopped = self.runs.decide(run_to_stop, "discard")
            else:
                stopped = self.runs.request_cancel(run_to_stop)
            return {"run": stopped["id"], "state": stopped["state"]}
        if name == "skip_item":
            item_id = UUID(str(arguments["item"]))
            skipped = self.backlog.set_state(item_id, "skipped")
            self.backlog.note_facts(
                item_id,
                {"skipped_by": "supervisor", "skipped_reason": _words(arguments.get("reason"))},
            )
            return {"item": skipped["id"], "state": skipped["state"]}
        if name == "requeue_item":
            queue = self._queues().requeue(
                UUID(str(arguments["queue"])),
                UUID(str(arguments["item"])),
                _words(arguments.get("reason"), 200),
            )
            return {
                "queue": queue["id"],
                "state": queue["state"],
                "waiting": queue["counts"]["waiting"],
            }
        if name == "conclude_incident":
            said = _words(arguments.get("diagnosis"), 4000)
            done = _words(arguments.get("action"), 1000)
            closed = self._operations().conclude(
                UUID(str(arguments["incident"])),
                diagnosis=f"{said}\n\nWhat was done: {done}" if done else said,
                confidence=str(arguments.get("confidence") or ""),
                run_id=run_id,
            )
            return {"incident": closed["id"], "state": closed["state"]}
        if name == "propose_queue":
            entries = arguments["items"]
            made = self._queues().create(
                str(arguments["project"]),
                name=str(arguments["name"]),
                item_ids=[UUID(str(entry["item"])) for entry in entries],
                proposed_by="supervisor",
                reason=str(arguments.get("reason") or ""),
                reasons={UUID(str(entry["item"])): str(entry["reason"]) for entry in entries},
            )
            return {"queue": made["id"], "state": made["state"], "items": made["counts"]["total"]}
        if name == "reorder_queue":
            queue = self._queues().reorder(
                UUID(str(arguments["queue"])), [UUID(str(item)) for item in arguments["items"]]
            )
            return {"queue": queue["id"], "order": [e["title"] for e in queue["entries"]][:20]}
        if name == "start_queue":
            queue = self._queues().start(UUID(str(arguments["queue"])))
            return {"queue": queue["id"], "state": queue["state"]}
        if name == "pause_queue":
            queue = self._queues().pause(
                UUID(str(arguments["queue"])),
                f"Paused by the supervisor: {_words(arguments.get('reason'), 240)}",
            )
            return {"queue": queue["id"], "state": queue["state"]}
        raise ProjectInvalid("That tool changes nothing")

    def _room(self, project: str | None) -> None:
        """Refuse when the budgets leave no room for another run on the project."""
        if self.queues is not None and project:
            refusal = self.queues.budgets.may_start(project)
            if refusal is not None:
                raise ProjectConflict(f"Nothing more can start right now. {refusal}")

    @staticmethod
    def _unsure(arguments: dict[str, Any]) -> str | None:
        """Why an action waits when the supervisor is not sure of its diagnosis."""
        confidence = str(arguments.get("confidence") or "").lower()
        if confidence == "high":
            return None
        return f"The supervisor is not sure (confidence: {confidence or 'not given'})."

    def _requeued(self, queue_id: str, item_id: str) -> bool:
        """Whether the supervisor already returned that item to that queue."""
        with self.sessions() as session:
            earlier = session.scalars(
                select(SupervisorAction)
                .where(
                    SupervisorAction.tool == "requeue_item",
                    SupervisorAction.outcome.in_(("applied", "accepted")),
                )
                .order_by(SupervisorAction.created_at.desc())
                .limit(200)
            ).all()
            return any(
                str((action.arguments or {}).get("queue")) == queue_id
                and str((action.arguments or {}).get("item")) == item_id
                for action in earlier
            )

    # --- the administrator's side --------------------------------------------------------

    def pending(self, *, thread: str | None = None) -> list[dict[str, Any]]:
        """Actions that wait for the administrator, as inbox entries, oldest first."""
        with self.sessions() as session:
            query = (
                select(SupervisorAction, ProjectRecord)
                .outerjoin(ProjectRecord, SupervisorAction.project_id == ProjectRecord.id)
                .where(SupervisorAction.outcome == "pending")
            )
            if thread is not None:
                origin = SandboxRun.data["thread"].as_string()
                query = query.outerjoin(SandboxRun, SupervisorAction.run_id == SandboxRun.id).where(
                    origin == thread if thread else origin.is_(None)
                )
            rows = session.execute(query.order_by(SupervisorAction.created_at).limit(100)).all()
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
                    "arguments": {
                        k: v for k, v in (action.arguments or {}).items() if not k.startswith("_")
                    },
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

    def decide(
        self, action_id: UUID, *, accept: bool, via: str, thread: str | None = None
    ) -> dict[str, Any]:
        """The administrator's answer to an action that waits: carry it out, or not."""
        with self.sessions.begin() as session:
            action = session.scalar(
                select(SupervisorAction).where(SupervisorAction.id == action_id).with_for_update()
            )
            if action is None:
                raise ProjectNotFound(str(action_id))
            run = session.get(SandboxRun, action.run_id) if action.run_id else None
            origin = (run.data or {}).get("thread", "") if run else ""
            original_scope = (run.data or {}).get("scope") if run else None
            if thread is not None and thread != origin:
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
                    action.result = self._perform(
                        action.tool, dict(action.arguments or {}), action.run_id
                    )
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
                "result": action.result,
            }
        if origin and row.get("result"):
            said += " " + _text(row["result"])[:900]
        self.conversation.note(said, thread=origin, scope=original_scope)
        if row["outcome"] == "failed":
            raise ProjectConflict(str(row["detail"]))
        return row
