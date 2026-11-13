"""The chat channel: one conversation with the administrator in a messaging app.

It carries three kinds of message. What the administrator writes: a fixed command
(``/status``, ``/inbox``, ``/runs``, ``/queues``, ``/digest``, ``/help``) is answered by the
platform itself; anything else is a message to the supervisor, in the same conversation as
the console's panel. What the platform tells them: the supervisor's replies to what they
wrote here, things that wait for them, runs that failed, queues that stopped. And once a
day, a digest.

Only the administrator is answered: one user id, in a private chat. Anyone else is ignored
without a reply. What the supervisor proposed can be accepted or rejected with a button;
publishing a change needs the password and is done in the console, to which the message
links.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
)
from engineering_control.db.models import (
    ChatDelivery,
    ChatState,
    ProviderAccount,
    SandboxRun,
    SupervisorAction,
    SupervisorMessage,
    WorkQueue,
)
from engineering_control.flow_library import DELIVER, WORK_KINDS
from engineering_control.sandbox.queues import Queues
from engineering_control.sandbox.runs import CONVERSATION, Runs
from engineering_control.supervisor.conversation import Conversation
from engineering_control.supervisor.tools import Toolbox

LOG = structlog.get_logger(__name__)
HELP: Final = (
    "Write to me as you would in the console's panel: I pass it to the supervisor and bring "
    "back its answer.\n\n"
    "These I answer myself, at once:\n"
    "/status — what runs and what waits for you\n"
    "/inbox — everything that waits for you\n"
    "/runs — the latest runs\n"
    "/queues — the queues and how far they are\n"
    "/digest — the last day in short\n"
    "/help — this text"
)
STALE: Final = timedelta(minutes=2)
OUTCOMES: Final = {
    "applied": "done at once",
    "accepted": "accepted by you",
    "rejected": "rejected by you",
    "refused": "refused by the platform",
    "failed": "failed",
    "pending": "waiting for you",
}


class Transport(Protocol):
    async def send(
        self, chat_id: int, text: str, *, buttons: list[tuple[str, str]] | None = None
    ) -> None: ...

    async def answer_button(self, callback_id: str, text: str) -> None: ...

    async def clear_buttons(self, chat_id: int, message_id: int) -> None: ...


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


class ChatChannel:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        transport: Transport,
        *,
        administrator_id: int,
        conversation: Conversation,
        toolbox: Toolbox,
        runs: Runs,
        queues: Queues,
        console_url: str | None = None,
        digest_hour: int = 7,
    ) -> None:
        self.sessions = sessions
        self.transport = transport
        self.administrator_id = administrator_id
        self.conversation = conversation
        self.toolbox = toolbox
        self.runs = runs
        self.queues = queues
        self.console_url = console_url.rstrip("/") if console_url else None
        self.digest_hour = digest_hour

    # --- what it remembers ---------------------------------------------------------------

    def remembered(self, key: str) -> str | None:
        with self.sessions() as session:
            state = session.get(ChatState, key)
            return state.value if state is not None else None

    def remember(self, key: str, value: str) -> None:
        with self.sessions.begin() as session:
            state = session.get(ChatState, key)
            if state is None:
                session.add(ChatState(key=key, value=value))
            else:
                state.value = value

    def since(self) -> datetime:
        """From when on things are reported. Set the first time the channel runs, so that
        nothing older is ever sent."""
        stored = self.remembered("since")
        if stored is None:
            stored = datetime.now(UTC).isoformat()
            self.remember("since", stored)
        return datetime.fromisoformat(stored)

    def _told(self, kind: str, ref: str) -> bool:
        """Record that something is being told. False when it was told before."""
        try:
            with self.sessions.begin() as session:
                session.add(ChatDelivery(kind=kind, ref=ref[:120]))
        except IntegrityError:
            return False
        return True

    def _link(self, path: str) -> str:
        return f"\n{self.console_url}/console/{path}" if self.console_url else ""

    # --- what the administrator sends ----------------------------------------------------

    async def handle(self, update: dict[str, Any]) -> None:
        """One update of the transport. Anything not from the administrator, in a private
        chat with the bot, is dropped without an answer."""
        button = update.get("callback_query")
        if isinstance(button, dict):
            await self._button(button)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        sender = (message.get("from") or {}).get("id")
        chat = message.get("chat") or {}
        if sender != self.administrator_id or chat.get("type") != "private":
            LOG.warning("chat_ignored", reason="not the administrator")
            return
        sent = message.get("date")
        if isinstance(sent, int) and datetime.fromtimestamp(sent, UTC) < self.since() - STALE:
            # Written before the channel existed here: not answered now, out of the blue.
            LOG.info("chat_ignored", reason="older than the channel")
            return
        text = str(message.get("text") or "").strip()
        if not text:
            await self.transport.send(self.administrator_id, "I can only read text.")
            return
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
        if command in ("/start", "/help"):
            await self.transport.send(self.administrator_id, HELP)
        elif command == "/status":
            await self.transport.send(self.administrator_id, self.status())
        elif command == "/inbox":
            await self._inbox()
        elif command == "/runs":
            await self.transport.send(self.administrator_id, self.latest_runs())
        elif command == "/queues":
            await self.transport.send(self.administrator_id, self.queue_summary())
        elif command == "/digest":
            await self.transport.send(self.administrator_id, self.digest(datetime.now(UTC)))
        elif command is not None:
            await self.transport.send(
                self.administrator_id, f"I do not know {command}. /help lists what I know."
            )
        else:
            try:
                self.conversation.post(text, channel="chat")
            except ProjectInvalid as error:
                await self.transport.send(self.administrator_id, str(error))

    async def _button(self, button: dict[str, Any]) -> None:
        sender = (button.get("from") or {}).get("id")
        callback_id = str(button.get("id") or "")
        if sender != self.administrator_id:
            LOG.warning("chat_ignored", reason="not the administrator")
            return
        kind, _, rest = str(button.get("data") or "").partition(":")
        action_id, _, answer = rest.partition(":")
        if kind != "act" or answer not in ("yes", "no"):
            await self.transport.answer_button(callback_id, "That button is no longer good.")
            return
        try:
            decided = self.toolbox.decide(UUID(action_id), accept=answer == "yes", via="chat")
            said = "Accepted." if decided["outcome"] == "accepted" else "Rejected."
        except ProjectConflict as error:
            said = str(error)
        except (ProjectNotFound, ValueError):
            said = "That is no longer there."
        await self.transport.answer_button(callback_id, said)
        shown = button.get("message") or {}
        if isinstance(shown.get("message_id"), int):
            await self.transport.clear_buttons(self.administrator_id, shown["message_id"])
        await self.transport.send(self.administrator_id, said)

    # --- what the platform answers itself ------------------------------------------------

    def status(self) -> str:
        with self.sessions() as session:
            running = session.scalars(
                select(SandboxRun)
                .where(SandboxRun.kind.in_(WORK_KINDS), SandboxRun.state.in_(("queued", "running")))
                .order_by(SandboxRun.created_at)
                .limit(10)
            ).all()
            lines = [f"- {run.title}" for run in running]
        waiting = self.toolbox.inbox_entries()
        urgent = [entry for entry in waiting if entry["kind"] not in ("unrouted", "undecided")]
        parts = [
            f"**Running**: {_plural(len(lines), 'run', 'runs')} of work."
            if lines
            else "**Running**: nothing."
        ]
        parts += lines
        parts.append(
            f"**Waiting for you**: {_plural(len(urgent), 'thing', 'things')}. /inbox shows them."
            if urgent
            else "**Waiting for you**: nothing pressing."
        )
        active = [q for q in self.queues.listing()["items"] if q["state"] in ("running", "paused")]
        for queue in active[:5]:
            counts = queue["counts"]
            parts.append(
                f"Queue **{queue['name']}** ({queue['project']}): {queue['state']}, "
                f"{counts['done']} of {counts['total']} done."
            )
        return "\n".join(parts)

    def _entry(self, entry: dict[str, Any]) -> str:
        kind = entry["kind"]
        project = entry.get("project_name") or entry.get("project") or ""
        if kind == "run_waiting":
            return (
                f"A change is ready: **{entry.get('run')}**. Publish or discard it in the console."
            )
        if kind == "account_sign_in":
            return f"The account **{entry.get('account')}** needs a sign-in."
        if kind == "supervisor_action":
            return f"The supervisor proposes: **{entry.get('summary')}**"
        if kind == "queue_proposed":
            return f"The supervisor proposed the queue **{entry.get('queue')}** for {project}."
        if kind == "queue_paused":
            why = entry.get("detail") or ""
            return f"The queue **{entry.get('queue')}** of {project} is paused. {why}"
        if kind == "sync_failed":
            return f"The issues of {project} could not be read from GitHub."
        if kind == "section_without_flow":
            waits = _plural(entry["count"], "item waits", "items wait")
            return f"{waits} in a section of {project} that has no flow."
        if kind == "unrouted":
            return f"{_plural(entry['count'], 'item', 'items')} of {project} without a section."
        return f"{_plural(entry['count'], 'item', 'items')} of {project} to decide on."

    async def _inbox(self) -> None:
        entries = self.toolbox.inbox_entries()
        if not entries:
            await self.transport.send(self.administrator_id, "Nothing is waiting for you.")
            return
        plain = [entry for entry in entries if entry["kind"] != "supervisor_action"]
        if plain:
            await self.transport.send(
                self.administrator_id,
                "\n".join(f"- {self._entry(entry)}" for entry in plain[:25]) + self._link("inbox"),
            )
        for entry in [e for e in entries if e["kind"] == "supervisor_action"][:10]:
            await self._propose(entry)

    async def _propose(self, entry: dict[str, Any]) -> None:
        text = f"The supervisor proposes: **{entry.get('summary')}**"
        if entry.get("reason"):
            text += f"\nWhy: {entry['reason']}"
        if entry.get("detail"):
            text += f"\n{entry['detail']}"
        action = entry["action_id"]
        await self.transport.send(
            self.administrator_id,
            text,
            buttons=[("Accept", f"act:{action}:yes"), ("Reject", f"act:{action}:no")],
        )

    def latest_runs(self) -> str:
        runs = self.runs.listing(limit=8)["items"]
        if not runs:
            return "Nothing has run yet."
        return "\n".join(
            f"- **{run['title']}**: {run['state']}"
            + (f" ({run['outcome']})" if run["outcome"] and run["outcome"] != run["state"] else "")
            for run in runs
        ) + self._link("runs")

    def queue_summary(self) -> str:
        queues = [q for q in self.queues.listing()["items"] if q["state"] != "finished"]
        if not queues:
            return "There is no queue under way."
        lines = []
        for queue in queues[:10]:
            counts = queue["counts"]
            line = (
                f"- **{queue['name']}** ({queue['project']}): {queue['state']}, "
                f"{counts['done']} of {counts['total']} done"
            )
            if counts["failed"]:
                line += f", {counts['failed']} failed"
            if queue["pause_reason"]:
                line += f". {queue['pause_reason']}"
            lines.append(line)
        return "\n".join(lines) + self._link("queues")

    def digest(self, now: datetime) -> str:
        """The last day in short: what ran, what the supervisor did, what waits."""
        since = now - timedelta(hours=24)
        with self.sessions() as session:
            runs = session.execute(
                select(SandboxRun.state, func.count())
                .where(SandboxRun.kind.in_(WORK_KINDS), SandboxRun.created_at > since)
                .group_by(SandboxRun.state)
            ).all()
            actions = session.execute(
                select(SupervisorAction.outcome, func.count())
                .where(SupervisorAction.created_at > since, SupervisorAction.outcome != "read")
                .group_by(SupervisorAction.outcome)
            ).all()
            turns = session.scalar(
                select(func.count())
                .select_from(SandboxRun)
                .where(SandboxRun.kind == CONVERSATION, SandboxRun.created_at > since)
            )
            limited = session.scalars(
                select(ProviderAccount.name).where(
                    ProviderAccount.archived_at.is_(None),
                    ProviderAccount.state.in_(("limited", "sign_in_required", "signed_out")),
                )
            ).all()
        ran = {str(state): int(count) for state, count in runs}
        total = sum(ran.values())
        lines = ["**The last day**"]
        if total:
            detail = ", ".join(
                f"{count} {label}"
                for label, count in (
                    ("published or done", ran.get("succeeded", 0)),
                    ("waiting for your decision", ran.get("waiting", 0)),
                    ("failed", ran.get("failed", 0)),
                    ("stopped", ran.get("cancelled", 0)),
                    ("still running", ran.get("queued", 0) + ran.get("running", 0)),
                )
                if count
            )
            lines.append(f"Work: {_plural(total, 'run', 'runs')} started: {detail}.")
        else:
            lines.append("Work: no run was started.")
        did = {str(outcome): int(count) for outcome, count in actions}
        if did or turns:
            parts = [f"{count} {OUTCOMES.get(outcome, outcome)}" for outcome, count in did.items()]
            lines.append(
                f"Supervisor: {_plural(int(turns or 0), 'turn', 'turns')}"
                + (f"; actions: {', '.join(parts)}." if parts else "; no action.")
            )
        else:
            lines.append("Supervisor: not asked.")
        if limited:
            lines.append(f"Accounts that cannot work right now: {', '.join(limited)}.")
        waiting = [
            entry
            for entry in self.toolbox.inbox_entries()
            if entry["kind"] not in ("unrouted", "undecided")
        ]
        lines.append(
            f"Waiting for you: {_plural(len(waiting), 'thing', 'things')}. /inbox shows them."
            if waiting
            else "Nothing pressing waits for you."
        )
        return "\n".join(lines)

    # --- what the platform tells by itself -----------------------------------------------

    async def deliver(self, now: datetime | None = None) -> int:
        """Tell the administrator what is new since the last pass. Returns how many
        messages that was."""
        now = now or datetime.now(UTC)
        since = self.since()
        sent = 0
        for kind, ref, text, buttons in self._news(since):
            if not self._told(kind, ref):
                continue
            await self.transport.send(self.administrator_id, text, buttons=buttons)
            sent += 1
        # The digest: once on each day, at the first pass after its hour.
        if now.hour >= self.digest_hour and since < now - timedelta(hours=1):
            if self._told("digest", now.date().isoformat()):
                await self.transport.send(self.administrator_id, self.digest(now))
                sent += 1
        return sent

    def _news(self, since: datetime) -> list[tuple[str, str, str, list[tuple[str, str]] | None]]:
        news: list[tuple[str, str, str, list[tuple[str, str]] | None]] = []
        with self.sessions() as session:
            # The supervisor's replies to what was written here, and turns of this channel
            # that could not be answered.
            chat_turns = select(SandboxRun.id).where(
                SandboxRun.kind == CONVERSATION,
                SandboxRun.created_at > since - timedelta(hours=1),
            )
            for message in session.scalars(
                select(SupervisorMessage)
                .where(
                    SupervisorMessage.created_at > since,
                    SupervisorMessage.role.in_(("supervisor", "platform")),
                    SupervisorMessage.run_id.in_(chat_turns),
                )
                .order_by(SupervisorMessage.created_at)
                .limit(50)
            ):
                run = session.get(SandboxRun, message.run_id)
                channel = (run.data or {}).get("channel") if run is not None else None
                if channel == "operations" and message.role == "supervisor":
                    # What the supervisor did about trouble it was asked to look at.
                    text = f"The supervisor looked at trouble with the work:\n{message.text}"
                    news.append(("message", str(message.id), text, None))
                elif channel == "chat":
                    news.append(("message", str(message.id), message.text, None))
            for action in session.scalars(
                select(SupervisorAction)
                .where(SupervisorAction.outcome == "pending", SupervisorAction.created_at > since)
                .order_by(SupervisorAction.created_at)
                .limit(20)
            ):
                text = f"The supervisor proposes: **{action.summary}**"
                if action.reason:
                    text += f"\nWhy: {action.reason}"
                if action.detail:
                    text += f"\n{action.detail}"
                news.append(
                    (
                        "action",
                        str(action.id),
                        text,
                        [("Accept", f"act:{action.id}:yes"), ("Reject", f"act:{action.id}:no")],
                    )
                )
            for run in session.scalars(
                select(SandboxRun)
                .where(
                    SandboxRun.kind.in_(WORK_KINDS),
                    SandboxRun.state.in_(("waiting", "failed", "succeeded")),
                    SandboxRun.updated_at > since,
                )
                .order_by(SandboxRun.updated_at)
                .limit(30)
            ):
                if run.state == "succeeded":
                    # A delivery that ended well was published by the administrator, who
                    # knows. An audit or a search that ended has something to say.
                    if run.kind == DELIVER:
                        continue
                    text = f"**{run.title}** has ended. {run.detail or ''}"
                    if (run.outcome or "") in ("findings", "proposed"):
                        text += " They wait in the backlog as proposed items."
                elif run.state == "waiting":
                    text = (
                        f"A change is ready: **{run.title}**. {run.detail or ''} Publishing "
                        "needs your password, so decide in the console."
                    )
                else:
                    text = f"A run failed: **{run.title}**. {run.detail or run.outcome or ''}"
                news.append(
                    (
                        "run",
                        f"{run.id}:{run.state}:{run.outcome or ''}"[:120],
                        text + self._link(f"run?id={run.id}"),
                        None,
                    )
                )
            for queue in session.scalars(
                select(WorkQueue)
                .where(WorkQueue.updated_at > since)
                .order_by(WorkQueue.updated_at)
                .limit(30)
            ):
                link = self._link(f"queue?id={queue.id}")
                if queue.state == "finished" and queue.finished_at is not None:
                    news.append(
                        (
                            "queue",
                            f"{queue.id}:finished:{_aware(queue.finished_at).isoformat()}",
                            f"The queue **{queue.name}** has finished." + link,
                            None,
                        )
                    )
                elif queue.state == "paused" and queue.pause_reason:
                    news.append(
                        (
                            "queue",
                            f"{queue.id}:paused:{_aware(queue.updated_at).isoformat()}",
                            f"The queue **{queue.name}** is paused. {queue.pause_reason}" + link,
                            None,
                        )
                    )
                elif queue.state == "draft" and queue.proposed_by == "supervisor":
                    news.append(
                        (
                            "queue",
                            f"{queue.id}:proposed",
                            f"The supervisor proposed the queue **{queue.name}**. "
                            f"{queue.reason or ''} Nothing runs until you start it." + link,
                            None,
                        )
                    )
            for account in session.scalars(
                select(ProviderAccount).where(
                    ProviderAccount.archived_at.is_(None),
                    ProviderAccount.state == "sign_in_required",
                    ProviderAccount.checked_at > since,
                )
            ):
                news.append(
                    (
                        "account",
                        f"{account.id}:{account.credential_generation}",
                        f"The account **{account.name}** needs a new sign-in. "
                        f"{account.state_reason or ''}" + self._link("accounts"),
                        None,
                    )
                )
        return news
