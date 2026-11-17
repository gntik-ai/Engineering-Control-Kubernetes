"""The chat channel: only the administrator is answered, fixed commands are answered by the
platform, anything else goes to the supervisor, and news is told once."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.chat.channel import ChatChannel
from engineering_control.chat.service import token_from
from engineering_control.chat.telegram import Telegram, TelegramError, chunks, to_html
from engineering_control.config import Settings
from engineering_control.dashboard.accounts import Accounts
from engineering_control.dashboard.project_records import ProjectRecords
from engineering_control.dashboard.project_sections import SectionRecords
from engineering_control.db.base import Base
from engineering_control.db.models import (
    BacklogItem,
    ChatDelivery,
    ProjectRecord,
    ProjectSection,
    SandboxRun,
    SandboxStep,
    SupervisorMessage,
)
from engineering_control.sandbox.queues import Budgets, Queues
from engineering_control.sandbox.runs import Runs
from engineering_control.supervisor.service import toolbox_from

ADMINISTRATOR = 685699815
TOKEN = "1234567890:" + "A" * 35  # noqa: S105 - the shape of a bot token, not one


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, list[tuple[str, str]] | None]] = []
        self.answered: list[str] = []
        self.cleared: list[int] = []

    async def send(
        self, chat_id: int, text: str, *, buttons: list[tuple[str, str]] | None = None
    ) -> None:
        assert chat_id == ADMINISTRATOR
        self.sent.append((text, buttons))

    async def answer_button(self, callback_id: str, text: str) -> None:
        self.answered.append(text)

    async def clear_buttons(self, chat_id: int, message_id: int) -> None:
        self.cleared.append(message_id)

    def texts(self) -> list[str]:
        found = [text for text, _buttons in self.sent]
        self.sent.clear()
        return found


def _message(
    text: str, *, sender: int = ADMINISTRATOR, kind: str = "private", **more: Any
) -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 7,
            "from": {"id": sender},
            "chat": {"id": sender, "type": kind},
            "date": int(datetime.now(UTC).timestamp()),
            "text": text,
            **more,
        },
    }


@pytest.fixture
def world(tmp_path: Path) -> Any:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'chat.db'}")
    Base.metadata.create_all(engine)
    sessions: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)
    ProjectRecords(sessions).create(
        name="falcone-workflow",
        display_name="Falcone workflow",
        repository="gntik-ai/falcone-workflow",
        default_branch="main",
    )
    SectionRecords(sessions).sync_core()
    with sessions.begin() as session:
        project = session.query(ProjectRecord).filter_by(name="falcone-workflow").one()
        issues = session.query(ProjectSection).filter_by(project_id=project.id, key="issues").one()
        issues.flow_key = "deliver-issue"
        ids = []
        for number, title in ((11, "Expired cookie"), (12, "Export to CSV")):
            item = BacklogItem(
                project_id=project.id,
                section_id=issues.id,
                title=title,
                source="github",
                state="accepted",
                routed_by="label",
                repository="gntik-ai/falcone-workflow",
                issue_number=number,
                issue_labels=[],
            )
            session.add(item)
            session.flush()
            ids.append(item.id)
    runs = Runs(sessions)
    queues = Queues(sessions, runs, Budgets(sessions))
    toolbox = toolbox_from(sessions, None)
    transport = FakeTransport()

    class World:
        def __init__(self) -> None:
            self.sessions = sessions
            self.items = ids
            self.runs = runs
            self.queues = queues
            self.toolbox = toolbox
            self.transport = transport
            self.accounts = Accounts(sessions)
            self.channel = ChatChannel(
                sessions,
                transport,
                administrator_id=ADMINISTRATOR,
                conversation=toolbox.conversation,
                toolbox=toolbox,
                runs=runs,
                queues=queues,
                console_url="https://ade.musematic.ai/",
                digest_hour=7,
            )
            self.channel.since()

        def turn(self, channel: str = "chat") -> UUID:
            with sessions.begin() as session:
                run = SandboxRun(
                    kind="supervisor_turn",
                    state="running",
                    title="Turn",
                    cancel_requested=False,
                    data={"channel": channel},
                )
                session.add(run)
                session.flush()
                session.add(
                    SandboxStep(
                        run_id=run.id, position=1, name="answer", state="running", attempt=1
                    )
                )
                return run.id

    yield World()
    engine.dispose()


# --- the transport ----------------------------------------------------------------------


def test_replies_are_sent_as_plain_text_with_bold_and_code_and_nothing_else() -> None:
    assert to_html("Routed **#11 <b>x</b>** to `issues` & done") == (
        "Routed <b>#11 &lt;b&gt;x&lt;/b&gt;</b> to <code>issues</code> &amp; done"
    )
    assert to_html("## Heading\ntext") == "Heading\ntext"
    assert chunks("short") == ["short"] and chunks("  ") == []
    long = "\n\n".join(f"Paragraph {n} " + "word " * 120 for n in range(12))
    parts = chunks(long, 1000)
    assert len(parts) > 5 and all(len(part) <= 1000 for part in parts)
    assert "".join(parts).replace("\n", "").replace(" ", "") == long.replace("\n", "").replace(
        " ", ""
    )
    assert all(len(part) <= 50 for part in chunks("x" * 500, 50))


async def test_the_transport_never_puts_the_token_into_an_error_or_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[httpx.Request] = []
    # The HTTP client logs each request's address when asked for informational messages,
    # and Telegram's address holds the token: asked for them here, it must log none.
    logging.getLogger("httpx").setLevel(logging.INFO)
    caplog.set_level(logging.INFO)

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 5}, "junk"]})
        if request.url.path.endswith("/sendMessage"):
            body = json.loads(request.content)
            if "refuse" in body["text"]:
                return httpx.Response(
                    400, json={"ok": False, "description": "Bad Request: chat not found"}
                )
            return httpx.Response(200, json={"ok": True, "result": {}})
        raise httpx.ConnectError(f"could not reach {request.url}")

    telegram = Telegram(TOKEN, transport=httpx.MockTransport(answer))
    assert await telegram.updates(None) == [{"update_id": 5}]
    assert json.loads(seen[0].content) == {
        "timeout": 25,
        "allowed_updates": ["message", "callback_query"],
    }
    await telegram.updates(6)
    assert json.loads(seen[1].content)["offset"] == 6
    await telegram.send(
        1, "Accept **this**?", buttons=[("Accept", "act:1:yes"), ("Reject", "act:1:no")]
    )
    sent = json.loads(seen[2].content)
    assert sent["text"] == "Accept <b>this</b>?" and sent["parse_mode"] == "HTML"
    assert sent["reply_markup"]["inline_keyboard"] == [
        [
            {"text": "Accept", "callback_data": "act:1:yes"},
            {"text": "Reject", "callback_data": "act:1:no"},
        ]
    ]
    with pytest.raises(TelegramError, match="chat not found") as refused:
        await telegram.send(1, "refuse this")
    with pytest.raises(TelegramError, match="did not answer") as failed:
        await telegram.answer_button("1", "x")
    for error in (refused.value, failed.value):
        assert TOKEN not in str(error) and TOKEN not in repr(error.__cause__)
    await telegram.aclose()
    assert len(seen) >= 4
    assert not [r for r in caplog.records if TOKEN in r.getMessage() or "bot" in r.getMessage()]
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_the_token_is_read_from_its_mounted_file_or_not_at_all(tmp_path: Path) -> None:
    assert token_from(Settings()) is None
    assert token_from(Settings(telegram_token_file=tmp_path / "missing")) is None
    path = tmp_path / "bot-token"
    path.write_text(f"{TOKEN}\n")
    path.chmod(0o600)
    assert token_from(Settings(telegram_token_file=path)) == TOKEN
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        token_from(Settings(telegram_token_file=path))
    for wrong in ("http://api.telegram.org", "https://api.telegram.org/bot"):
        with pytest.raises(ValueError):
            Settings(telegram_api_url=wrong)
    with pytest.raises(ValueError):
        Settings(chat_digest_hour=24)


# --- what the administrator sends -------------------------------------------------------


async def test_only_the_administrator_in_a_private_chat_is_answered(world: Any) -> None:
    for update in (
        _message("/status", sender=42),
        _message("/status", kind="group"),
        {"update_id": 2, "edited_message": {"text": "/status"}},
        {"update_id": 3, "callback_query": {"id": "1", "from": {"id": 42}, "data": "act:x:yes"}},
    ):
        await world.channel.handle(update)
    assert world.transport.sent == [] and world.transport.answered == []
    assert world.toolbox.conversation.listing()["items"] == []
    # What was written before the channel existed is not answered out of the blue.
    old = _message("What is open?")
    old["message"]["date"] = int((datetime.now(UTC) - timedelta(hours=3)).timestamp())
    await world.channel.handle(old)
    assert world.toolbox.conversation.listing()["items"] == []
    await world.channel.handle(_message("/help"))
    (text,) = world.transport.texts()
    assert "/status" in text and "/inbox" in text and "supervisor" in text
    await world.channel.handle(_message("/reboot now"))
    assert world.transport.texts() == ["I do not know /reboot. /help lists what I know."]
    await world.channel.handle(
        {"update_id": 4, "message": {**_message("x")["message"], "text": ""}}
    )
    assert world.transport.texts() == ["I can only read text."]


async def test_commands_are_answered_by_the_platform_and_the_rest_goes_to_the_supervisor(
    world: Any,
) -> None:
    await world.channel.handle(_message("/status@gntikbot"))
    (status,) = world.transport.texts()
    assert "**Running**: nothing." in status and "nothing pressing" in status
    await world.channel.handle(_message("/runs"))
    assert world.transport.texts() == ["Nothing has run yet."]
    await world.channel.handle(_message("/queues"))
    assert world.transport.texts() == ["There is no queue under way."]
    queue = world.queues.create("falcone-workflow", name="Next", item_ids=world.items)
    world.queues.start(UUID(queue["id"]))
    world.queues.advance()
    await world.channel.handle(_message("/status"))
    (status,) = world.transport.texts()
    assert "**Running**: 1 run of work." in status and "- #11 Expired cookie" in status
    assert "Queue **Next** (falcone-workflow): running, 0 of 2 done." in status
    await world.channel.handle(_message("/queues"))
    (listed,) = world.transport.texts()
    assert "**Next** (falcone-workflow): running, 0 of 2 done" in listed
    assert listed.endswith("https://ade.musematic.ai/console/queues")
    await world.channel.handle(_message("/runs"))
    assert "- **#11 Expired cookie**: queued" in world.transport.texts()[0]
    # Anything else is the administrator talking to the supervisor, in the one conversation.
    await world.channel.handle(_message("What should we do next?"))
    assert world.transport.sent == []
    (said,) = world.toolbox.conversation.listing()["items"]
    assert (said["role"], said["channel"], said["text"]) == (
        "administrator",
        "chat",
        "What should we do next?",
    )
    assert world.toolbox.conversation.listing()["turn"]["state"] == "queued"
    await world.channel.handle(_message("x" * 9000))
    assert "at most 8000 characters" in world.transport.texts()[0]


# --- what the platform tells ------------------------------------------------------------


async def test_the_supervisors_replies_to_chat_are_told_once_and_the_consoles_are_not(
    world: Any,
) -> None:
    assert await world.channel.deliver() == 0
    chat, console = world.turn("chat"), world.turn("console")
    world.toolbox.conversation.answered(chat, "Three items are **open**.")
    world.toolbox.conversation.answered(console, "Said in the console.")
    with world.sessions.begin() as session:
        session.add(
            SupervisorMessage(
                role="platform",
                channel="platform",
                run_id=chat,
                text="The supervisor could not answer. It ran out of time.",
            )
        )
    assert await world.channel.deliver() == 2
    assert world.transport.texts() == [
        "Three items are **open**.",
        "The supervisor could not answer. It ran out of time.",
    ]
    assert await world.channel.deliver() == 0
    with world.sessions() as session:
        assert {d.kind for d in session.scalars(select(ChatDelivery))} == {"message"}


async def test_a_proposal_is_answered_with_a_button(world: Any) -> None:
    run_id = world.turn()
    definition = world.toolbox.conversation.definitions.current()
    item = str(world.items[0])
    said = await world.toolbox.call(
        run_id, definition, "start_item", {"item": item, "reason": "You asked for it."}
    )
    assert said.text.startswith("Not done:")
    assert await world.channel.deliver() == 1
    ((text, buttons),) = world.transport.sent
    world.transport.sent.clear()
    assert text.startswith("The supervisor proposes: **Start work on “Expired cookie”")
    assert "Why: You asked for it." in text
    (pending,) = world.toolbox.pending()
    assert buttons == [
        ("Accept", f"act:{pending['action_id']}:yes"),
        ("Reject", f"act:{pending['action_id']}:no"),
    ]
    press = {
        "update_id": 9,
        "callback_query": {
            "id": "cb1",
            "from": {"id": ADMINISTRATOR},
            "data": buttons[0][1],
            "message": {"message_id": 77},
        },
    }
    await world.channel.handle(press)
    assert world.transport.answered == ["Accepted."] and world.transport.cleared == [77]
    assert world.transport.texts() == ["Accepted."]
    (run,) = world.runs.listing()["items"]
    assert run["kind"] == "deliver_issue"
    record = world.toolbox.record()["items"][0]
    assert (record["outcome"], record["decided_via"]) == ("accepted", "chat")
    # Pressed again, or a button that names nothing: said so, and nothing happens.
    await world.channel.handle(press)
    assert world.transport.answered[-1] == "That has already been decided"
    for data in (f"act:{uuid4()}:yes", "act:not-an-id:yes", "act:x:maybe", "other"):
        press["callback_query"]["data"] = data
        await world.channel.handle(press)
    assert world.transport.answered[-4:] == [
        "That is no longer there.",
        "That is no longer there.",
        "That button is no longer good.",
        "That button is no longer good.",
    ]
    assert len(world.runs.listing()["items"]) == 1
    # /inbox lists what waits, proposals with their buttons.
    world.transport.sent.clear()
    await world.toolbox.call(
        run_id, definition, "start_item", {"item": str(world.items[1]), "reason": "Next."}
    )
    await world.channel.handle(_message("/inbox"))
    assert any(buttons for _text, buttons in world.transport.sent)


async def test_runs_queues_and_accounts_that_need_the_administrator_are_told_once(
    world: Any,
) -> None:
    run = world.runs.start_delivery(world.items[0], toolchain={})
    assert await world.channel.deliver() == 0, "a run that is under way is not news"
    world.runs.run_waiting(UUID(run["id"]), outcome="ready", detail="2 files changed.")
    assert await world.channel.deliver() == 1
    (text,) = world.transport.texts()
    assert text.startswith("A change is ready: **#11 Expired cookie**. 2 files changed.")
    assert "your password" in text
    assert text.endswith(f"https://ade.musematic.ai/console/run?id={run['id']}")
    assert await world.channel.deliver() == 0
    failed = world.runs.start_delivery(world.items[1], toolchain={})
    world.runs.run_finished(
        UUID(failed["id"]), state="failed", outcome="tests_failed", detail="The tests failed."
    )
    assert await world.channel.deliver() == 1
    assert world.transport.texts()[0].startswith(
        "A run failed: **#12 Export to CSV**. The tests failed."
    )
    # A queue the platform paused, and one the supervisor proposed.
    world.runs.run_finished(UUID(run["id"]), state="succeeded", outcome="published", detail=None)
    queue = world.queues.create(
        "falcone-workflow",
        name="Next",
        item_ids=world.items,
        proposed_by="supervisor",
        reason="Smallest first.",
    )
    assert await world.channel.deliver() == 1
    (proposed,) = world.transport.texts()
    assert proposed.startswith("The supervisor proposed the queue **Next**. Smallest first.")
    world.queues.start(UUID(queue["id"]))
    world.queues.advance()
    world.queues.pause(UUID(queue["id"]), "Paused by the supervisor: the same test keeps failing.")
    assert await world.channel.deliver() == 1
    assert "is paused. Paused by the supervisor" in world.transport.texts()[0]
    # An account the provider no longer accepts.
    account = UUID(world.accounts.create(name="claude one", provider="claude")["id"])
    world.accounts.signed_in(account, None)
    world.runs.account_learned(
        account,
        state="sign_in_required",
        reason="The provider refused the stored sign-in.",
        limited_until=None,
    )
    assert await world.channel.deliver() == 1
    assert world.transport.texts()[0].startswith("The account **claude one** needs a new sign-in.")
    assert await world.channel.deliver() == 0
    # A batch that has ended, was late, or failed; not one the administrator stopped.
    from engineering_control.db.models import ModelBatch

    def batch(**fields: Any) -> None:
        with world.sessions.begin() as session:
            session.add(
                ModelBatch(
                    provider="anthropic",
                    model="claude-haiku-5-5",
                    requests=4,
                    deadline_at=datetime.now(UTC),
                    **fields,
                )
            )

    batch(state="ended", answered=4)
    batch(state="ended", answered=1, stopping="late", detail="1 of 4 were answered.")
    batch(state="failed", answered=0, detail="Anthropic rejected the request.")
    batch(state="ended", answered=0, stopping="stopped")
    batch(state="waiting", answered=0)
    assert await world.channel.deliver() == 3
    assert sorted(world.transport.texts()) == [
        "A batch at Anthropic API failed. Anthropic rejected the request.",
        "A batch at Anthropic API has ended: 4 of 4 questions answered. The run goes on.",
        "A batch at Anthropic API was not answered in its time. 1 of 4 were answered.",
    ]
    assert await world.channel.deliver() == 0


async def test_the_digest_is_sent_once_a_day_after_its_hour(world: Any) -> None:
    started = world.channel.since()
    world.channel.remember("since", (started - timedelta(days=2)).isoformat())
    run = world.runs.start_delivery(world.items[0], toolchain={})
    world.runs.run_finished(
        UUID(run["id"]), state="failed", outcome="x", detail="The tests failed."
    )
    world.transport.sent.clear()
    with world.sessions.begin() as session:
        session.add(ChatDelivery(kind="run", ref=f"{run['id']}:failed:x"))
    day = datetime.now(UTC).replace(hour=6, minute=0)
    assert await world.channel.deliver(day) == 0, "before its hour"
    assert await world.channel.deliver(day.replace(hour=7, minute=5)) == 1
    (digest,) = world.transport.texts()
    assert digest.splitlines()[0] == "**The last day**"
    assert "Work: 1 run started: 1 failed." in digest and "Supervisor: not asked." in digest
    assert await world.channel.deliver(day.replace(hour=9)) == 0, "once a day"
    assert await world.channel.deliver(day.replace(hour=8) + timedelta(days=1)) == 1
    world.transport.sent.clear()
    await world.channel.handle(_message("/digest"))
    assert world.transport.texts()[0].startswith("**The last day**")
    # A channel that has only just started has no day to report yet.
    noon = day.replace(hour=12, minute=0) + timedelta(days=3)
    world.channel.remember("since", (noon - timedelta(minutes=20)).isoformat())
    assert await world.channel.deliver(noon) == 0
    assert await world.channel.deliver(noon + timedelta(hours=2)) == 1
