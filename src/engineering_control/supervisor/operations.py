"""The supervisor's operations role: trouble with the work, noticed and looked at.

Noticing is the platform's and needs no model: a run that failed, a run that makes no
progress, a queue that paused itself. Each becomes an incident, once. What to do about it
is the supervisor's: the platform asks it in a turn of its own, and the supervisor reads
what happened and acts through its tools, which decide by the classes of the study's
section 12.3 what it may do at once (try again, skip an item with a recorded reason,
return an item to its queue, stop a run that holds no work) and what waits for the
administrator (stopping a run that holds work, anything it is not sure about, anything
past a limit). It never approves, publishes or touches a sign-in.

The administrator decides whether the supervisor looks at trouble by itself at all, and
how often within an hour. Switched off, incidents wait in the inbox instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import ProjectConflict, ProjectNotFound
from engineering_control.db.models import (
    ProjectRecord,
    SandboxRun,
    SandboxStep,
    SupervisorAction,
    SupervisorIncident,
    SupervisorMessage,
    WorkQueue,
    WorkQueueEntry,
)
from engineering_control.flow_library import WORK_KINDS
from engineering_control.supervisor.conversation import Conversation

# A step that has waited this long for an account or a sandbox is not just busy.
STALL_MINUTES: Final = 20
# Failures older than this are history, not news: the platform noticed nothing while it
# was away, and does not start on them when it comes back.
NEWS_MINUTES: Final = 60
INCIDENTS_PER_TURN: Final = 5
# How often the supervisor is asked about the same incident before it is left to the
# administrator.
MAX_LOOKS: Final = 2
OPEN: Final = ("open", "looking", "waiting")
TITLE: Final = "Supervisor (operations): "

INSTRUCTIONS: Final = """\
The platform noticed the incidents below by itself. Nobody asked you about them: look at \
each one and deal with it. For each incident:
1. Read what happened: get_run for a run (its failed step, its detail and the end of its \
output), get_queue for a queue, get_item for the item.
2. Decide what the cause is, and how sure you are (high, medium or low).
3. Act only when acting helps:
   - A cause that passes by itself (an account that was limited, a sandbox that did not \
start, a provider that was overloaded, a client that gave no answer): try again with \
retry_run.
   - A cause in the item itself (the implementer said it cannot be done, the reviewer \
stopped the change, the change touched protected files or held a credential): do not try \
again. Skip the item with skip_item when the item is not workable as it is written; \
otherwise leave it and say what would have to change.
   - A cause in the project (its tests fail before any change, its repository cannot be \
checked out, its tools are missing): do not try again; say what the administrator has to \
fix. When a queue will only repeat the failure, pause it with pause_queue; when several \
runs of the project failed for it, pause the project with pause_project.
   - A delivery whose rounds of correction ran out while each round came closer: grant \
it more rounds with extend_correction.
   - A run that waits and will never get what it waits for: stop it with cancel_run.
   - When you are not sure, say so in the confidence you give; the tool then asks the \
