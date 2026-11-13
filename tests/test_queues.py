"""Queues of work and budgets: made, ordered, started, moved forward by the launcher, and
proposed by the supervisor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.accounts import Accounts
from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    ProjectRecords,
)
from engineering_control.dashboard.project_sections import SectionRecords
from engineering_control.db.base import Base
from engineering_control.db.models import (
    BacklogItem,
    ProjectRecord,
    ProjectSection,
    SandboxRun,
    SandboxStep,
)
from engineering_control.sandbox.queues import Budgets, Queues
from engineering_control.sandbox.runs import Runs
from engineering_control.supervisor.service import toolbox_from
from tests.test_dashboard_accounts import _console

REPOSITORY = "gntik-ai/falcone-workflow"
TITLES = ("Expired cookie", "Export to CSV", "Slow search", "Wrong total")


@pytest.fixture
def world(tmp_path: Path) -> Any:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'queues.db'}")
    Base.metadata.create_all(engine)
    sessions: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)
    projects = ProjectRecords(sessions)
    for name in ("falcone-workflow", "falcone"):
        projects.create(
            name=name,
            display_name=name.title(),
            repository=f"gntik-ai/{name}",
            default_branch="main",
        )
    SectionRecords(sessions).sync_core()
    with sessions.begin() as session:
        project = session.query(ProjectRecord).filter_by(name="falcone-workflow").one()
        other = session.query(ProjectRecord).filter_by(name="falcone").one()
        issues = session.query(ProjectSection).filter_by(project_id=project.id, key="issues").one()
        issues.flow_key = "deliver-issue"
        made = []
        for number, title in enumerate(TITLES, start=11):
            item = BacklogItem(
                project_id=project.id,
                section_id=issues.id,
                title=title,
                source="github",
                state="accepted",
                routed_by="label",
                repository=REPOSITORY,
                issue_number=number,
                issue_labels=[],
            )
            session.add(item)
            made.append(item)
        elsewhere = BacklogItem(
            project_id=other.id,
            title="Of another project",
            source="manual",
            state="accepted",
            issue_labels=[],
        )
        session.add(elsewhere)
        session.flush()
        item_ids = [item.id for item in made]
        elsewhere_id = elsewhere.id
        project_id = project.id

    class World:
        def __init__(self) -> None:
            self.sessions = sessions
            self.items = item_ids
            self.elsewhere = elsewhere_id
            self.project_id = project_id
            self.runs = Runs(sessions)
            self.budgets = Budgets(sessions)
            self.queues = Queues(sessions, self.runs, self.budgets)
            self.backlog = BacklogItems(sessions)
            self.accounts = Accounts(sessions)
            self.toolbox = toolbox_from(sessions, None)

        def queue(self, *indexes: int, **options: Any) -> dict[str, Any]:
            return self.queues.create(
                "falcone-workflow",
                name=options.pop("name", "Next"),
                item_ids=[item_ids[index] for index in (indexes or range(len(item_ids)))],
                **options,
            )

        def end(self, item_index: int, state: str, detail: str | None = None) -> None:
            """The run of an item ends that way."""
            with sessions.begin() as session:
                run = session.scalars(
                    select(SandboxRun)
                    .where(SandboxRun.item_id == item_ids[item_index])
                    .order_by(SandboxRun.created_at.desc())
                ).first()
                assert run is not None, "the item has no run"
                run.state = state
                run.detail = detail

        def states(self, queue_id: str) -> list[tuple[str, str]]:
            queue = self.queues.get(UUID(queue_id))
            return [(entry["title"], entry["state"]) for entry in queue["entries"]]

        def turn(self) -> UUID:
            """A turn of the supervisor that is running, for calls to its tools."""
            with sessions.begin() as session:
                run = SandboxRun(
                    kind="supervisor_turn",
                    state="running",
                    title="Turn",
                    cancel_requested=False,
                    data={},
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


def test_a_queue_is_a_draft_of_open_items_of_one_project_in_order(world: Any) -> None:
    queue = world.queue(2, 0, 3, name="  Next  up ", at_once=2)
    assert (queue["name"], queue["state"], queue["at_once"]) == ("Next up", "draft", 2)
    assert (queue["project"], queue["proposed_by"]) == ("falcone-workflow", None)
    assert [entry["title"] for entry in queue["entries"]] == [
        "Slow search",
        "Expired cookie",
        "Wrong total",
    ]
    assert queue["counts"] == {
        "waiting": 3,
        "running": 0,
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "total": 3,
    }
    assert queue["entries"][0]["section"] == {
        "key": "issues",
        "name": "Issues",
        "flow": "deliver-issue",
    }
    listed = world.queues.listing(project="falcone-workflow")["items"]
    assert [item["name"] for item in listed] == ["Next up"]
    assert world.queues.listing(project="falcone")["items"] == []
    # An item is in one queue under way at a time, and only an open item of the project.
    with pytest.raises(ProjectConflict, match="already in the queue Next up"):
        world.queue(0, name="Another")
    with pytest.raises(ProjectInvalid, match="its own project"):
        world.queues.create("falcone-workflow", name="Mixed", item_ids=[world.elsewhere])
    with pytest.raises(ProjectInvalid, match="only once"):
        world.queues.create("falcone-workflow", name="Twice", item_ids=[world.items[1]] * 2)
    world.backlog.set_state(world.items[1], "done")
    with pytest.raises(ProjectInvalid, match="not open any more"):
        world.queue(1, name="Closed")
    for wrong in ({"name": " "}, {"at_once": 0}, {"at_once": 5}):
        with pytest.raises(ProjectInvalid):
            world.queue(1, **{"name": "Other", **wrong})
    with pytest.raises(ProjectInvalid, match="at least one item"):
        world.queues.create("falcone-workflow", name="Empty", item_ids=[])
    with pytest.raises(ProjectNotFound):
        world.queues.create("nothing", name="Next", item_ids=[world.items[1]])
    assert world.queues.for_items([str(world.items[0]), str(world.items[1])]) == {
        str(world.items[0]): {"id": queue["id"], "name": "Next up", "state": "draft"}
    }


def test_the_waiting_items_are_reordered_extended_and_taken_out(world: Any) -> None:
    queue = world.queue(0, 1, 2)
    queue_id = UUID(queue["id"])
    reordered = world.queues.reorder(queue_id, [world.items[2], world.items[0], world.items[1]])
    assert [entry["title"] for entry in reordered["entries"]] == [
        "Slow search",
        "Expired cookie",
        "Export to CSV",
    ]
    assert [entry["position"] for entry in reordered["entries"]] == [1, 2, 3]
    for wrong in ([world.items[0]], [world.items[0], world.items[1], world.items[3]]):
        with pytest.raises(ProjectConflict, match="every item that still waits"):
            world.queues.reorder(queue_id, wrong)
    extended = world.queues.add_items(queue_id, [world.items[3], world.items[0]])
    assert [entry["title"] for entry in extended["entries"]][-1] == "Wrong total"
    assert extended["counts"]["total"] == 4
    shortened = world.queues.remove_item(queue_id, world.items[0])
    assert "Expired cookie" not in [entry["title"] for entry in shortened["entries"]]
    with pytest.raises(ProjectNotFound):
        world.queues.remove_item(queue_id, world.items[0])
    changed = world.queues.update(queue_id, name="Soon", at_once=3)
    assert (changed["name"], changed["at_once"]) == ("Soon", 3)
    # A draft is discarded; its items stay in the backlog and are free for another queue.
    assert world.queues.discard(queue_id)["name"] == "Soon"
    with pytest.raises(ProjectNotFound):
        world.queues.get(queue_id)
    assert world.backlog.get(world.items[2])["state"] == "accepted"
    world.queue(2, name="Again")


def test_a_started_queue_runs_its_items_in_order(world: Any) -> None:
    queue = world.queue(0, 1, 2)
    queue_id = UUID(queue["id"])
    assert world.queues.advance() == 0, "a draft starts nothing"
    assert world.queues.start(queue_id)["state"] == "running"
    with pytest.raises(ProjectConflict, match="already running"):
        world.queues.start(queue_id)
    assert world.queues.advance() == 1
    assert world.states(queue["id"]) == [
        ("Expired cookie", "running"),
        ("Export to CSV", "waiting"),
        ("Slow search", "waiting"),
    ]
    (run,) = world.runs.listing()["items"]
    assert run["kind"] == "deliver_issue" and run["title"] == "#11 Expired cookie"
    assert world.queues.advance() == 0, "one at a time"
    # A change that waits for the administrator does not hold the queue up.
    world.end(0, "waiting")
    assert world.queues.advance() == 1
    first = world.queues.get(queue_id)["entries"][0]
    assert (first["state"], first["detail"]) == ("done", "A change waits for your decision.")
    # What still waits can be reordered while the queue runs; what ran keeps its place.
    world.queues.pause(queue_id)
    assert world.queues.advance() == 0
    world.end(1, "succeeded")
    world.queues.advance()
    assert world.states(queue["id"])[1] == ("Export to CSV", "done")
    assert world.queues.get(queue_id)["state"] == "paused"
    with pytest.raises(ProjectConflict, match="Only an item that still waits"):
        world.queues.remove_item(queue_id, world.items[0])
    world.queues.start(queue_id)
    assert world.queues.advance() == 1
    world.end(2, "succeeded")
    world.queues.advance()
    finished = world.queues.get(queue_id)
    assert finished["state"] == "finished" and finished["finished_at"] is not None
    assert finished["counts"]["done"] == 3
    with pytest.raises(ProjectConflict, match="has finished"):
        world.queues.start(queue_id)
    # Given more to do, it is under way again and waits to be resumed.
    again = world.queues.add_items(queue_id, [world.items[3]])
    assert again["state"] == "paused" and "after it had finished" in again["pause_reason"]


def test_several_at_once_failures_in_a_row_and_items_that_cannot_start(world: Any) -> None:
    with world.sessions.begin() as session:
        # The second item's section has no flow: nothing can run for it.
        item = session.get(BacklogItem, world.items[1])
        audit = (
            session.query(ProjectSection)
            .filter_by(project_id=world.project_id, key="code-audit")
            .one()
        )
        item.section_id = audit.id
    queue = world.queue(0, 1, 2, 3, at_once=2)
    queue_id = UUID(queue["id"])
    world.queues.start(queue_id)
    assert world.queues.advance() == 1
    states = dict(world.states(queue["id"]))
    assert states["Expired cookie"] == "running" and states["Export to CSV"] == "skipped"
    skipped = world.queues.get(queue_id)["entries"][1]
    assert "no flow that delivers issues" in skipped["detail"]
    assert world.queues.advance() == 1, "the place of the skipped item is taken by the next"
    assert dict(world.states(queue["id"]))["Slow search"] == "running"
    world.end(0, "failed", "The tests failed.")
    world.queues.advance()
    assert world.queues.get(queue_id)["state"] == "running", "one failure does not stop it"
    world.end(2, "failed", "The reviewer asked for changes three times.")
    world.queues.advance()
    paused = world.queues.get(queue_id)
    assert paused["state"] == "paused" and "2 items in a row failed" in paused["pause_reason"]
    assert paused["entries"][2]["detail"] == "The reviewer asked for changes three times."
    assert [entry["kind"] for entry in world.queues.inbox()] == ["queue_paused"]
    # While an item still runs the queue cannot be discarded; once idle it can.
    assert dict(world.states(queue["id"]))["Wrong total"] == "running"
    with pytest.raises(ProjectConflict, match="is running"):
        world.queues.discard(queue_id)
    world.end(3, "cancelled")
    world.queues.advance()
    assert dict(world.states(queue["id"]))["Wrong total"] == "failed"
    world.queues.discard(queue_id)


def test_budgets_hold_back_what_the_platform_starts_by_itself(world: Any) -> None:
    listing = world.budgets.listing()
    assert listing["platform"] == {
        "scope": "platform",
        "scope_id": None,
        "at_once": None,
        "per_day": None,
        "running": 0,
        "started_today": 0,
    }
    assert {row["name"] for row in listing["projects"]} >= {"falcone-workflow", "falcone"}
    saved = world.budgets.save("project", world.project_id, at_once=1, per_day=3)
    assert (saved["at_once"], saved["per_day"], saved["budget"]) == (1, 3, "Falcone-Workflow")
    queue = world.queue(0, 1, 2, 3, at_once=3)
    queue_id = UUID(queue["id"])
    world.queues.start(queue_id)
    assert world.queues.advance() == 1, "the queue allows three, the project's budget one"
    waiting = world.queues.get(queue_id)
    assert waiting["waiting_for"] == "The budget of this project allows 1 at the same time."
    world.end(0, "succeeded")
    assert world.queues.advance() == 1
    world.end(1, "succeeded")
    assert world.queues.advance() == 1
    world.end(2, "succeeded")
    assert world.queues.advance() == 0
    assert world.queues.get(queue_id)["waiting_for"] == (
        "The budget of this project allows 3 in a day; that is used up."
    )
    row = next(r for r in world.budgets.listing()["projects"] if r["name"] == "falcone-workflow")
    assert (row["running"], row["started_today"]) == (0, 3)
    # The platform's budget comes first; clearing a limit lifts it.
    world.budgets.save("project", world.project_id, at_once=None, per_day=None)
    world.budgets.save("platform", None, at_once=None, per_day=3)
    assert world.budgets.may_start("falcone-workflow") == (
        "The budget of the platform allows 3 in a day; that is used up."
    )
    world.budgets.save("platform", None, at_once=None, per_day=None)
    assert world.budgets.may_start("falcone-workflow") is None
    assert world.queues.advance() == 1
    for wrong in (
        ("team", None, {"at_once": 1, "per_day": None}),
        ("platform", None, {"at_once": 0, "per_day": None}),
        ("platform", None, {"at_once": None, "per_day": "many"}),
    ):
        with pytest.raises(ProjectInvalid):
            world.budgets.save(wrong[0], wrong[1], **wrong[2])
    with pytest.raises(ProjectNotFound):
        world.budgets.save("project", uuid4(), at_once=1, per_day=None)


def test_an_account_past_its_budget_of_steps_is_given_to_no_step(world: Any) -> None:
    first = UUID(world.accounts.create(name="claude one", provider="claude")["id"])
    second = UUID(world.accounts.create(name="claude two", provider="claude")["id"])
    for account in (first, second):
        world.accounts.signed_in(account, None)
    with pytest.raises(ProjectInvalid, match="set on the account itself"):
        world.budgets.save("account", first, at_once=2, per_day=None)
    world.budgets.save("account", first, at_once=None, per_day=1)

    def lease() -> UUID | None:
        run = world.runs.start_account_check(second)
        with world.sessions.begin() as session:
            step = session.scalars(
                select(SandboxStep).where(SandboxStep.run_id == UUID(run["id"]))
            ).one()
            step.account_id = None
            step.provider = "claude"
        found = world.runs.lease(step.id)
        if found is not None:
            world.runs.step_started(step.id, "pod")
        world.runs.run_finished(UUID(run["id"]), state="succeeded", outcome="ready", detail=None)
        return found.account_id if found else None

    used = {lease(), lease(), lease()}
    assert used == {first, second}
    row = next(r for r in world.budgets.listing()["accounts"] if r["name"] == "claude one")
    assert (row["per_day"], row["started_today"]) == (1, 1)
    # From now on only the other account is given out.
    assert {lease(), lease()} == {second}


async def test_the_supervisor_proposes_a_queue_and_the_administrator_starts_it(world: Any) -> None:
    run_id = world.turn()
    definition = world.toolbox.conversation.definitions.current()

    async def call(tool: str, /, **arguments: Any) -> str:
        return (await world.toolbox.call(run_id, definition, tool, arguments)).text

    items = [
        {"item": str(world.items[2]), "reason": "The others build on it."},
        {"item": str(world.items[0]), "reason": "Small and safe."},
    ]
    said = await call(
        "propose_queue",
        project="falcone-workflow",
        name="Search first",
        reason="Dependencies first, then the small ones.",
        items=items,
    )
    assert said.startswith(
        "Done: Propose the queue “Search first” for falcone-workflow with 2 items"
    )
    (queue,) = json.loads(await call("list_queues", project="falcone-workflow"))
    assert (queue["state"], queue["proposed_by"]) == ("draft", "supervisor")
    read = json.loads(await call("get_queue", queue=queue["id"]))
    assert read["reason"] == "Dependencies first, then the small ones."
    assert [(entry["title"], entry["reason"]) for entry in read["items"]] == [
        ("Slow search", "The others build on it."),
        ("Expired cookie", "Small and safe."),
    ]
    # A proposed queue waits in the inbox until the administrator starts or discards it.
    (waiting,) = [e for e in world.toolbox.inbox_entries() if e["kind"].startswith("queue")]
    assert (waiting["kind"], waiting["queue"], waiting["count"]) == (
        "queue_proposed",
        "Search first",
        2,
    )
    # Its own draft the supervisor may reorder; starting it waits for the administrator.
    order = [str(world.items[0]), str(world.items[2])]
    assert (
        await call("reorder_queue", queue=queue["id"], items=order, reason="Safer.")
    ).startswith("Done:")
    assert (await call("reorder_queue", queue=queue["id"], items=order[:1], reason="x")).startswith(
        "Refused:"
    )
    said = await call("start_queue", queue=queue["id"], reason="You asked me to.")
    assert said.startswith("Not done:") and world.queues.get(UUID(queue["id"]))["state"] == "draft"
    (pending,) = world.toolbox.pending()
    assert pending["summary"] == "Start the queue “Search first” (falcone-workflow)."
    world.toolbox.decide(UUID(pending["action_id"]), accept=True, via="console")
    assert world.queues.get(UUID(queue["id"]))["state"] == "running"
    # A running queue is reordered only with the administrator's confirmation, and may be
    # paused by the supervisor itself, with its reason.
    said = await call("reorder_queue", queue=queue["id"], items=order, reason="Again.")
    assert said.startswith("Not done:") and "under way" in said
    assert (
        await call("pause_queue", queue=queue["id"], reason="Two runs failed on the same test.")
    ).startswith("Done:")
    paused = world.queues.get(UUID(queue["id"]))
    assert paused["state"] == "paused"
    assert paused["pause_reason"] == "Paused by the supervisor: Two runs failed on the same test."
    assert (await call("pause_queue", queue=queue["id"], reason="Again.")).startswith("Refused:")
    for tool, arguments in (
        ("get_queue", {"queue": str(uuid4())}),
        (
            "propose_queue",
            {
                "project": "falcone-workflow",
                "name": "X",
                "reason": "y",
                "items": [{"item": str(world.elsewhere), "reason": "z"}],
            },
        ),
        (
            "propose_queue",
            {
                "project": "falcone-workflow",
                "name": "X",
                "reason": "y",
                "items": [{"item": str(world.items[1])}],
            },
        ),
        (
            "propose_queue",
            {
                "project": "nothing",
                "name": "X",
                "reason": "y",
                "items": [{"item": str(world.items[1]), "reason": "z"}],
            },
        ),
    ):
        assert (await call(tool, **arguments)).startswith("Refused:"), tool
    # Work the supervisor starts is held back by the budgets like a queue's next item.
    world.budgets.save("platform", None, at_once=None, per_day=1)
    world.runs.start_delivery(world.items[3], toolchain={})
    said = await call("start_item", item=str(world.items[1]), reason="You asked.")
    assert said == (
        "Refused: Nothing more can start right now. The budget of the platform allows 1 in "
        "a day; that is used up."
    )


def _post(client: TestClient, path: str, **fields: Any) -> httpx.Response:
    csrf = client.get("/api/dashboard/session").json()["csrf_token"]
    return client.post(f"/api/dashboard{path}", json={"csrf_token": csrf, **fields})


def test_queues_and_budgets_in_the_console(tmp_path: Path) -> None:
    client = _console(tmp_path, None)
    repository = client.app.state.dashboard_repository  # type: ignore[attr-defined]
    repository.sections.sync_core()
    with repository.sessions.begin() as session:
        project = session.query(ProjectRecord).filter_by(name="falcone").one()
        issues = session.query(ProjectSection).filter_by(project_id=project.id, key="issues").one()
        issues.flow_key = "deliver-issue"
        items = []
        for number, title in enumerate(TITLES[:3], start=11):
            item = BacklogItem(
                project_id=project.id,
                section_id=issues.id,
                title=title,
                source="github",
                state="accepted",
                repository="gntik-ai/falcone",
                issue_number=number,
                issue_labels=[],
            )
            session.add(item)
            items.append(item)
        session.flush()
        ids = [str(item.id) for item in items]
        project_id = str(project.id)
    assert client.get("/api/dashboard/queues").json() == {"items": [], "total": 0}
    created = _post(client, "/projects/falcone/queues", name="Next", items=ids[:2])
    assert created.status_code == 201, created.text
    queue = created.json()
    assert queue["state"] == "draft" and len(queue["entries"]) == 2
    path = f"/queues/{queue['id']}"
    assert _post(client, "/projects/falcone/queues", name="Next", items=[]).status_code == 422
    assert _post(client, "/projects/falcone/queues", name="Dup", items=ids[:1]).status_code == 409
    assert _post(client, "/projects/nothing/queues", name="X", items=ids[2:]).status_code == 404
    assert _post(client, f"{path}/add", items=ids[2:]).json()["counts"]["total"] == 3
    ordered = _post(client, f"{path}/order", items=[ids[2], ids[0], ids[1]])
    assert [entry["title"] for entry in ordered.json()["entries"]] == [
        "Slow search",
        "Expired cookie",
        "Export to CSV",
    ]
    assert _post(client, f"{path}/order", items=ids[:1]).status_code == 409
    assert _post(client, f"{path}/update", name="Soon", at_once=2).json()["at_once"] == 2
    assert _post(client, f"{path}/update", at_once=9).status_code == 422
    removed = _post(client, f"{path}/items/{ids[1]}/remove")
    assert removed.status_code == 200 and removed.json()["counts"]["total"] == 2
    assert _post(client, f"{path}/items/{ids[1]}/remove").status_code == 404
    # The backlog says which queue an item is in.
    listed = client.get("/api/dashboard/items", params={"project": "falcone"}).json()["items"]
    by_title = {item["title"]: item for item in listed}
    assert by_title["Slow search"]["queue"] == {"id": queue["id"], "name": "Soon", "state": "draft"}
    assert by_title["Export to CSV"]["queue"] is None
    assert _post(client, f"{path}/pause").status_code == 409
    assert _post(client, f"{path}/start").json()["state"] == "running"
    assert _post(client, f"{path}/discard").status_code == 409
    assert _post(client, f"{path}/pause").json()["state"] == "paused"
    assert client.get(f"/api/dashboard{path}").json()["name"] == "Soon"
    assert client.get(f"/api/dashboard/queues/{uuid4()}").status_code == 404
    assert client.get("/api/dashboard/queues", params={"project": "falcone"}).json()["total"] == 1
    assert _post(client, f"{path}/discard").status_code == 200
    assert client.get("/api/dashboard/queues").json()["total"] == 0
    # Budgets.
    budgets = client.get("/api/dashboard/budgets").json()
    assert budgets["platform"]["at_once"] is None and budgets["accounts"] == []
    saved = _post(client, "/budgets", scope="project", scope_id=project_id, at_once=2, per_day=10)
    assert saved.status_code == 200 and saved.json()["at_once"] == 2
    assert _post(client, "/budgets", scope="platform", per_day=0).status_code == 422
    assert _post(client, "/budgets", scope="team").status_code == 422
    assert (
        _post(client, "/budgets", scope="project", scope_id=str(uuid4()), at_once=1).status_code
        == 404
    )
    row = next(
        r for r in client.get("/api/dashboard/budgets").json()["projects"] if r["name"] == "falcone"
    )
    assert (row["at_once"], row["per_day"]) == (2, 10)
    client.cookies.clear()
    assert client.get("/api/dashboard/queues").status_code == 401
    assert client.get("/api/dashboard/budgets").status_code == 401
