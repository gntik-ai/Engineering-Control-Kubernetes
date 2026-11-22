"""Persistent project conversations and the shared console/Telegram conversation.

The console's panel and the chat channel are two ways into the same conversation. A message
of the administrator is answered by a turn: a run of kind ``supervisor_turn`` with one step,
in which the supervisor's client reads the conversation, calls its tools and replies. One
turn runs at a time; messages that arrive meanwhile are answered together by the next.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    active_project,
)
from engineering_control.db.models import SandboxRun, SandboxStep, SupervisorMessage
from engineering_control.supervisor.definition import Definitions

KIND: Final = "supervisor_turn"
ACTIVE: Final = ("queued", "running")
MAX_MESSAGE: Final = 8_000
HISTORY_MESSAGES: Final = 40
HISTORY_CHARACTERS: Final = 4_000
CHANNELS: Final = ("console", "chat")


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _row(message: SupervisorMessage) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "role": message.role,
        "channel": message.channel,
        "text": message.text,
        "run_id": str(message.run_id) if message.run_id else None,
        "at": _when(message.created_at),
    }


class Conversation:
    def __init__(self, sessions: sessionmaker[Session], definitions: Definitions) -> None:
        self.sessions = sessions
        self.definitions = definitions

    @staticmethod
    def _lock(session: Session, thread: str) -> None:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(select(func.pg_advisory_xact_lock(36814, func.hashtext(thread))))

    @staticmethod
    def _active(thread: str) -> Any:
        key = SandboxRun.data["thread"].as_string()
        return select(SandboxRun).where(
            SandboxRun.kind == KIND,
            SandboxRun.state.in_(ACTIVE),
            key == thread if thread else key.is_(None),
        )

    # --- the console and the chat channel ------------------------------------------------

    def listing(
        self, *, limit: int = 80, after: datetime | None = None, thread: str = ""
    ) -> dict[str, Any]:
        """The latest messages, oldest first, and whether the supervisor is answering."""
        limit = max(1, min(limit, 200))
        with self.sessions() as session:
            if thread:
                active_project(session, thread)
            query = (
                select(SupervisorMessage)
                .where(SupervisorMessage.thread == thread)
                .order_by(SupervisorMessage.created_at.desc(), SupervisorMessage.id.desc())
            )
            if after is not None:
                query = query.where(SupervisorMessage.created_at > after)
            found = list(reversed(session.scalars(query.limit(limit)).all()))
            turn = session.scalar(self._active(thread).order_by(SandboxRun.created_at).limit(1))
            waiting = None
            if turn is not None:
                step = session.scalar(
                    select(SandboxStep)
                    .where(SandboxStep.run_id == turn.id)
                    .order_by(SandboxStep.position.desc())
                    .limit(1)
                )
                waiting = {
                    "run_id": str(turn.id),
                    "scope": (turn.data or {}).get("scope"),
                    "definition_version": (turn.data or {}).get("definition_version"),
                    "state": turn.state,
                    "detail": step.detail if step is not None and step.state == "pending" else None,
                    "provider": step.provider
                    if step is not None and step.state == "running"
                    else None,
                    "since": _when(turn.created_at),
                }
            return {"items": [_row(message) for message in found], "turn": waiting}

    def post(
        self,
        text: str,
        *,
        channel: str,
        thread: str = "",
        projects: list[str] | None = None,
        all_reviews: bool = False,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        """A message of the administrator. A turn answers it, at once when none is under
        way, otherwise as soon as the one under way has ended."""
        clean = text.strip()
        if not clean:
            raise ProjectInvalid("Write something first")
        if len(clean) > MAX_MESSAGE:
            raise ProjectInvalid(f"A message has at most {MAX_MESSAGE} characters")
        if channel not in CHANNELS:
            raise ProjectInvalid("That is not a way into the conversation")
        with self.sessions.begin() as session:
            self._lock(session, thread)
            scope = None
            if thread:
                names = sorted({thread, *(projects or [])})
                for name in names:
                    active_project(session, name)
                scope = {"projects": names, "all_reviews": all_reviews}
            if request_id:
                old = session.scalar(
                    select(SupervisorMessage).where(
                        SupervisorMessage.thread == thread,
                        SupervisorMessage.request_id == request_id,
                    )
                )
                if old:
                    if old.text != clean or old.scope != scope:
                        raise ProjectConflict("This message identifier has already been used.")
                    return _row(old)
            message = SupervisorMessage(
                role="administrator",
                channel=channel,
                text=clean,
                thread=thread,
                scope=scope,
                request_id=request_id,
            )
            session.add(message)
            session.flush()
            self._start_turn(session, thread)
            session.flush()
            return _row(message)

    def note(self, text: str, *, thread: str = "", scope: dict[str, Any] | None = None) -> None:
        """Something the platform adds to the conversation, for both to read."""
        with self.sessions.begin() as session:
            session.add(
                SupervisorMessage(
                    role="platform",
                    channel="platform",
                    text=text[:2000],
                    thread=thread,
                    scope=scope or {},
                )
            )

    def since(self, moment: datetime, *, roles: tuple[str, ...]) -> list[dict[str, Any]]:
        """Messages of those roles written after ``moment``, oldest first."""
        with self.sessions() as session:
            found = session.scalars(
                select(SupervisorMessage)
                .where(
                    SupervisorMessage.thread == "",
                    SupervisorMessage.created_at > moment,
                    SupervisorMessage.role.in_(roles),
                )
                .order_by(SupervisorMessage.created_at)
                .limit(50)
            ).all()
            return [_row(message) for message in found]

    # --- turns ---------------------------------------------------------------------------

    def _start_turn(self, session: Session, thread: str = "") -> SandboxRun | None:
        """Start a turn for the administrator's messages no turn has taken yet, unless one
        is under way. The lock on the unanswered messages keeps two callers from both
        starting one."""
        self._lock(session, thread)
        unanswered = session.scalars(
            select(SupervisorMessage)
            .where(
                SupervisorMessage.thread == thread,
                SupervisorMessage.role == "administrator",
                SupervisorMessage.run_id.is_(None),
            )
            .order_by(SupervisorMessage.created_at)
            .with_for_update()
        ).all()
        if not unanswered:
            return None
        # Separate turns preserve the exact scope the administrator selected on each send.
        batch = []
        for message in unanswered:
            if message.scope != unanswered[0].scope:
                break
            batch.append(message)
        first = " ".join(batch[-1].text.split())
        data: dict[str, Any] = {"channel": batch[-1].channel}
        if thread:
            data.update(thread=thread, scope=batch[0].scope)
        run = self.open_turn(session, title=f"Supervisor: {first}", data=data)
        if run is None:
            return None
        for message in batch:
            message.run_id = run.id
        return run

    def open_turn(self, session: Session, *, title: str, data: dict[str, Any]) -> SandboxRun | None:
        """A turn of the supervisor, as a run with its one step, unless one is under way
        or a message of the administrator still waits for its own."""
        thread = str(data.get("thread") or "")
        self._lock(session, thread)
        busy = session.scalar(self._active(thread).limit(1))
        waiting = session.scalar(
            select(func.count())
            .select_from(SupervisorMessage)
            .where(
                SupervisorMessage.thread == thread,
                SupervisorMessage.role == "administrator",
                SupervisorMessage.run_id.is_(None),
            )
        )
        if busy or (waiting and data.get("channel") not in CHANNELS):
            return None
        definition = self.definitions.current()
        run = SandboxRun(
            project_id=active_project(session, thread).id if thread else None,
            kind=KIND,
            state="queued",
            title=title[:300],
            cancel_requested=False,
            data={"definition_version": definition["version"], **data},
        )
        session.add(run)
        session.flush()
        given: dict[str, Any] = {}
        if definition["account_id"]:
            given["prefer_account"] = definition["account_id"]
        session.add(
            SandboxStep(
                run_id=run.id,
                position=1,
                name="answer",
                state="pending",
                provider=",".join(definition["providers"]),
                input=given or None,
                attempt=0,
            )
        )
        return run

    def history(self, run_id: UUID) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """For the turn's prompt: what was said before, and the messages it is to answer."""
        with self.sessions() as session:
            asked = session.scalars(
                select(SupervisorMessage)
                .where(
                    SupervisorMessage.run_id == run_id, SupervisorMessage.role == "administrator"
                )
                .order_by(SupervisorMessage.created_at)
            ).all()
            if not asked:
                return [], []
            # Everything said before, in the order it was said. A message that waited
            # for this turn was written before the reply to the one ahead of it, so "before"
            # is not a matter of time: it is every message but the ones to answer now and
            # the ones that have no turn yet.
            before = session.scalars(
                select(SupervisorMessage)
                .where(
                    SupervisorMessage.thread == asked[0].thread,
                    SupervisorMessage.id.not_in([message.id for message in asked]),
                    or_(
                        SupervisorMessage.role != "administrator",
                        SupervisorMessage.run_id.is_not(None),
                    ),
                )
                .order_by(SupervisorMessage.created_at.desc())
                .limit(HISTORY_MESSAGES)
            ).all()

            def shown(message: SupervisorMessage) -> dict[str, Any]:
                row = _row(message)
                if len(row["text"]) > HISTORY_CHARACTERS:
                    row["text"] = row["text"][:HISTORY_CHARACTERS] + " […]"
                return row

            scope = asked[0].scope or {}
            targets = set(scope.get("projects", ()))

            def visible(message: SupervisorMessage) -> bool:
                previous = message.scope or {}
                return set(previous.get("projects", ())).issubset(targets) and (
                    not previous.get("all_reviews") or scope.get("all_reviews", False)
                )

            return [shown(message) for message in reversed(before) if visible(message)], [
                _row(message) for message in asked
            ]

    def recent(self, run_id: UUID, *, limit: int = 12) -> list[dict[str, Any]]:
        """The latest of what was said, for a turn that answers no message: so that the
        supervisor knows what the administrator and it were about."""
        with self.sessions() as session:
            found = session.scalars(
                select(SupervisorMessage)
                .where(
                    SupervisorMessage.thread == "",
                    or_(SupervisorMessage.run_id.is_(None), SupervisorMessage.run_id != run_id),
                    or_(
                        SupervisorMessage.role != "administrator",
                        SupervisorMessage.run_id.is_not(None),
                    ),
                )
                .order_by(SupervisorMessage.created_at.desc())
                .limit(limit)
            ).all()
            rows = [_row(message) for message in reversed(found)]
            for row in rows:
                if len(row["text"]) > HISTORY_CHARACTERS // 2:
                    row["text"] = row["text"][: HISTORY_CHARACTERS // 2] + " […]"
            return rows

    def answered(self, run_id: UUID, text: str) -> None:
        """The supervisor's reply of a turn."""
        with self.sessions.begin() as session:
            run = session.get(SandboxRun, run_id)
            channel = (run.data or {}).get("channel") if run is not None else None
            session.add(
                SupervisorMessage(
                    thread=(run.data or {}).get("thread", "") if run else "",
                    scope=(run.data or {}).get("scope") or {} if run else {},
                    role="supervisor",
                    channel=channel if channel in CHANNELS else "console",
                    text=text.strip()[:60_000] or "(The supervisor gave no answer.)",
                    run_id=run_id,
                )
            )

    def settle(self) -> int:
        """Called by the launcher on every pass. A turn that ended without a reply is said
        so in the conversation, once; and messages that arrived while a turn was under way
        get theirs. Returns how many turns it started."""
        with self.sessions.begin() as session:
            ended = session.scalars(
                select(SandboxRun)
                .where(
                    SandboxRun.kind == KIND,
                    SandboxRun.state.in_(("failed", "cancelled")),
                    SandboxRun.outcome != "said",
                )
                .order_by(SandboxRun.created_at)
                .limit(20)
                .with_for_update()
            ).all()
            for run in ended:
                replied = session.scalar(
                    select(func.count())
                    .select_from(SupervisorMessage)
                    .where(
                        SupervisorMessage.run_id == run.id,
                        SupervisorMessage.role != "administrator",
                    )
                )
                if not replied:
                    why = (
                        "The turn was stopped."
                        if run.state == "cancelled"
                        else (run.detail or "The turn ended without an answer.")
                    )
                    session.add(
                        SupervisorMessage(
                            role="platform",
                            channel="platform",
                            thread=(run.data or {}).get("thread", ""),
                            scope=(run.data or {}).get("scope") or {},
                            text=f"The supervisor could not answer. {why}",
                            run_id=run.id,
                        )
                    )
                # Keep what happened in the detail; the outcome only marks it as said.
                run.detail = f"{run.outcome or 'failed'}: {run.detail or ''}"[:600]
                run.outcome = "said"
            threads = session.scalars(
                select(SupervisorMessage.thread)
                .where(
                    SupervisorMessage.role == "administrator", SupervisorMessage.run_id.is_(None)
                )
                .distinct()
                .order_by(SupervisorMessage.thread)
            ).all()
            return sum(self._start_turn(session, thread) is not None for thread in threads)
