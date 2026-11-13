"""The one conversation between the administrator and the supervisor.

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

from engineering_control.dashboard.project_records import ProjectInvalid
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

    # --- the console and the chat channel ------------------------------------------------

    def listing(self, *, limit: int = 80, after: datetime | None = None) -> dict[str, Any]:
        """The latest messages, oldest first, and whether the supervisor is answering."""
        limit = max(1, min(limit, 200))
        with self.sessions() as session:
            query = select(SupervisorMessage).order_by(SupervisorMessage.created_at.desc())
            if after is not None:
                query = query.where(SupervisorMessage.created_at > after)
            found = list(reversed(session.scalars(query.limit(limit)).all()))
            turn = session.scalar(
                select(SandboxRun)
                .where(SandboxRun.kind == KIND, SandboxRun.state.in_(ACTIVE))
                .order_by(SandboxRun.created_at)
                .limit(1)
            )
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
                    "state": turn.state,
                    "detail": step.detail if step is not None and step.state == "pending" else None,
                    "provider": step.provider
                    if step is not None and step.state == "running"
                    else None,
                    "since": _when(turn.created_at),
                }
            return {"items": [_row(message) for message in found], "turn": waiting}

    def post(self, text: str, *, channel: str) -> dict[str, Any]:
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
            message = SupervisorMessage(role="administrator", channel=channel, text=clean)
            session.add(message)
            session.flush()
            self._start_turn(session)
            session.flush()
            return _row(message)

    def note(self, text: str) -> None:
        """Something the platform adds to the conversation, for both to read."""
        with self.sessions.begin() as session:
            session.add(SupervisorMessage(role="platform", channel="platform", text=text[:2000]))

    def since(self, moment: datetime, *, roles: tuple[str, ...]) -> list[dict[str, Any]]:
        """Messages of those roles written after ``moment``, oldest first."""
        with self.sessions() as session:
            found = session.scalars(
                select(SupervisorMessage)
                .where(SupervisorMessage.created_at > moment, SupervisorMessage.role.in_(roles))
                .order_by(SupervisorMessage.created_at)
                .limit(50)
            ).all()
            return [_row(message) for message in found]

    # --- turns ---------------------------------------------------------------------------

    def _start_turn(self, session: Session) -> SandboxRun | None:
        """Start a turn for the administrator's messages no turn has taken yet, unless one
        is under way. The lock on the unanswered messages keeps two callers from both
        starting one."""
        unanswered = session.scalars(
            select(SupervisorMessage)
            .where(SupervisorMessage.role == "administrator", SupervisorMessage.run_id.is_(None))
            .order_by(SupervisorMessage.created_at)
            .with_for_update()
        ).all()
        if not unanswered:
            return None
        busy = session.scalar(
            select(func.count())
            .select_from(SandboxRun)
            .where(SandboxRun.kind == KIND, SandboxRun.state.in_(ACTIVE))
        )
        if busy:
            return None
        first = " ".join(unanswered[-1].text.split())
        run = self.open_turn(
            session, title=f"Supervisor: {first}", data={"channel": unanswered[-1].channel}
        )
        if run is None:
            return None
        for message in unanswered:
            message.run_id = run.id
        return run

    def open_turn(self, session: Session, *, title: str, data: dict[str, Any]) -> SandboxRun | None:
        """A turn of the supervisor, as a run with its one step, unless one is under way
        or a message of the administrator still waits for its own."""
        busy = session.scalar(
            select(func.count())
            .select_from(SandboxRun)
            .where(SandboxRun.kind == KIND, SandboxRun.state.in_(ACTIVE))
        )
        waiting = session.scalar(
            select(func.count())
            .select_from(SupervisorMessage)
            .where(SupervisorMessage.role == "administrator", SupervisorMessage.run_id.is_(None))
        )
        if busy or (waiting and data.get("channel") not in CHANNELS):
            return None
        definition = self.definitions.current()
        run = SandboxRun(
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

            return [shown(message) for message in reversed(before)], [
                _row(message) for message in asked
            ]

    def recent(self, run_id: UUID, *, limit: int = 12) -> list[dict[str, Any]]:
        """The latest of what was said, for a turn that answers no message: so that the
        supervisor knows what the administrator and it were about."""
        with self.sessions() as session:
            found = session.scalars(
                select(SupervisorMessage)
                .where(
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
                            text=f"The supervisor could not answer. {why}",
                            run_id=run.id,
                        )
                    )
                # Keep what happened in the detail; the outcome only marks it as said.
                run.detail = f"{run.outcome or 'failed'}: {run.detail or ''}"[:600]
                run.outcome = "said"
            started = self._start_turn(session)
            return 1 if started is not None else 0