administrator instead of acting.
4. Close the incident with conclude_incident: your diagnosis in one or two sentences, your \
confidence, and what you did or why you did nothing. Do this for every incident, also \
when a tool answered that an action waits for the administrator.
Then reply with a short report for the administrator: one line per incident, what \
happened, what you did, and what, if anything, they have to do.\
"""


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _words(value: Any, limit: int = 600) -> str:
    return " ".join(str(value or "").split())[:limit]


def _row(incident: SupervisorIncident, project: ProjectRecord | None) -> dict[str, Any]:
    return {
        "id": str(incident.id),
        "kind": incident.kind,
        "state": incident.state,
        "summary": incident.summary,
        "facts": dict(incident.facts or {}),
        "project": project.name if project else None,
        "project_name": project.display_name if project else None,
        "run_id": str(incident.run_id) if incident.run_id else None,
        "item_id": str(incident.item_id) if incident.item_id else None,
        "turn_run_id": str(incident.turn_run_id) if incident.turn_run_id else None,
        "diagnosis": incident.diagnosis,
        "confidence": incident.confidence,
        "at": _when(incident.created_at),
        "closed_at": _when(incident.closed_at),
    }


class Operations:
    def __init__(self, sessions: sessionmaker[Session], conversation: Conversation) -> None:
        self.sessions = sessions
        self.conversation = conversation

    def settings(self) -> dict[str, Any]:
        return dict(self.conversation.definitions.current()["operations"])

    # --- noticing ------------------------------------------------------------------------

    def watch(self, now: datetime | None = None) -> int:
        """Open an incident for each trouble not yet known. Returns how many were opened."""
        now = now or datetime.now(UTC)
        with self.sessions.begin() as session:
            known = {
                (kind, ref)
                for kind, ref in session.execute(
                    select(SupervisorIncident.kind, SupervisorIncident.ref).where(
                        SupervisorIncident.created_at > now - timedelta(days=2)
                    )
                )
            }
            found = [
                *self._failed(session, now),
                *self._stalled(session, now),
                *self._paused(session),
            ]
            opened = 0
            for incident in found:
                if (incident.kind, incident.ref) in known:
                    continue
                known.add((incident.kind, incident.ref))
                session.add(incident)
                opened += 1
            return opened

    @staticmethod
    def _failed(session: Session, now: datetime) -> list[SupervisorIncident]:
        runs = session.scalars(
            select(SandboxRun)
            .where(
                SandboxRun.kind.in_(WORK_KINDS),
                SandboxRun.state == "failed",
                SandboxRun.finished_at > now - timedelta(minutes=NEWS_MINUTES),
            )
            .order_by(SandboxRun.finished_at)
            .limit(50)
        ).all()
        found = []
        for run in runs:
            step = session.scalar(
                select(SandboxStep)
                .where(SandboxStep.run_id == run.id, SandboxStep.state == "failed")
                .order_by(SandboxStep.position.desc())
                .limit(1)
            )
            where = f" at the step {step.name}" if step is not None else ""
            found.append(
                SupervisorIncident(
                    kind="run_failed",
                    ref=str(run.id),
                    state="open",
                    summary=_words(
                        f"The run “{run.title}” failed{where}: "
                        f"{run.detail or run.outcome or 'no reason was given'}"
                    ),
                    facts={
                        "run": run.title,
                        "run_kind": run.kind,
                        "outcome": run.outcome,
                        "step": step.name if step is not None else None,
                    },
                    project_id=run.project_id,
                    run_id=run.id,
                    item_id=run.item_id,
                )
            )
        return found

    @staticmethod
    def _stalled(session: Session, now: datetime) -> list[SupervisorIncident]:
        rows = session.execute(
            select(SandboxRun, SandboxStep)
            .join(SandboxStep, SandboxStep.run_id == SandboxRun.id)
            .where(
                SandboxRun.kind.in_(WORK_KINDS),
                SandboxRun.state.in_(("queued", "running")),
                SandboxRun.cancel_requested.is_(False),
                SandboxStep.state == "pending",
                SandboxStep.created_at < now - timedelta(minutes=STALL_MINUTES),
            )
            .order_by(SandboxStep.created_at)
            .limit(50)
        ).all()
        found = []
        for run, step in rows:
            minutes = int((now - _aware(step.created_at)).total_seconds() // 60)
            found.append(
                SupervisorIncident(
                    kind="run_stalled",
                    ref=f"{run.id}:{step.position}",
                    state="open",
                    summary=_words(
                        f"The run “{run.title}” has waited {minutes} minutes to start its "
                        f"step {step.name}: {step.detail or 'no reason is known'}"
                    ),
                    facts={
                        "run": run.title,
                        "run_kind": run.kind,
                        "step": step.name,
                        "waiting_for": step.detail,
                    },
                    project_id=run.project_id,
                    run_id=run.id,
                    item_id=run.item_id,
                )
            )
        return found

    @staticmethod
    def _paused(session: Session) -> list[SupervisorIncident]:
        """Queues the platform paused because their items kept failing."""
        queues = session.scalars(
            select(WorkQueue)
            .where(
                WorkQueue.state == "paused",
                WorkQueue.pause_reason.is_not(None),
                WorkQueue.pause_reason.not_like("Paused by the supervisor%"),
            )
            .limit(50)
        ).all()
        found = []
        for queue in queues:
            failed = session.scalar(
                select(func.count())
                .select_from(WorkQueueEntry)
                .where(WorkQueueEntry.queue_id == queue.id, WorkQueueEntry.state == "failed")
            )
            if not failed:
                # Paused by the administrator, with their own reason: not trouble.
                continue
            found.append(
                SupervisorIncident(
                    kind="queue_paused",
                    # A queue that is resumed and pauses again is new trouble.
                    ref=f"{queue.id}:{failed}",
                    state="open",
                    summary=_words(f"The queue “{queue.name}” paused itself: {queue.pause_reason}"),
                    facts={"queue": queue.name, "queue_id": str(queue.id), "failed": failed},
                    project_id=queue.project_id,
                )
            )
        return found

    # --- asking the supervisor -----------------------------------------------------------

    def dispatch(self, now: datetime | None = None) -> UUID | None:
        """Ask the supervisor about the open incidents, in a turn of its own. Not while a
        turn is under way or the administrator waits for an answer, and not more often
        within an hour than the administrator allows. Returns the turn, when one started."""
        now = now or datetime.now(UTC)
        settings = self.settings()
        if not settings["enabled"]:
            return None
        with self.sessions.begin() as session:
            incidents = session.scalars(
                select(SupervisorIncident)
                .where(SupervisorIncident.state == "open")
                .order_by(SupervisorIncident.created_at)
                .limit(INCIDENTS_PER_TURN)
                .with_for_update()
            ).all()
            if not incidents:
                return None
            asked = session.scalar(
                select(func.count())
                .select_from(SandboxRun)
                .where(
                    SandboxRun.kind == "supervisor_turn",
                    SandboxRun.title.like(f"{TITLE}%"),
                    SandboxRun.created_at > now - timedelta(hours=1),
                )
            )
            if (asked or 0) >= settings["per_hour"]:
                return None
            run = self.conversation.open_turn(
                session,
                title=f"{TITLE}{incidents[0].summary}",
                data={"channel": "operations", "incidents": [str(i.id) for i in incidents]},
            )
            if run is None:
                return None
            for incident in incidents:
                incident.state = "looking"
                incident.turn_run_id = run.id
                incident.facts = {
                    **(incident.facts or {}),
                    "looks": int((incident.facts or {}).get("looks") or 0) + 1,
                }
            listed = "\n".join(f"- {incident.summary}" for incident in incidents)
            session.add(
                SupervisorMessage(
                    role="platform",
                    channel="platform",
                    text=f"The platform asked the supervisor to look at:\n{listed}"[:2000],
                    run_id=run.id,
                )
            )
            return run.id

    def asked(self, run_id: UUID) -> str | None:
        """What a turn of the operations role is to look at, as the text it is given. None
        for a turn of the conversation."""
        with self.sessions() as session:
            run = session.get(SandboxRun, run_id)
            wanted = (run.data or {}).get("incidents") if run is not None else None
            if not wanted:
                return None
            incidents = session.scalars(
                select(SupervisorIncident)
                .where(SupervisorIncident.id.in_([UUID(str(value)) for value in wanted]))
                .order_by(SupervisorIncident.created_at)
            ).all()
            lines = []
            for incident in incidents:
                project = (
                    session.get(ProjectRecord, incident.project_id) if incident.project_id else None
                )
                ids = [f"incident {incident.id}"]
                if incident.run_id:
                    ids.append(f"run {incident.run_id}")
                if incident.item_id:
                    ids.append(f"item {incident.item_id}")
                if (incident.facts or {}).get("queue_id"):
                    ids.append(f"queue {incident.facts['queue_id']}")
                lines.append(
                    f"- {incident.summary}\n  Project: {project.name if project else 'none'}. "
                    f"Ids: {', '.join(ids)}."
                )
            return INSTRUCTIONS + "\n\n" + "\n".join(lines)

    def conclude(
        self, incident_id: UUID, *, diagnosis: str, confidence: str, run_id: UUID | None
    ) -> dict[str, Any]:
        """The supervisor's word on an incident. It is handled, unless something the
        supervisor wanted to do about it waits for the administrator."""
        with self.sessions.begin() as session:
            incident = session.get(SupervisorIncident, incident_id, with_for_update=True)
            if incident is None:
                raise ProjectNotFound(str(incident_id))
            if incident.state not in OPEN:
                raise ProjectConflict("The incident is closed already")
            incident.diagnosis = diagnosis.strip()[:6_000] or None
            incident.confidence = confidence if confidence in ("high", "medium", "low") else None
            if run_id is not None:
                incident.turn_run_id = run_id
            self._close(session, incident)
            project = (
                session.get(ProjectRecord, incident.project_id) if incident.project_id else None
            )
            return _row(incident, project)

    @staticmethod
    def _close(session: Session, incident: SupervisorIncident) -> None:
        pending = (
            session.scalar(
                select(func.count())
                .select_from(SupervisorAction)
                .where(
                    SupervisorAction.run_id == incident.turn_run_id,
                    SupervisorAction.outcome == "pending",
                )
            )
            if incident.turn_run_id is not None
            else 0
        )
        if pending:
            incident.state = "waiting"
            incident.facts = {**(incident.facts or {}), "needs": "decision"}
        else:
            incident.state = "handled"
            incident.closed_at = datetime.now(UTC)

    def settle(self) -> None:
        """Called by the launcher. Incidents whose turn has ended are closed by what the
        supervisor said, or go back to be looked at again, a limited number of times; and
        those that waited for a decision are closed once it is taken."""
        with self.sessions.begin() as session:
            for incident in session.scalars(
                select(SupervisorIncident)
                .where(SupervisorIncident.state.in_(("looking", "waiting")))
                .with_for_update()
            ).all():
                facts = dict(incident.facts or {})
                if incident.state == "waiting":
                    if facts.get("needs") == "decision":
                        self._close(session, incident)
                    continue
                turn = (
                    session.get(SandboxRun, incident.turn_run_id) if incident.turn_run_id else None
                )
                if turn is not None and turn.state in ("queued", "running"):
                    continue
                if turn is not None and turn.state == "succeeded":
                    # The supervisor answered without closing the incident: its reply
                    # is what it had to say.
                    reply = session.scalar(
                        select(SupervisorMessage.text)
                        .where(
                            SupervisorMessage.run_id == turn.id,
                            SupervisorMessage.role == "supervisor",
                        )
                        .limit(1)
                    )
                    incident.diagnosis = (reply or "The supervisor gave no diagnosis.")[:6_000]
                    self._close(session, incident)
                elif int(facts.get("looks") or 0) >= MAX_LOOKS:
                    incident.state = "waiting"
                    incident.facts = {**facts, "needs": "administrator"}
                    incident.diagnosis = (
                        "The supervisor could not look at this: its turn ended without an answer."
                    )
                else:
                    incident.state = "open"

    # --- the console ---------------------------------------------------------------------

    def listing(self, *, everything: bool = False, limit: int = 50) -> dict[str, Any]:
        """Incidents, newest first: the ones not closed, or all of them."""
        query = (
            select(SupervisorIncident, ProjectRecord)
            .outerjoin(ProjectRecord, SupervisorIncident.project_id == ProjectRecord.id)
            .order_by(SupervisorIncident.created_at.desc())
            .limit(max(1, min(limit, 200)))
        )
        if not everything:
            query = query.where(SupervisorIncident.state.in_(OPEN))
        with self.sessions() as session:
            rows = session.execute(query).all()
            return {
                "items": [_row(incident, project) for incident, project in rows],
                "total": len(rows),
                "settings": self.settings(),
            }

    def get(self, incident_id: UUID) -> dict[str, Any]:
        with self.sessions() as session:
            incident = session.get(SupervisorIncident, incident_id)
            if incident is None:
                raise ProjectNotFound(str(incident_id))
            project = (
                session.get(ProjectRecord, incident.project_id) if incident.project_id else None
            )
            return _row(incident, project)

    def inbox(self) -> list[dict[str, Any]]:
        """Incidents that are the administrator's: the supervisor could not look at them,
        or it does not look at trouble by itself. One that waits for a decision is in the
        inbox already, as the action that waits."""
        enabled = self.settings()["enabled"]
        with self.sessions() as session:
            rows = session.execute(
                select(SupervisorIncident, ProjectRecord)
                .outerjoin(ProjectRecord, SupervisorIncident.project_id == ProjectRecord.id)
                .where(SupervisorIncident.state.in_(("open", "waiting")))
                .order_by(SupervisorIncident.created_at)
                .limit(100)
            ).all()
            entries = []
            for incident, project in rows:
                needs = (incident.facts or {}).get("needs")
                if incident.state == "waiting" and needs != "administrator":
                    continue
                if incident.state == "open" and enabled:
                    continue
                entries.append(
                    {
                        "kind": "incident",
                        "project": project.name if project else None,
                        "project_name": project.display_name if project else None,
                        "section": None,
                        "count": 1,
                        "detail": incident.summary,
                        "incident_id": str(incident.id),
                        "run_id": str(incident.run_id) if incident.run_id else None,
                        "queue_id": (incident.facts or {}).get("queue_id"),
                        "diagnosis": incident.diagnosis,
                    }
                )
            return entries

    def dismiss(self, incident_id: UUID) -> dict[str, Any]:
        """The administrator has seen it: nothing more is to be done about it."""
        with self.sessions.begin() as session:
            incident = session.get(SupervisorIncident, incident_id, with_for_update=True)
            if incident is None:
                raise ProjectNotFound(str(incident_id))
            if incident.state not in OPEN:
                raise ProjectConflict("The incident is closed already")
            incident.state = "dismissed"
            incident.closed_at = datetime.now(UTC)
            project = (
                session.get(ProjectRecord, incident.project_id) if incident.project_id else None
            )
            return _row(incident, project)
