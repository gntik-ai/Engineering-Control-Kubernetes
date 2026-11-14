"""Runs and their steps, as records.

The console creates a run and reads it; the launcher moves it forward. A run is ``queued``
until its first sandbox starts, ``running`` while a step runs or waits for its turn, and
ends ``succeeded``, ``failed`` or ``cancelled``. An account is leased to a step for as long
as the step is running: that, counted against the account's limit, is all a lease is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    application_host,
    paused_refusal,
)
from engineering_control.db.models import (
    BacklogItem,
    Budget,
    FlowVersion,
    ProjectRecord,
    ProjectSection,
    ProviderAccount,
    SandboxRun,
    SandboxStep,
    SupervisorDefinition,
)
from engineering_control.engine.builtin import BUILTIN, FLOW_OF_KIND, kind_of
from engineering_control.engine.library import (
    document_in_force,
    plan_for_item,
    section_action,
    section_word,
    traits,
)
from engineering_control.engine.records import in_force
from engineering_control.flow_library import EVALUATION, FLOW, WORK_KINDS
from engineering_control.sandbox.pods import identity_secret

ACTIVE: Final = ("queued", "running", "waiting")
# The kind of run that is a turn of the supervisor's conversation.
CONVERSATION: Final = "supervisor_turn"
FINISHED: Final = ("succeeded", "failed", "cancelled")
MAX_LOG_CHARACTERS: Final = 400_000
# Clients that renew their sign-in in place need a volume of the account's own.
KEEPS_HOME: Final = frozenset({"chatgpt"})


@dataclass(frozen=True)
class StepWork:
    """What the launcher needs to know about the step it is to move forward."""

    run_id: UUID
    run_kind: str
    run_data: dict[str, Any]
    cancel_requested: bool
    step_id: UUID
    name: str
    position: int
    state: str
    provider: str | None
    account_id: UUID | None
    pod_name: str | None
    attempt: int
    started_at: datetime | None
    input: dict[str, Any]


@dataclass(frozen=True)
class Lease:
    account_id: UUID
    provider: str
    generation: int

    @property
    def keeps_home(self) -> bool:
        return self.provider in KEEPS_HOME


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _step_row(step: SandboxStep, *, with_log: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": str(step.id),
        "position": step.position,
        "name": step.name,
        "state": step.state,
        "provider": step.provider,
        "account_id": str(step.account_id) if step.account_id else None,
        "attempt": step.attempt,
        "exit_code": step.exit_code,
        "outcome": step.outcome,
        "detail": step.detail,
        "started_at": _when(step.started_at),
        "finished_at": _when(step.finished_at),
        "usage": step.usage,
    }
    if with_log:
        row["log"] = step.log or ""
        row["input"] = step.input
        row["output"] = _shown(step.output)
    return row


def _shown(output: dict[str, Any] | None) -> dict[str, Any] | None:
    """A step's output as the console may have it: the files of a change by name, never
    their content."""
    if not output or not isinstance(output.get("files"), list):
        return output
    files = [
        {"path": file.get("path"), "status": file.get("status")}
        for file in output["files"]
        if isinstance(file, dict)
    ]
    return {**{key: value for key, value in output.items() if key != "diff"}, "files": files}


def item_facts(item: BacklogItem) -> dict[str, Any]:
    """What a run carries about the item it works on beyond its text: its labels, where
    it comes from, and what a run made it as."""
    facts = item.facts or {}
    return {
        "labels": [str(label)[:100] for label in (item.issue_labels or [])[:50]],
        "by": "github" if item.source == "github" else (item.proposed_by or "administrator"),
        **({"kind": str(facts["kind"])} if facts.get("kind") else {}),
        **({"severity": str(facts["severity"])} if facts.get("severity") else {}),
    }


def _run_row(run: SandboxRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "kind": run.kind,
        "state": run.state,
        "title": run.title,
        "outcome": run.outcome,
        "detail": run.detail,
        "cancel_requested": run.cancel_requested,
        "account_id": str(run.account_id) if run.account_id else None,
        "item_id": str(run.item_id) if run.item_id else None,
        "project": (run.data or {}).get("project"),
        "project_name": (run.data or {}).get("project_name"),
        "data": _shown_data(run.data or {}),
        "created_at": _when(run.created_at),
        "started_at": _when(run.started_at),
        "finished_at": _when(run.finished_at),
        "run": run.title,
    }


def _shown_data(data: dict[str, Any]) -> dict[str, Any]:
    """A run's data as the console may have it: the issue without its whole text.

    The digest of a supervisor turn's tool token is for the tool service alone.
    """
    if "tools_token" in data or "flow_document" in data:
        # A run of the evaluation lab carries the flow it tries: the lab shows it.
        data = {k: v for k, v in data.items() if k not in ("tools_token", "flow_document")}
    issue = data.get("issue")
    if isinstance(issue, dict) and "body" in issue:
        data = {**data, "issue": {key: value for key, value in issue.items() if key != "body"}}
    return data


class Runs:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    # --- the console ---------------------------------------------------------------------

    def start_account_check(self, account_id: UUID) -> dict[str, Any]:
        """Try an account: one step that asks its client a trivial question."""
        with self.sessions.begin() as session:
            account = session.scalar(
                select(ProviderAccount)
                .where(ProviderAccount.id == account_id, ProviderAccount.archived_at.is_(None))
                .with_for_update()
            )
            if account is None:
                raise ProjectNotFound(str(account_id))
            if account.credential_generation < 1 or account.state == "signed_out":
                raise ProjectInvalid("Sign the account in first: there is nothing to try yet")
            busy = session.scalar(
                select(func.count())
                .select_from(SandboxRun)
                .where(
                    SandboxRun.kind == "account_check",
                    SandboxRun.account_id == account.id,
                    SandboxRun.state.in_(ACTIVE),
                )
            )
            if busy:
                raise ProjectConflict(f"{account.name} is already being tried")
            run = SandboxRun(
                kind="account_check",
                state="queued",
                title=f"Try {account.name}",
                account_id=account.id,
                cancel_requested=False,
                data={"provider": account.provider, "account": account.name},
            )
            session.add(run)
            session.flush()
            session.add(
                SandboxStep(
                    run_id=run.id,
                    position=1,
                    name="check",
                    state="pending",
                    provider=account.provider,
                    account_id=account.id,
                    attempt=0,
                )
            )
            session.flush()
            return _run_row(run)

    def start_item(
        self, item_id: UUID, *, toolchain: dict[str, Any], max_rounds: int | None = None
    ) -> dict[str, Any]:
        """Start work on a backlog item: the run its section's flow says.

        Most items are delivered: a run implements what the item asks for, has it reviewed
        and tested, and then waits for the administrator before anything reaches GitHub.
        In a section that audits, an item that is not a finding is a request, and the run
        is an audit of what it names.
        """
        with self.sessions.begin() as session:
            found = session.execute(
                select(BacklogItem, ProjectRecord, ProjectSection)
                .join(ProjectRecord, BacklogItem.project_id == ProjectRecord.id)
                .outerjoin(ProjectSection, BacklogItem.section_id == ProjectSection.id)
                .where(BacklogItem.id == item_id, ProjectRecord.archived_at.is_(None))
                .with_for_update(of=BacklogItem)
            ).first()
            if found is None:
                raise ProjectNotFound(str(item_id))
            item, project, section = found
            if refusal := paused_refusal(project):
                raise ProjectConflict(refusal)
            if item.state not in ("proposed", "accepted"):
                raise ProjectInvalid("The item is not open any more")
            if section is None:
                raise ProjectInvalid("Route the item to a section first")
            plan = plan_for_item(session, section.flow_key, produced=is_produced(item))
            if plan is None:
                raise ProjectInvalid(
                    f"The section {section.name} has no flow that can run here. Assign "
                    "Deliver issue to it on the project's page."
                    if section.flow_key is None
                    else f"The flow of the section {section.name} cannot run on this "
                    "platform yet. Assign another one on the project's page."
                )
            repository = item.repository or project.source_repository
            if not repository:
                raise ProjectInvalid("Connect a repository to the project first")
            busy = session.scalar(
                select(func.count())
                .select_from(SandboxRun)
                .where(SandboxRun.item_id == item.id, SandboxRun.state.in_(ACTIVE))
            )
            if busy:
                raise ProjectConflict("A run for this item is already under way")
            flow, mode = plan
            found_flow = document_in_force(session, flow)
            if found_flow is None:  # ``plan_for_item`` names only flows that are in force
                raise ProjectInvalid(f"The flow {flow} is not in force")
            document, flow_version = found_flow
            kind = kind_of(flow)
            numbered = item.issue_number is not None
            title = f"#{item.issue_number} {item.title}" if numbered else item.title
            if mode == "scope":
                title = f"{section_word(document)}: {title}"
            run = SandboxRun(
                kind=kind,
                state="queued",
                title=title[:300],
                project_id=project.id,
                item_id=item.id,
                cancel_requested=False,
                data={},
            )
            session.add(run)
            session.flush()
            start = document.start
            common = {
                # The flow the run follows, at the version in force now: it keeps it.
                "flow": {"key": flow, "version": flow_version, "sha256": document.sha256()},
                "project": project.name,
                "project_name": project.display_name,
                "repository": repository,
                "base_branch": project.default_branch or "main",
                "toolchain": toolchain,
                # What the item is, for an If of the flow to ask about.
                "item": item_facts(item),
            }
            if traits(document)["browses"] and not project.application_url:
                raise ProjectInvalid(
                    "Say where the project's application runs first, on the project's page"
                )
            if traits(document)["browses"]:
                common = {**common, "application": application_of(project)}
            if mode == "scope":
                run.data = {
                    **common,
                    "section": section.key,
                    "scope": {"title": item.title, "text": item.description or ""},
                }
            else:
                name = f"issue-{item.issue_number}" if numbered else f"item-{item.id.hex[:8]}"
                run.data = {
                    **common,
                    "branch": f"engineering-control/{name}-{run.id.hex[:8]}",
                    # What is to be done. For an issue of the repository its text is read
                    # from GitHub when the run starts; an item of the platform's own
                    # carries its text itself.
                    "issue": {
                        "number": item.issue_number,
                        "title": item.title,
                        "url": item.issue_url,
                        **({} if numbered else {"body": item.description or ""}),
                    },
                }
                if max_rounds is not None:
                    # More rounds of correction than usual, granted for this run.
                    run.data = {**run.data, "max_rounds": int(max_rounds)}
            session.add(
                SandboxStep(run_id=run.id, position=1, name=start, state="pending", attempt=0)
            )
            # Starting work on an item is deciding that it is wanted.
            if item.state == "proposed":
                item.state = "accepted"
            session.flush()
            return _run_row(run)

    def start_delivery(self, item_id: UUID, *, toolchain: dict[str, Any]) -> dict[str, Any]:
        """The older name of ``start_item``, from when every item was delivered."""
        return self.start_item(item_id, toolchain=toolchain)

    def start_for_section(
        self, project: str, section: str, *, scope: str, toolchain: dict[str, Any]
    ) -> dict[str, Any]:
        """Start what a section runs by itself to produce items: an audit of the code, or
        a search for enhancements. ``scope`` narrows it, in the administrator's words;
        empty means the whole repository."""
        with self.sessions.begin() as session:
            record = session.scalar(
                select(ProjectRecord)
                .where(ProjectRecord.name == project, ProjectRecord.archived_at.is_(None))
                .with_for_update()
            )
            if record is None:
                raise ProjectNotFound(project)
            if refusal := paused_refusal(record):
                raise ProjectConflict(refusal)
            found = session.scalar(
                select(ProjectSection).where(
                    ProjectSection.project_id == record.id, ProjectSection.key == section
                )
            )
            if found is None:
                raise ProjectNotFound(section)
            in_force_now = document_in_force(session, found.flow_key) if found.flow_key else None
            if in_force_now is None or section_action(in_force_now[0]) is None:
                raise ProjectInvalid(
                    f"The section {found.name} has no flow that produces items by itself"
                )
            document, flow_version = in_force_now
            kind = kind_of(document.key)
            if not record.source_repository:
                raise ProjectInvalid("Connect a repository to the project first")
            busy = session.scalar(
                select(func.count())
                .select_from(SandboxRun)
                .where(
                    SandboxRun.project_id == record.id,
                    SandboxRun.kind == kind,
                    SandboxRun.item_id.is_(None),
                    SandboxRun.state.in_(ACTIVE),
                )
            )
            if kind == FLOW:
                # Every flow made here is the same kind of run: which one, the run says.
                busy = sum(
                    1
                    for data in session.scalars(
                        select(SandboxRun.data).where(
                            SandboxRun.project_id == record.id,
                            SandboxRun.kind == kind,
                            SandboxRun.item_id.is_(None),
                            SandboxRun.state.in_(ACTIVE),
                        )
                    )
                    if ((data or {}).get("flow") or {}).get("key") == document.key
                )
            if busy:
                raise ProjectConflict(f"{document.name} is already under way for this project")
            narrowed = " ".join(scope.split())[:2000]
            what = section_word(document)
            where = narrowed[:200] if narrowed else "the whole repository"
            if traits(document)["browses"]:
                if not record.application_url:
                    raise ProjectInvalid(
                        "Say where the project's application runs first, on the project's page"
                    )
                where = narrowed[:200] if narrowed else "the whole application"
            run = SandboxRun(
                kind=kind,
                state="queued",
                title=f"{what}: {where}"[:300],
                project_id=record.id,
                cancel_requested=False,
                data={
                    "flow": {
                        "key": document.key,
                        "version": flow_version,
                        "sha256": document.sha256(),
                    },
                    "project": record.name,
                    "project_name": record.display_name,
                    "repository": record.source_repository,
                    "base_branch": record.default_branch or "main",
                    "toolchain": toolchain,
                    "section": found.key,
                    "scope": {"title": "", "text": narrowed},
                    **(
                        {"application": application_of(record)}
                        if traits(document)["browses"]
                        else {}
                    ),
                },
            )
            session.add(run)
            session.flush()
            session.add(
                SandboxStep(
                    run_id=run.id,
                    position=1,
                    name=document.start,
                    state="pending",
                    attempt=0,
                )
            )
            session.flush()
            return _run_row(run)

    def for_items(self, item_ids: list[str]) -> dict[str, dict[str, Any]]:
        """The latest run of each of the items, by item."""
        if not item_ids:
            return {}
        wanted = [UUID(item_id) for item_id in item_ids]
        with self.sessions() as session:
            runs = session.scalars(
                select(SandboxRun)
                .where(SandboxRun.item_id.in_(wanted))
                .order_by(SandboxRun.created_at)
            ).all()
            return {
                str(run.item_id): {"id": str(run.id), "state": run.state, "outcome": run.outcome}
                for run in runs
            }

    def inbox(self) -> list[dict[str, Any]]:
        """Runs that wait for the administrator."""
        with self.sessions() as session:
            waiting = session.scalars(
                select(SandboxRun)
                .where(SandboxRun.state == "waiting")
                .order_by(SandboxRun.created_at)
            ).all()
            return [
                {
                    "kind": "run_waiting",
                    "project": (run.data or {}).get("project"),
                    "project_name": (run.data or {}).get("project_name"),
                    "section": None,
                    "count": 1,
                    "detail": run.detail,
                    "run_id": str(run.id),
                    "run": run.title,
                    "outcome": run.outcome,
                }
                for run in waiting
            ]

    def accounts_being_tried(self) -> set[str]:
        with self.sessions() as session:
            return {
                str(account_id)
                for account_id in session.scalars(
                    select(SandboxRun.account_id).where(
                        SandboxRun.kind == "account_check",
                        SandboxRun.state.in_(ACTIVE),
                        SandboxRun.account_id.is_not(None),
                    )
                )
            }

    def account_in_use(self, account_id: UUID) -> bool:
        """Whether a sandbox is working with the account, or a trial of it is under way."""
        with self.sessions() as session:
            running = session.scalar(
                select(func.count())
                .select_from(SandboxStep)
                .where(SandboxStep.account_id == account_id, SandboxStep.state == "running")
            )
            trying = session.scalar(
                select(func.count())
                .select_from(SandboxRun)
                .where(SandboxRun.account_id == account_id, SandboxRun.state.in_(ACTIVE))
            )
            return bool(running or trying)

    def listing(
        self,
        *,
        limit: int = 50,
        state: str | None = None,
        project: str | None = None,
        conversation: bool = False,
    ) -> dict[str, Any]:
        """Runs, newest first. The supervisor's turns are a conversation, not work: they
        are listed only when asked for."""
        query = select(SandboxRun).order_by(SandboxRun.created_at.desc())
        query = query.where(
            SandboxRun.kind == CONVERSATION if conversation else SandboxRun.kind != CONVERSATION
        )
        if state is not None:
            query = query.where(SandboxRun.state == state)
        if project is not None:
            query = query.join(ProjectRecord, SandboxRun.project_id == ProjectRecord.id).where(
                ProjectRecord.name == project
            )
        with self.sessions() as session:
            runs = session.scalars(query.limit(max(1, min(limit, 200)))).all()
            used = usage_of(session, [run.id for run in runs])
            return {
                "items": [{**_run_row(run), "usage": used.get(run.id)} for run in runs],
                "total": len(runs),
            }

    def get(self, run_id: UUID) -> dict[str, Any]:
        with self.sessions() as session:
            run = session.get(SandboxRun, run_id)
            if run is None:
                raise ProjectNotFound(str(run_id))
            steps = session.scalars(
                select(SandboxStep)
                .where(SandboxStep.run_id == run.id)
                .order_by(SandboxStep.position)
            ).all()
            return {
                **_run_row(run),
                "usage": usage_of(session, [run.id]).get(run.id),
                "steps": [_step_row(step, with_log=True) for step in steps],
            }

    def flow_of(self, run_id: UUID) -> dict[str, Any]:
        """The flow the run follows, as the document of the version it started with: what
        its page draws the run on."""
        with self.sessions() as session:
            run = session.get(SandboxRun, run_id)
            if run is None:
                raise ProjectNotFound(str(run_id))
            data = run.data or {}
            said = data.get("flow") or {}
            key = str(said.get("key") or FLOW_OF_KIND.get(run.kind) or "")
            version = int(said.get("version") or 0)
            carried = data.get("flow_document")
            if isinstance(carried, dict):
                # A run of the evaluation lab carries the flow it tries.
                return {"key": key, "version": version, "document": carried}
            if version:
                row = session.scalar(
                    select(FlowVersion.document).where(
                        FlowVersion.flow_key == key, FlowVersion.version == version
                    )
                )
                if isinstance(row, dict):
                    return {"key": key, "version": version, "document": row}
            if key in BUILTIN:
                return {"key": key, "version": 0, "document": BUILTIN[key].as_dict()}
            raise ProjectNotFound(str(run_id))

    def request_cancel(self, run_id: UUID) -> dict[str, Any]:
        with self.sessions.begin() as session:
            run = session.scalar(
                select(SandboxRun).where(SandboxRun.id == run_id).with_for_update()
            )
            if run is None:
                raise ProjectNotFound(str(run_id))
            if run.state in FINISHED:
                raise ProjectConflict("The run has already ended")
            run.cancel_requested = True
            session.flush()
            return _run_row(run)

    # --- the launcher --------------------------------------------------------------------

    def work(self) -> list[StepWork]:
        """For every run that is not finished, its first step that is not finished either,
        oldest run first. A run with no such step is returned with state ``done``."""
        found: list[StepWork] = []
        with self.sessions() as session:
            runs = session.scalars(
                select(SandboxRun)
                .where(SandboxRun.state.in_(("queued", "running")))
                .order_by(SandboxRun.created_at)
            ).all()
            for run in runs:
                step = session.scalar(
                    select(SandboxStep)
                    .where(
                        SandboxStep.run_id == run.id,
                        SandboxStep.state.in_(("pending", "running")),
                    )
                    .order_by(SandboxStep.position)
                    .limit(1)
                )
                if step is None:
                    last = session.scalar(
                        select(SandboxStep)
                        .where(SandboxStep.run_id == run.id)
                        .order_by(SandboxStep.position.desc())
                        .limit(1)
                    )
                    if last is None:
                        continue
                    step, state = last, "done"
                else:
                    state = step.state
                found.append(
                    StepWork(
                        run_id=run.id,
                        run_kind=run.kind,
                        run_data=dict(run.data or {}),
                        cancel_requested=run.cancel_requested,
                        step_id=step.id,
                        name=step.name,
                        position=step.position,
                        state=state,
                        provider=step.provider,
                        account_id=step.account_id,
                        pod_name=step.pod_name,
                        attempt=step.attempt,
                        started_at=step.started_at,
                        input=dict(step.input or {}),
                    )
                )
        return found

    def lease(self, step_id: UUID) -> Lease | None:
        """An account for the step, or None when none is free right now.

        A step that names its account (trying an account) gets that one, whatever state it
        is in. Otherwise the least recently used account of the step's provider that is
        signed in, not paused, not limited and below its limit of sandboxes.
        """
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            step = session.get(SandboxStep, step_id)
            if step is None or step.provider is None:
                return None
            # A step may name the providers it accepts, most wanted first, and an account
            # it would rather have.
            wanted = [name for name in step.provider.split(",") if name]
            preferred = str((step.input or {}).get("prefer_account") or "")
            # An account that is the supervisor's own is given to no other work.
            run = session.get(SandboxRun, step.run_id)
            latest = session.scalar(
                select(SupervisorDefinition).order_by(SupervisorDefinition.version.desc()).limit(1)
            )
            reserved = (
                latest.account_id
                if latest is not None
                and latest.exclusive
                and (run is None or run.kind != CONVERSATION)
                else None
            )
            using = (
                select(SandboxStep.account_id, func.count().label("running"))
                .where(SandboxStep.state == "running", SandboxStep.account_id.is_not(None))
                .group_by(SandboxStep.account_id)
                .subquery()
            )
            query = (
                select(ProviderAccount, func.coalesce(using.c.running, 0))
                .outerjoin(using, using.c.account_id == ProviderAccount.id)
                .where(
                    ProviderAccount.archived_at.is_(None),
                    ProviderAccount.provider.in_(wanted),
                    ProviderAccount.credential_generation > 0,
                )
                .order_by(ProviderAccount.last_used_at.is_not(None), ProviderAccount.last_used_at)
                .with_for_update(of=ProviderAccount)
            )
            if step.account_id is not None:
                query = query.where(ProviderAccount.id == step.account_id)
            candidates = sorted(
                session.execute(query).all(),
                key=lambda row: (str(row[0].id) != preferred, wanted.index(row[0].provider)),
            )
            spent = _spent_accounts(session, now)
            for account, running in candidates:
                if account.id in spent:
                    # Its budget of steps for a day is used up: no step gets it, not even
                    # one that names it.
                    continue
                if step.account_id is None:
                    if reserved is not None and account.id == reserved:
                        continue
                    limited = (
                        account.state == "limited"
                        and account.limited_until is not None
                        and _aware(account.limited_until) > now
                    )
                    if account.paused or limited:
                        continue
                    if account.state not in ("unchecked", "ready", "limited"):
                        continue
                if running >= account.max_concurrent:
                    continue
                step.account_id = account.id
                step.provider = account.provider
                account.last_used_at = now
                session.flush()
                return Lease(account.id, account.provider, account.credential_generation)
        return None

    def step_started(self, step_id: UUID, pod_name: str) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            step = session.get(SandboxStep, step_id)
            if step is None:
                return
            step.state = "running"
            step.pod_name = pod_name
            step.attempt += 1
            step.started_at = now
            step.detail = None
            run = session.get(SandboxRun, step.run_id)
            if run is not None and run.state == "queued":
                run.state = "running"
                run.started_at = now

    def step_waiting(self, step_id: UUID, detail: str) -> None:
        with self.sessions.begin() as session:
            step = session.get(SandboxStep, step_id)
            if step is not None and step.detail != detail:
                step.detail = detail[:600]

    def step_log(self, step_id: UUID, log: str) -> None:
        with self.sessions.begin() as session:
            step = session.get(SandboxStep, step_id)
            if step is not None:
                step.log = log[-MAX_LOG_CHARACTERS:]

    def step_finished(
        self,
        step_id: UUID,
        *,
        state: str,
        outcome: str,
        detail: str | None,
        exit_code: int | None = None,
        output: dict[str, Any] | None = None,
        log: str | None = None,
        keep_account: bool = True,
    ) -> None:
        with self.sessions.begin() as session:
            step = session.get(SandboxStep, step_id)
            if step is None:
                return
            step.state = state
            step.outcome = outcome[:40]
            step.detail = detail[:600] if detail else None
            step.exit_code = exit_code
            step.output = output
            if log is not None:
                step.log = log[-MAX_LOG_CHARACTERS:]
            step.finished_at = datetime.now(UTC)
            if not keep_account:
                step.account_id = None

    def run_finished(self, run_id: UUID, *, state: str, outcome: str, detail: str | None) -> None:
        with self.sessions.begin() as session:
            run = session.get(SandboxRun, run_id)
            if run is None or run.state in FINISHED:
                return
            run.state = state
            run.outcome = outcome[:40]
            run.detail = detail[:600] if detail else None
            run.finished_at = datetime.now(UTC)
            if run.started_at is None:
                run.started_at = run.finished_at
            # Steps that never ran are not left looking as if they still might.
            for step in session.scalars(
                select(SandboxStep).where(
                    SandboxStep.run_id == run.id, SandboxStep.state.in_(("pending", "running"))
                )
            ):
                step.state = "cancelled" if state == "cancelled" else "skipped"
                step.finished_at = run.finished_at

    def add_step(
        self, run_id: UUID, *, name: str, provider: str | None, given: dict[str, Any] | None
    ) -> None:
        """Append the step that follows; a run's steps are decided as it goes."""
        with self.sessions.begin() as session:
            last = session.scalar(
                select(func.max(SandboxStep.position)).where(SandboxStep.run_id == run_id)
            )
            session.add(
                SandboxStep(
                    run_id=run_id,
                    position=(last or 0) + 1,
                    name=name,
                    state="pending",
                    provider=provider,
                    input=given,
                    attempt=0,
                )
            )

    def merge_data(self, run_id: UUID, updates: dict[str, Any]) -> None:
        with self.sessions.begin() as session:
            run = session.get(SandboxRun, run_id)
            if run is not None and updates:
                run.data = {**(run.data or {}), **updates}

    def run_waiting(self, run_id: UUID, *, outcome: str, detail: str) -> None:
        """The run can go no further by itself: it waits for the administrator."""
        with self.sessions.begin() as session:
            run = session.get(SandboxRun, run_id)
            if run is not None and run.state not in FINISHED:
                run.state = "waiting"
                run.outcome = outcome[:40]
                run.detail = detail[:600]

    def decide(self, run_id: UUID, decision: str) -> dict[str, Any]:
        """The administrator's answer to a run that waits: publish its change, or discard it."""
        if decision not in ("publish", "discard"):
            raise ProjectInvalid("That is not a decision a run can take")
        with self.sessions.begin() as session:
            run = session.scalar(
                select(SandboxRun).where(SandboxRun.id == run_id).with_for_update()
            )
            if run is None:
                raise ProjectNotFound(str(run_id))
            if run.state != "waiting":
                raise ProjectConflict("The run is not waiting for a decision")
            run.data = {**(run.data or {}), "decision": decision}
            run.state = "running"
            run.detail = (
                "Publishing the change." if decision == "publish" else "Discarding the change."
            )
            session.flush()
            return _run_row(run)

    def step_output(self, run_id: UUID, name: str) -> dict[str, Any] | None:
        """What the run's latest step of that name produced, whole."""
        with self.sessions() as session:
            step = session.scalar(
                select(SandboxStep)
                .where(SandboxStep.run_id == run_id, SandboxStep.name == name)
                .order_by(SandboxStep.position.desc())
                .limit(1)
            )
            return dict(step.output) if step is not None and step.output else None

    def tries(self, run_id: UUID) -> int:
        """How many times within a day the work of that run was started: runs for the same
        item, or of the same kind by the same section when it has no item."""
        since = datetime.now(UTC) - timedelta(hours=24)
        with self.sessions() as session:
            run = session.get(SandboxRun, run_id)
            if run is None:
                return 0
            query = (
                select(func.count())
                .select_from(SandboxRun)
                .where(SandboxRun.kind == run.kind, SandboxRun.created_at > since)
            )
            if run.item_id is not None:
                query = query.where(SandboxRun.item_id == run.item_id)
            else:
                query = query.where(
                    SandboxRun.project_id == run.project_id, SandboxRun.item_id.is_(None)
                )
            return int(session.scalar(query) or 0)

    def failures(self, project: str) -> int:
        """How many runs of work on the project failed within a day."""
        since = datetime.now(UTC) - timedelta(hours=24)
        with self.sessions() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(SandboxRun)
                    .join(ProjectRecord, SandboxRun.project_id == ProjectRecord.id)
                    .where(
                        ProjectRecord.name == project,
                        SandboxRun.kind.in_(WORK_KINDS),
                        SandboxRun.state == "failed",
                        SandboxRun.finished_at > since,
                    )
                )
                or 0
            )

    def item_of(self, run_id: UUID) -> UUID | None:
        """The backlog item the run works on, when it works on one."""
        with self.sessions() as session:
            run = session.get(SandboxRun, run_id)
            return run.item_id if run is not None else None

    def note_step(self, step_id: UUID, noted: dict[str, Any]) -> None:
        """Add to what a step was given: which prompt, in which wording."""
        with self.sessions.begin() as session:
            step = session.get(SandboxStep, step_id)
            if step is not None and noted:
                step.input = {**(step.input or {}), **noted}

    def note_usage(self, step_id: UUID, used: dict[str, Any]) -> None:
        """What the step's agent reported using."""
        with self.sessions.begin() as session:
            step = session.get(SandboxStep, step_id)
            if step is not None:
                step.usage = used

    def usage_totals(self, *, days: int = 30) -> dict[str, Any]:
        """What the runs created in the last ``days`` days used, by project and by
        version of a flow. The supervisor's turns are counted under their own name."""
        since = datetime.now(UTC) - timedelta(days=max(1, min(days, 365)))
        with self.sessions() as session:
            runs = session.scalars(select(SandboxRun).where(SandboxRun.created_at > since)).all()
            used = usage_of(session, [run.id for run in runs])
            projects: dict[str, dict[str, Any]] = {}
            flows: dict[tuple[str, int, bool], dict[str, Any]] = {}

            def add(total: dict[str, Any], run: SandboxRun) -> None:
                spent = used.get(run.id) or {}
                total["runs"] += 1
                total["succeeded"] += run.state == "succeeded"
                total["failed"] += run.state == "failed"
                for name in ("tokens", "cached", "minutes", "sandboxes"):
                    total[name] += int(spent.get(name) or 0)

            def empty(**named: Any) -> dict[str, Any]:
                return {
                    **named,
                    "runs": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "tokens": 0,
                    "cached": 0,
                    "minutes": 0,
                    "sandboxes": 0,
                }

            for run in runs:
                data = run.data or {}
                if run.kind == "account_check":
                    continue
                name = str(data.get("project") or "")
                if name:
                    add(
                        projects.setdefault(
                            name,
                            empty(project=name, project_name=data.get("project_name") or name),
                        ),
                        run,
                    )
                flow = data.get("flow") or {}
                key = str(flow.get("key") or FLOW_OF_KIND.get(run.kind) or run.kind)
                version = int(flow.get("version") or 0)
                tried = bool(data.get("evaluation"))
                add(
                    flows.setdefault(
                        (key, version, tried), empty(key=key, version=version, evaluation=tried)
                    ),
                    run,
                )
        return {
            "days": days,
            "since": _when(since),
            "projects": sorted(projects.values(), key=lambda row: -row["tokens"]),
            "flows": sorted(
                flows.values(), key=lambda row: (row["key"], -row["version"], row["evaluation"])
            ),
        }

    def start_evaluation(
        self,
        past_run_id: UUID,
        *,
        flow: dict[str, Any],
        document: dict[str, Any],
        start: str,
        evaluation_id: UUID,
    ) -> dict[str, Any]:
        """Start a run that goes through a past run's work again on another version of
        its flow: the same words, the same commit. Nothing leaves such a run."""
        with self.sessions.begin() as session:
            past = session.get(SandboxRun, past_run_id)
            if past is None:
                raise ProjectNotFound(str(past_run_id))
            data = past.data or {}
            if not data.get("base_sha") or not data.get("repository"):
                raise ProjectInvalid("That run never checked the repository out")
            project = session.get(ProjectRecord, past.project_id) if past.project_id else None
            if project is None or project.archived_at is not None:
                raise ProjectInvalid("The project of that run is not there any more")
            if refusal := paused_refusal(project):
                raise ProjectConflict(refusal)
            kept = ("project", "project_name", "repository", "base_branch", "toolchain")
            run = SandboxRun(
                kind=EVALUATION,
                state="queued",
                title=f"Evaluation: {past.title}"[:300],
                project_id=project.id,
                cancel_requested=False,
                data={},
            )
            session.add(run)
            session.flush()
            run.data = {
                **{name: data[name] for name in kept if name in data},
                **{
                    name: data[name]
                    for name in ("issue", "scope", "section", "application")
                    if name in data
                },
                "flow": flow,
                # The flow as it was when the evaluation started: a draft may change.
                "flow_document": document,
                "evaluation": {"id": str(evaluation_id), "of_run": str(past.id)},
                "at_commit": data["base_sha"],
                "branch": f"engineering-control/evaluation-{run.id.hex[:8]}",
            }
            session.add(
                SandboxStep(run_id=run.id, position=1, name=start, state="pending", attempt=0)
            )
            session.flush()
            return _run_row(run)

    def minutes_running(self, run_id: UUID) -> int:
        """How long the run has been under way, in whole minutes."""
        with self.sessions() as session:
            run = session.get(SandboxRun, run_id)
            if run is None or run.started_at is None:
                return 0
            return int((datetime.now(UTC) - _aware(run.started_at)).total_seconds() // 60)

    def tokens_used(self, run_id: UUID) -> int:
        """How many tokens the run's agents have reported using so far."""
        with self.sessions() as session:
            used = session.scalars(
                select(SandboxStep.usage).where(
                    SandboxStep.run_id == run_id, SandboxStep.usage.is_not(None)
                )
            ).all()
            return sum(int((entry or {}).get("tokens") or 0) for entry in used)

    def step_outputs(self, run_id: UUID, name: str) -> list[dict[str, Any]]:
        """What each of the run's steps of that name produced, in the order they ran.
        Steps that failed, or were given again to another account, produced nothing."""
        with self.sessions() as session:
            steps = session.scalars(
                select(SandboxStep)
                .where(
                    SandboxStep.run_id == run_id,
                    SandboxStep.name == name,
                    SandboxStep.state == "succeeded",
                )
                .order_by(SandboxStep.position)
            ).all()
            return [dict(step.output) for step in steps if step.output]

    def account_learned(
        self,
        account_id: UUID,
        *,
        state: str,
        reason: str | None,
        limited_until: datetime | None,
    ) -> None:
        """Record what a step taught the platform about the account it worked with."""
        with self.sessions.begin() as session:
            account = session.get(ProviderAccount, account_id)
            if account is None or account.archived_at is not None:
                return
            account.state = state
            account.state_reason = reason[:300] if reason else None
            account.limited_until = limited_until
            account.checked_at = datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def usage_of(session: Session, run_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    """What each of the runs used: tokens its agents reported, and minutes in sandboxes."""
    found: dict[UUID, dict[str, Any]] = {}
    if not run_ids:
        return found
    rows = session.execute(
        select(
            SandboxStep.run_id, SandboxStep.usage, SandboxStep.started_at, SandboxStep.finished_at
        ).where(SandboxStep.run_id.in_(run_ids))
    ).all()
    seconds: dict[UUID, float] = {}
    for run_id, usage, started, finished in rows:
        total = found.setdefault(
            run_id,
            {"tokens": 0, "input": 0, "output": 0, "cached": 0, "minutes": 0, "sandboxes": 0},
        )
        for name in ("tokens", "input", "output", "cached"):
            try:
                total[name] += int((usage or {}).get(name) or 0)
            except (TypeError, ValueError):
                continue
        if started is not None:
            total["sandboxes"] += 1
            if finished is not None:
                took = (_aware(finished) - _aware(started)).total_seconds()
                seconds[run_id] = seconds.get(run_id, 0.0) + max(0.0, took)
    for run_id, took in seconds.items():
        found[run_id]["minutes"] = round(took / 60)
    return found


def first_task(session: Session, key: str, version: int) -> str:
    """The task a run of that version of the flow starts at."""
    if version:
        row = session.scalar(
            select(FlowVersion.document).where(
                FlowVersion.flow_key == key, FlowVersion.version == version
            )
        )
        if isinstance(row, dict) and row.get("start"):
            return str(row["start"])
    return BUILTIN[key].start if key in BUILTIN else "prepare"


def _flow_now(session: Session, key: str) -> dict[str, Any]:
    """The flow of that key at the version in force now, as a run records it."""
    version = in_force(session, key) or (0, "")
    return {"key": key, "version": version[0], "sha256": version[1]}


def application_of(project: ProjectRecord) -> dict[str, Any]:
    """What a step that looks at the project's application is told of it: where it runs,
    whether a test identity is stored, and the Secret that holds it. Never the identity."""
    return {
        "url": project.application_url,
        # Every host a step that looks at the application may reach: the address's own
        # and the others the project names.
        "hosts": [
            host
            for host in dict.fromkeys(
                [application_host(project.application_url), *(project.application_hosts or [])]
            )
            if host
        ],
        "identity": project.application_identity_at is not None,
        "secret": identity_secret(project.id),
    }


def is_finding(item: BacklogItem) -> bool:
    """Whether the item is something an audit found, as opposed to a request."""
    return (item.facts or {}).get("kind") == "finding"


def is_produced(item: BacklogItem) -> bool:
    """Whether a run produced the item (a finding, a proposal), as opposed to someone
    asking for something."""
    return item.source_run_id is not None or bool((item.facts or {}).get("kind"))


def _spent_accounts(session: Session, now: datetime) -> set[UUID]:
    """The accounts whose budget of steps within a day is used up (see sandbox/queues.py)."""
    since = now - timedelta(hours=24)
    spent: set[UUID] = set()
    for budget in session.scalars(
        select(Budget).where(Budget.scope == "account", Budget.per_day.is_not(None))
    ):
        if budget.scope_id is None or budget.per_day is None:
            continue
        used = session.scalar(
            select(func.count())
            .select_from(SandboxStep)
            .where(SandboxStep.account_id == budget.scope_id, SandboxStep.started_at > since)
        )
        if (used or 0) >= budget.per_day:
            spent.add(budget.scope_id)
    return spent
