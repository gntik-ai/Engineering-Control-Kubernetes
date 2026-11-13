"""The supervisor's operations role: trouble with the work is noticed by the platform,
looked at by the supervisor in a turn of its own, and acted on within limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from engineering_control.db.models import (
    SandboxRun,
    SandboxStep,
    SupervisorIncident,
    SupervisorMessage,
    WorkQueue,
)
from engineering_control.supervisor.definition import DEFAULT_PROMPT, TOOLS, default_tools
from engineering_control.supervisor.operations import (
    INSTRUCTIONS,
    MAX_LOOKS,
    NEWS_MINUTES,
    STALL_MINUTES,
    Operations,
)
from tests.test_supervisor import (  # noqa: F401 - ``world`` is a fixture
    _claude_said,
    _log,
    _ran,
    world,
)


@pytest.fixture
def site(world: Any) -> Any:  # noqa: F811 - the fixture of the supervisor tests
    world.operations = world.launcher.operations
    world.queues = world.toolbox.queues
    world.backlog.route(world.item_id, "issues")
    world.account("Claude one", "claude")
    return world


def _fail(
    site: Any, run_id: str, outcome: str = "not_started", detail: str = "No sandbox."
) -> None:
    """End a run as failed, at its current step, the way the launcher does."""
    with site.sessions.begin() as session:
        step = session.scalar(
            select(SandboxStep)
            .where(SandboxStep.run_id == UUID(run_id))
            .order_by(SandboxStep.position.desc())
            .limit(1)
        )
        step_id = step.id
    site.runs.step_finished(step_id, state="failed", outcome=outcome, detail=detail)
    site.runs.run_finished(UUID(run_id), state="failed", outcome=outcome, detail=detail)


def _failed_run(site: Any, **how: Any) -> str:
    run = site.runs.start_item(site.item_id, toolchain={})
    _fail(site, run["id"], **how)
    return str(run["id"])


def _incidents(site: Any) -> list[SupervisorIncident]:
    with site.sessions() as session:
        return list(
            session.scalars(select(SupervisorIncident).order_by(SupervisorIncident.created_at))
        )


def _disable(site: Any, **operations: Any) -> None:
    site.definitions.save(
        prompt=DEFAULT_PROMPT,
        skills=[],
        tools=default_tools(),
        providers=["claude", "chatgpt"],
        account_id=None,
        exclusive=False,
        models={},
        note=None,
        operations=operations,
    )


# --- noticing ----------------------------------------------------------------------------


def test_a_failed_run_is_one_incident_however_often_it_is_seen(site: Any) -> None:
    run_id = _failed_run(site, detail="The sandbox did not start in time.")
    assert site.operations.watch() == 1
    assert site.operations.watch() == 0
    (incident,) = _incidents(site)
    assert (incident.kind, incident.ref, incident.state) == ("run_failed", run_id, "open")
    assert incident.summary == (
        "The run “#11 Signing in fails with an expired cookie” failed at the step prepare: "
        "The sandbox did not start in time."
    )
    assert incident.item_id == site.item_id and incident.facts["outcome"] == "not_started"
    # A failure from before the platform was looking is history, not news.
    old = _failed_run(site)
    with site.sessions.begin() as session:
        session.get(SandboxRun, UUID(old)).finished_at = datetime.now(UTC) - timedelta(
            minutes=NEWS_MINUTES + 5
        )
    assert site.operations.watch() == 0
    # Runs that are not work on a project are not looked at: a failed trial of an account
    # says so on the accounts page.
    account = site.account("Second", "claude")
    trial = site.runs.start_account_check(account)
    _fail(site, trial["id"])
    assert site.operations.watch() == 0


def test_a_step_that_waits_too_long_and_a_queue_that_paused_itself_are_incidents(
    site: Any,
) -> None:
    run = site.runs.start_item(site.item_id, toolchain={})
    assert site.operations.watch() == 0, "a step that only just started waiting is not stuck"
    with site.sessions.begin() as session:
        step = session.scalar(select(SandboxStep).where(SandboxStep.run_id == UUID(run["id"])))
        step.created_at = datetime.now(UTC) - timedelta(minutes=STALL_MINUTES + 7)
        step.detail = "Waiting for an account that is signed in and free."
    assert site.operations.watch() == 1
    (stalled,) = _incidents(site)
    assert (stalled.kind, stalled.ref) == ("run_stalled", f"{run['id']}:1")
    assert "has waited 27 minutes to start its step prepare" in stalled.summary
    assert "Waiting for an account" in stalled.summary
    # A run that is being stopped is not stuck.
    site.runs.request_cancel(UUID(run["id"]))

    made = site.queues.create("falcone-workflow", name="Fixes", item_ids=[site.item_id])
    with site.sessions.begin() as session:
        queue = session.get(WorkQueue, UUID(made["id"]))
        queue.state, queue.pause_reason = "paused", "Paused while I am away."
    assert site.operations.watch() == 0, "a queue the administrator paused is not trouble"
    with site.sessions.begin() as session:
        queue = session.get(WorkQueue, UUID(made["id"]))
        queue.pause_reason = "2 items in a row failed. Look at their runs, then resume the queue."
        for entry in queue_entries(session, queue.id):
            entry.state = "failed"
    assert site.operations.watch() == 1
    paused = _incidents(site)[-1]
    assert (paused.kind, paused.ref) == ("queue_paused", f"{made['id']}:1")
    assert paused.facts["queue_id"] == made["id"]


def queue_entries(session: Any, queue_id: UUID) -> list[Any]:
    from engineering_control.db.models import WorkQueueEntry

    return list(session.scalars(select(WorkQueueEntry).where(WorkQueueEntry.queue_id == queue_id)))


# --- asking the supervisor ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_supervisor_is_asked_in_a_turn_of_its_own(site: Any) -> None:
    run_id = _failed_run(site, detail="The sandbox did not start in time.")
    site.operations.watch()
    turn_id = site.operations.dispatch()
    assert turn_id is not None
    (incident,) = _incidents(site)
    assert (incident.state, incident.turn_run_id, incident.facts["looks"]) == (
        "looking",
        turn_id,
        1,
    )
    (turn,) = site.turns()
    assert turn.title.startswith("Supervisor (operations): The run “#11 Signing in fails")
    assert turn.data["channel"] == "operations" and turn.data["incidents"] == [str(incident.id)]
    # The conversation says what the platform asked, so that the reply reads in its place.
    with site.sessions() as session:
        asked = session.scalar(select(SupervisorMessage).where(SupervisorMessage.run_id == turn_id))
    assert asked.role == "platform" and asked.text.startswith(
        "The platform asked the supervisor to look at:\n- The run “#11"
    )
    # One turn at a time: nothing more is asked while this one is under way.
    _failed_run(site)
    site.operations.watch()
    assert site.operations.dispatch() is None

    await site.launcher.tick()
    prompt = site.job()["commands"][0]["stdin"]
    assert "The administrator has not written: the platform is asking you." in prompt
    assert "## What the platform asks you to look at" in prompt
    assert INSTRUCTIONS.splitlines()[0] in prompt
    assert f"Ids: incident {incident.id}, run {run_id}, item {site.item_id}." in prompt
    assert "## The administrator's new message" not in prompt
    assert "Deal with the incidents now, then write your report." in prompt

    # A turn that answers without closing the incident closes it by what it said.
    site.fake.finish(_log([_ran("agent", _claude_said("The sandbox node was away. Tried again."))]))
    await site.launcher.tick()
    await site.launcher.tick()
    site.operations.settle()
    incident = _incidents(site)[0]
    assert incident.state == "handled" and incident.closed_at is not None
    assert incident.diagnosis == "The sandbox node was away. Tried again."
    replies = site.conversation.listing()["items"]
    assert replies[-1]["role"] == "supervisor" and replies[-1]["channel"] == "console"


def test_the_administrators_message_goes_first_and_the_hour_has_a_limit(site: Any) -> None:
    _failed_run(site)
    site.operations.watch()
    # A message of the administrator that waits is answered before trouble is looked at.
    with site.sessions.begin() as session:
        session.add(SupervisorMessage(role="administrator", channel="console", text="Hello"))
    assert site.operations.dispatch() is None
    with site.sessions.begin() as session:
        for message in session.scalars(select(SupervisorMessage)):
            session.delete(message)

    _disable(site, enabled=True, per_hour=1)
    first = site.operations.dispatch()
    assert first is not None
    site.runs.run_finished(first, state="failed", outcome="no_answer", detail="No answer.")
    site.operations.settle()
    assert _incidents(site)[0].state == "open", "it is looked at once more"
    assert site.operations.dispatch() is None, "not more often within an hour than allowed"


def test_switched_off_trouble_waits_in_the_inbox_and_can_be_dismissed(site: Any) -> None:
    _disable(site, enabled=False, per_hour=6)
    run_id = _failed_run(site, detail="The tests failed.")
    site.operations.watch()
    assert site.operations.dispatch() is None and site.turns() == []
    (entry,) = [e for e in site.toolbox.inbox_entries() if e["kind"] == "incident"]
    assert entry["run_id"] == run_id and "The tests failed." in entry["detail"]
    assert entry["project"] == "falcone-workflow"
    listed = site.operations.listing()
    assert listed["settings"] == {"enabled": False, "per_hour": 6} and listed["total"] == 1
    closed = site.operations.dismiss(UUID(entry["incident_id"]))
    assert closed["state"] == "dismissed"
    assert [e for e in site.toolbox.inbox_entries() if e["kind"] == "incident"] == []
    assert site.operations.listing()["total"] == 0
    assert site.operations.listing(everything=True)["total"] == 1
    with pytest.raises(Exception, match="closed already"):
        site.operations.dismiss(UUID(entry["incident_id"]))
    with pytest.raises(Exception, match="whole number"):
        _disable(site, per_hour="often")
    with pytest.raises(Exception, match="Between 1 and 30"):
        _disable(site, per_hour=0)


def test_a_supervisor_that_cannot_look_leaves_the_incident_to_the_administrator(
    site: Any,
) -> None:
    _failed_run(site)
    site.operations.watch()
    for look in range(1, MAX_LOOKS + 1):
        turn = site.operations.dispatch()
        assert turn is not None, f"look {look}"
        site.runs.run_finished(turn, state="failed", outcome="no_answer", detail="No answer.")
        site.operations.settle()
    (incident,) = _incidents(site)
    assert (incident.state, incident.facts["needs"]) == ("waiting", "administrator")
    assert site.operations.dispatch() is None
    (entry,) = [e for e in site.toolbox.inbox_entries() if e["kind"] == "incident"]
    assert "could not look at this" in entry["diagnosis"]


# --- what the supervisor may do ----------------------------------------------------------


@pytest.mark.asyncio
async def test_trying_again_is_automatic_only_when_sure_and_only_a_few_times(site: Any) -> None:
    failed = _failed_run(site)
    turn, token = await site.running_turn("Look at the failed run.")
    assert {"retry_run", "cancel_run", "skip_item", "requeue_item", "conclude_incident"} <= {
        tool["name"] for tool in site.rpc(turn, token, "tools/list")["tools"]
    }
    said, error = site.call(turn, token, "retry_run", run=failed, reason="The node was away.")
    assert not error and "Not done" in said and "confidence: not given" in said
    said, error = site.call(
        turn, token, "retry_run", run=failed, reason="The node was away.", confidence="high"
    )
    assert not error and said.startswith("Done: Try “#11 Signing in fails")
    second = site.runs.for_items([str(site.item_id)])[str(site.item_id)]
    assert second["id"] != failed and second["state"] == "queued"
    # A run that has not failed is not tried again, and one is enough at a time.
    said, error = site.call(
        turn, token, "retry_run", run=second["id"], reason="Again.", confidence="high"
    )
    assert error and "is queued, not failed" in said

    # A cause that trying again does not remove waits for the administrator.
    _fail(site, second["id"], outcome="blocked", detail="The implementer could not do it.")
    said, _ = site.call(
        turn, token, "retry_run", run=second["id"], reason="Maybe now.", confidence="high"
    )
    assert "Not done" in said and "does not remove" in said

    # And so does work that already ran three times within a day.
    third = site.runs.start_item(site.item_id, toolchain={})
    _fail(site, third["id"])
    said, _ = site.call(
        turn, token, "retry_run", run=third["id"], reason="The node again.", confidence="high"
    )
    assert "Not done" in said and "already ran 3 times within a day" in said
    assert [action.outcome for action in site.actions() if action.tool == "retry_run"] == [
        "pending",
        "applied",
        "refused",
        "pending",
        "pending",
    ]


@pytest.mark.asyncio
async def test_a_run_that_holds_work_is_stopped_only_with_confirmation(site: Any) -> None:
    run = site.runs.start_item(site.item_id, toolchain={})
    turn, token = await site.running_turn("Stop it.")
    said, error = site.call(
        turn, token, "cancel_run", run=run["id"], reason="It waits for nothing."
    )
    assert not error and said.startswith("Done: Stop the run")
    assert site.runs.get(UUID(run["id"]))["cancel_requested"] is True
    site.runs.run_finished(UUID(run["id"]), state="cancelled", outcome="cancelled", detail=None)
    said, error = site.call(turn, token, "cancel_run", run=run["id"], reason="Again.")
    assert error and "already ended" in said

    working = site.runs.start_item(site.item_id, toolchain={})
    with site.sessions.begin() as session:
        session.add(
            SandboxStep(
                run_id=UUID(working["id"]),
                position=2,
                name="implement",
                state="succeeded",
                attempt=1,
            )
        )
    said, _ = site.call(turn, token, "cancel_run", run=working["id"], reason="It takes long.")
    assert "Not done" in said and "holds work that would be discarded" in said
    assert site.runs.get(UUID(working["id"]))["cancel_requested"] is False
    # The turn's own run is not work on a project.
    said, error = site.call(turn, token, "cancel_run", run=str(turn), reason="Me.")
    assert error and "Only a run of work" in said


@pytest.mark.asyncio
async def test_skipping_records_the_reason_and_needs_certainty(site: Any) -> None:
    turn, token = await site.running_turn("Skip what cannot be done.")
    item = str(site.item_id)
    said, _ = site.call(
        turn, token, "skip_item", item=item, reason="It asks for two things.", confidence="low"
    )
    assert "Not done" in said and site.backlog.get(site.item_id)["state"] == "proposed"
    running = site.runs.start_item(site.item_id, toolchain={})
    said, error = site.call(
        turn, token, "skip_item", item=item, reason="Unworkable.", confidence="high"
    )
    assert error and "A run for this item is under way" in said
    _fail(site, running["id"])
    said, error = site.call(
        turn,
        token,
        "skip_item",
        item=item,
        reason="It names an API that is gone.",
        confidence="high",
    )
    assert not error and said.startswith("Done: Skip “Signing in fails")
    skipped = site.backlog.get(site.item_id)
    assert skipped["state"] == "skipped"
    assert skipped["facts"] == {
        "skipped_by": "supervisor",
        "skipped_reason": "It names an API that is gone.",
    }


@pytest.mark.asyncio
async def test_an_item_is_returned_to_its_queue_once_without_asking(site: Any) -> None:
    made = site.queues.create("falcone-workflow", name="Fixes", item_ids=[site.item_id])
    queue_id = UUID(made["id"])
    site.queues.start(queue_id)
    assert site.queues.advance() == 1
    run = site.runs.for_items([str(site.item_id)])[str(site.item_id)]
    _fail(site, run["id"])
    site.queues.advance()
    turn, token = await site.running_turn("Put it back.")
    arguments = {"queue": made["id"], "item": str(site.item_id), "reason": "The node was away."}
    said, error = site.call(turn, token, "requeue_item", **arguments)
    assert not error and said.startswith("Done: Return “Signing in fails")
    (entry,) = site.queues.get(queue_id)["entries"]
    assert (
        entry["state"] == "waiting"
        and "Returned to the queue: The node was away." in entry["detail"]
    )
    said, error = site.call(turn, token, "requeue_item", **arguments)
    assert error and "is waiting in the queue, not failed" in said
    # The queue had finished with its one item; it is paused now, and resuming it is the
    # administrator's to confirm.
    assert site.queues.get(queue_id)["state"] == "paused"
    assert site.queues.advance() == 0
    site.queues.start(queue_id)
    # It runs and fails again: the second return waits for the administrator.
    assert site.queues.advance() == 1
    again = site.runs.for_items([str(site.item_id)])[str(site.item_id)]
    _fail(site, again["id"])
    site.queues.advance()
    said, _ = site.call(turn, token, "requeue_item", **arguments)
    assert "Not done" in said and "returned to the queue once already" in said


@pytest.mark.asyncio
async def test_an_incident_is_closed_with_a_diagnosis_or_waits_for_the_decision(site: Any) -> None:
    failed = _failed_run(site, outcome="blocked", detail="The implementer could not do it.")
    site.operations.watch()
    turn = site.operations.dispatch()
    await site.launcher.tick()
    argv = site.job()["commands"][0]["argv"]
    import json

    servers = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
    token = servers["platform"]["headers"]["Authorization"].removeprefix("Bearer ")
    (listed,) = json.loads(site.call(turn, token, "list_incidents")[0])
    assert listed["state"] == "looking" and listed["run_id"] == failed
    # It is not sure enough to skip the item itself: the skip waits, and so does the
    # incident, until the administrator has answered.
    said, _ = site.call(
        turn,
        token,
        "skip_item",
        item=str(site.item_id),
        reason="The issue asks for an API that does not exist.",
        confidence="medium",
    )
    assert "Not done" in said
    said, error = site.call(
        turn,
        token,
        "conclude_incident",
        incident=listed["id"],
        diagnosis="The issue cannot be resolved as written.",
        confidence="medium",
        action="Proposed to skip the item.",
    )
    assert not error and '"state":"waiting"' in said
    (incident,) = _incidents(site)
    assert (incident.state, incident.facts["needs"], incident.confidence) == (
        "waiting",
        "decision",
        "medium",
    )
    assert incident.diagnosis == (
        "The issue cannot be resolved as written.\n\nWhat was done: Proposed to skip the item."
    )
    # It is in the inbox once, as the action that waits, not as an incident besides.
    kinds = [entry["kind"] for entry in site.toolbox.inbox_entries()]
    assert "supervisor_action" in kinds and "incident" not in kinds
    (pending,) = site.actions(outcome="pending")
    site.toolbox.decide(pending.id, accept=True, via="console")
    assert site.backlog.get(site.item_id)["state"] == "skipped"
    site.operations.settle()
    assert _incidents(site)[0].state == "handled"
    said, error = site.call(
        turn,
        token,
        "conclude_incident",
        incident=listed["id"],
        diagnosis="Again.",
        confidence="high",
    )
    assert error and "closed already" in said


@pytest.mark.asyncio
async def test_an_audit_is_started_by_the_supervisor_only_with_confirmation(site: Any) -> None:
    from engineering_control.db.models import ProjectRecord, ProjectSection

    with site.sessions.begin() as session:
        project = session.query(ProjectRecord).filter_by(name="falcone-workflow").one()
        section = (
            session.query(ProjectSection).filter_by(project_id=project.id, key="code-audit").one()
        )
        section.flow_key = "audit-code"
    turn, token = await site.running_turn("Audit the session code.")
    arguments = {
        "project": "falcone-workflow",
        "section": "code-audit",
        "scope": "The session handling",
        "reason": "The administrator asked for it.",
    }
    said, error = site.call(turn, token, "start_section_run", **arguments)
    assert not error and "Not done" in said and "confirm this kind of action yourself" in said
    (pending,) = site.actions(outcome="pending")
    assert pending.summary == "Start an audit for falcone-workflow: The session handling."
    said, error = site.call(turn, token, "start_section_run", **{**arguments, "section": "issues"})
    assert error and "no flow that produces items by itself" in said
    site.toolbox.decide(pending.id, accept=True, via="console")
    (run,) = [r for r in site.runs.listing()["items"] if r["kind"] == "audit_code"]
    assert run["title"] == "Audit: The session handling"
    assert TOOLS["conclude_incident"].modes == ("automatic",)


# --- the console -------------------------------------------------------------------------


def test_the_console_lists_incidents_and_saves_how_the_supervisor_operates(site: Any) -> None:
    from fastapi.testclient import TestClient

    from engineering_control.config import Settings
    from engineering_control.dashboard.app import create_app
    from engineering_control.dashboard.repository import DashboardRepository

    password = "correct horse battery staple"  # noqa: S105 - deterministic test fixture
    repository = DashboardRepository(str(site.sessions.kw["bind"].url))
    repository.initialize_password_sync(password)
    assert isinstance(repository.operations, Operations)
    run_id = _failed_run(site, detail="The tests failed.")
    repository.operations.watch()
    client = TestClient(
        create_app(
            settings=Settings(database_url=str(repository.engine.url)),
            repository=repository,
            session_key=b"g" * 48,
        ),
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    )
    with client:
        token = client.get("/login/challenge").json()["csrf_token"]
        client.post("/login", data={"csrf_token": token, "password": password})

        def post(path: str, **fields: Any) -> httpx.Response:
            csrf = client.get("/api/dashboard/session").json()["csrf_token"]
            return client.post(f"/api/dashboard{path}", json={"csrf_token": csrf, **fields})

        listed = client.get("/api/dashboard/supervisor/incidents").json()
        assert listed["settings"] == {"enabled": True, "per_hour": 6}
        (incident,) = listed["items"]
        assert incident["run_id"] == run_id and incident["state"] == "open"

        settings = client.get("/api/dashboard/supervisor/settings").json()
        definition = settings["definition"]
        assert definition["operations"] == {"enabled": True, "per_hour": 6}
        assert "retry_run" in {tool["name"] for tool in settings["catalogue"]}
        saved = post(
            "/supervisor/settings",
            current_password=password,
            prompt=definition["prompt"],
            skills=definition["skills"],
            tools=definition["tools"],
            providers=definition["providers"],
            operations={"enabled": False, "per_hour": 2},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["operations"] == {"enabled": False, "per_hour": 2}
        # Switched off, the incident is the administrator's: it is in the inbox.
        inbox = client.get("/api/dashboard/inbox").json()["items"]
        assert [entry["kind"] for entry in inbox if entry["kind"] == "incident"] == ["incident"]
        dismissed = post(f"/supervisor/incidents/{incident['id']}/dismiss")
        assert dismissed.status_code == 200 and dismissed.json()["state"] == "dismissed"
        assert post(f"/supervisor/incidents/{incident['id']}/dismiss").status_code == 409
        assert client.get("/api/dashboard/supervisor/incidents").json()["items"] == []
        everything = client.get("/api/dashboard/supervisor/incidents?everything=true").json()
        assert [entry["state"] for entry in everything["items"]] == ["dismissed"]
