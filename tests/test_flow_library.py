"""The library of flows: flows made here beside the platform's own, brought in and taken
out as documents, assigned to sections, and run by the same engine."""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

import httpx
import pytest

from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
)
from engineering_control.dashboard.project_sections import SectionRecords
from engineering_control.db.models import BacklogItem, ProjectRecord, ProjectSection
from engineering_control.engine import library
from engineering_control.engine.builtin import BUILTIN, kind_of
from engineering_control.engine.document import parse
from engineering_control.engine.evaluations import Evaluations
from engineering_control.engine.records import FlowRecords
from engineering_control.sandbox.launcher import Launcher
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    BUNDLE,
    REPOSITORY,
    _implemented,
    _log,
    _prepared,
    _ran,
    _reviewed,
    _verified,
    world,
)
from tests.test_sandbox_runs import SANDBOXES


def _copy(of: str, key: str, name: str, **changes: Any) -> dict[str, Any]:
    raw = copy.deepcopy(BUILTIN[of].as_dict())
    return {**raw, "key": key, "name": name, **changes}


def _section(world: Any, key: str, flow: str | None) -> None:  # noqa: F811
    with world.sessions.begin() as session:
        project = session.query(ProjectRecord).filter_by(name="falcone-workflow").one()
        section = session.query(ProjectSection).filter_by(project_id=project.id, key=key).one()
        section.flow_key = flow


def test_a_document_says_what_its_items_do_and_its_drawing_is_not_what_it_says() -> None:
    audit = BUILTIN["audit-code"]
    assert (audit.items, audit.action) == ("request", "Start an audit")
    assert BUILTIN["deliver-issue"].as_dict().keys() == {
        "format",
        "key",
        "name",
        "summary",
        "start",
        "tasks",
        "prompts",
        "budgets",
    }, "a flow that says nothing about its items reads as it always did"
    drawn = {**audit.as_dict(), "layout": {"prepare": {"x": 10.4, "y": -3}, "ghost": {"x": 1}}}
    read = parse(drawn)
    assert read.layout == {"prepare": {"x": 10, "y": -3}}, "only the flow's own tasks are drawn"
    assert read.sha256() == audit.sha256() and read.as_dict()["layout"]
    assert library.traits(audit) == {
        "writes": False,
        "produces": True,
        "publishes": False,
        "browses": False,
    }
    assert library.section_action(audit) == "Start an audit"
    assert library.section_action(BUILTIN["deliver-issue"]) is None
    assert kind_of("audit-code") == "audit_code" and kind_of("anything-else") == "flow"


def test_a_flow_is_made_here_as_a_draft_and_is_not_there_until_it_is_in_force(world: Any) -> None:  # noqa: F811
    flows = FlowRecords(world.sessions)
    sections = SectionRecords(world.sessions)
    made = flows.create(_copy("deliver-issue", "careful-delivery", "Careful delivery"))
    assert (made["version"], made["state"], made["problems"]) == (1, "draft", [])
    with pytest.raises(ProjectConflict, match="a flow careful-delivery already"):
        flows.create(_copy("deliver-issue", "careful-delivery", "Again"))
    with pytest.raises(ProjectConflict, match="a flow audit-code already"):
        flows.create(BUILTIN["audit-code"].as_dict())
    with pytest.raises(ProjectInvalid, match="lower-case"):
        flows.create(_copy("deliver-issue", "Not A Key", "No"))
    assert (
        flows.keys()[-1] == "careful-delivery"
        and flows.version_in_force("careful-delivery") is None
    )
    row = next(row for row in flows.summary() if row["key"] == "careful-delivery")
    assert (row["name"], row["version"], row["draft"], row["builtin"]) == (
        "Careful delivery",
        None,
        1,
        False,
    )
    # Nothing runs by a flow that was never put in force: a section cannot have it.
    with pytest.raises(ProjectInvalid, match="never put in force"):
        sections.assign("falcone-workflow", "issues", "careful-delivery")
    with world.sessions() as session:
        assert library.describe(session, "careful-delivery") is None
        assert library.plan_for_item(session, "careful-delivery", produced=False) is None
        # A name of the library without a document can be assigned and runs nothing.
        assert library.describe(session, "recover-deployment")["runs_items"] is False
    # A draft that is dropped before it was ever in force takes the flow with it.
    flows.discard_draft("careful-delivery")
    assert "careful-delivery" not in flows.keys()
    with pytest.raises(ProjectNotFound):
        flows.version_in_force("careful-delivery")
    with pytest.raises(ProjectNotFound):
        flows.save_draft("careful-delivery", _copy("deliver-issue", "careful-delivery", "Back"))


@pytest.mark.asyncio
async def test_a_flow_made_here_runs_for_its_section_like_the_platforms_own(world: Any) -> None:  # noqa: F811
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    flows = FlowRecords(world.sessions)
    sections = SectionRecords(world.sessions)
    raw = _copy("deliver-issue", "careful-delivery", "Careful delivery")
    raw["prompts"] = {"implementer": {"role": "You implement {{subject}} with great care."}}
    raw["layout"] = {"prepare": {"x": 0, "y": 0}}
    flows.create(raw)
    flows.publish("careful-delivery", note="First")
    # Drawing it another way is a version; saying the same in the same place is not.
    flows.save_draft("careful-delivery", raw)
    with pytest.raises(ProjectConflict, match="says the same"):
        flows.publish("careful-delivery")
    flows.save_draft("careful-delivery", {**raw, "layout": {"prepare": {"x": 40, "y": 0}}})
    assert flows.publish("careful-delivery")["version"] == 2

    shown = sections.assign("falcone-workflow", "issues", "careful-delivery")
    assert shown["flow"] == {
        "key": "careful-delivery",
        "name": "Careful delivery",
        "summary": BUILTIN["deliver-issue"].summary,
        "runs_items": True,
        "section_action": None,
        "version": 2,
        "needs_application": False,
    }
    assert "Flow: Careful delivery." in sections.brief("falcone-workflow")
    assert sections.using("careful-delivery") == [
        {
            "project": "falcone-workflow",
            "project_name": "Falcone workflow",
            "section": "issues",
            "section_name": "Issues",
        }
    ]

    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    assert run["kind"] == "flow" and run["title"].startswith("#11 Signing in fails")
    assert run["data"]["flow"]["key"] == "careful-delivery" and run["data"]["flow"]["version"] == 2
    assert run["data"]["branch"].startswith("engineering-control/issue-11-")
    await world.step("prepare", _prepared())
    await world.launcher.tick()
    assert world.job()["commands"][0]["stdin"].startswith(
        f"You implement issue #11 of {REPOSITORY} with great care."
    )
    world.fake.finish(_implemented())
    await world.launcher.tick()
    await world.step("review", _reviewed())
    await world.step("verify", _verified())
    await world.step("bundle", _log([_ran("bundle", "2 files")], artifact=BUNDLE))
    waiting = world.run(run["id"])
    assert (waiting["state"], waiting["outcome"]) == ("waiting", "ready")
    world.runs.decide(UUID(run["id"]), "publish")
    await world.launcher.tick()
    done = world.run(run["id"])
    assert (done["state"], done["outcome"]) == ("succeeded", "published")
    assert world.hub.created["pull"]["head"] == done["data"]["branch"]

    # Its runs are what its evaluation lab can go through again, and only its own.
    lab = Evaluations(world.sessions, world.runs)
    assert [row["run_id"] for row in lab.candidates("careful-delivery")] == [run["id"]]
    assert lab.candidates("deliver-issue") == []
    assert world.runs.usage_totals()["flows"][0]["key"] == "careful-delivery"
    await world.github.aclose()


@pytest.mark.asyncio
async def test_a_flow_that_produces_items_runs_for_its_section_and_leaves_delivery_to_delivery(
    world: Any,  # noqa: F811
) -> None:
    from tests.test_sandbox_producers import AUDITED, _by_claude, _checked_out, _codex, _round

    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    backlog = BacklogItems(world.sessions)
    world.launcher = Launcher(
        world.runs, world.launcher.cluster, SANDBOXES, world.github, backlog=backlog
    )
    flows = FlowRecords(world.sessions)
    sections = SectionRecords(world.sessions)
    raw = _copy("audit-code", "quick-audit", "Quick audit", action="", items="deliver")
    next(task for task in raw["tasks"] if task["id"] == "verify")["with"]["rounds"] = 1
    flows.create(raw)
    flows.publish("quick-audit")
    shown = sections.assign("falcone-workflow", "code-audit", "quick-audit")
    assert shown["flow"]["section_action"] == "Run Quick audit", (
        "the flow's name, when it gives none"
    )

    run = world.runs.start_for_section(
        "falcone-workflow", "code-audit", scope="The API", toolchain={}
    )
    assert (run["kind"], run["title"]) == ("flow", "Quick audit: The API")
    with pytest.raises(ProjectConflict, match="Quick audit is already under way"):
        world.runs.start_for_section("falcone-workflow", "code-audit", scope="", toolchain={})
    await world.step("prepare", _checked_out())
    await world.step("audit", _codex({**AUDITED, "findings": AUDITED["findings"][:1]}))
    await world.step("verify", _by_claude(_round((1, True, True, True, True, None))))
    await world.launcher.tick()
    ended = world.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("succeeded", "findings")
    (finding,) = ended["data"]["result"]["findings"]

    # What it found is delivered by the delivery flow; so is anything else of the section,
    # since this flow leaves its section's items to delivery.
    found = world.runs.start_item(UUID(finding["item_id"]), toolchain=world.toolchain)
    assert (found["kind"], found["data"]["flow"]["key"]) == ("deliver_issue", "deliver-issue")
    with world.sessions.begin() as session:
        item = session.get(BacklogItem, world.item_id)
        code_audit = (
            session.query(ProjectSection)
            .filter_by(project_id=item.project_id, key="code-audit")
            .one()
        )
        item.section_id = code_audit.id
    asked = world.runs.start_item(world.item_id, toolchain=world.toolchain)
    assert asked["kind"] == "deliver_issue"
    world.runs.request_cancel(UUID(asked["id"]))
    world.runs.request_cancel(UUID(found["id"]))
    await world.launcher.tick()

    # With the platform's own audit, the same item is a request: an audit of what it names.
    _section(world, "code-audit", "audit-code")
    asked = world.runs.start_item(world.item_id, toolchain=world.toolchain)
    assert (asked["kind"], asked["title"][:10]) == ("audit_code", "Audit: #11")
    await world.github.aclose()


@pytest.mark.asyncio
async def test_code_clean_proposes_what_can_be_taken_out_as_clean_up_items(world: Any) -> None:  # noqa: F811
    from tests.test_sandbox_producers import AUDITED, _by_claude, _checked_out, _codex, _round

    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    backlog = BacklogItems(world.sessions)
    world.launcher = Launcher(
        world.runs, world.launcher.cluster, SANDBOXES, world.github, backlog=backlog
    )
    sections = SectionRecords(world.sessions)
    shown = sections.assign("falcone-workflow", "code-clean", "code-clean")
    assert shown["flow"]["section_action"] == "Look for things to clean up"
    run = world.runs.start_for_section(
        "falcone-workflow", "code-clean", scope="The helpers", toolchain=world.toolchain
    )
    assert (run["kind"], run["title"]) == ("flow", "Code clean: The helpers")
    await world.launcher.tick()
    assert [c["name"] for c in world.job()["commands"]] == ["checkout", "history"]
    world.fake.finish(_checked_out())
    await world.launcher.tick()
    await world.launcher.tick()
    assert [c["name"] for c in world.job()["commands"]] == ["install 1"]
    world.fake.finish(_checked_out())
    await world.launcher.tick()
    await world.launcher.tick()
    asked = world.job()["commands"][0]["stdin"]
    assert asked.startswith(f"You look for what can be cleaned up in {REPOSITORY}")
    assert "Give it the lens maintainability" in asked and "maintainability." in asked
    # What the platform fixes is there as in any audit.
    assert "<scope>\nThe helpers\n</scope>" in asked and "material to judge" in asked
    unused = {
        **AUDITED["findings"][0],
        "title": "formatLegacyDate is never called",
        "lens": "maintainability",
        "severity": "low",
    }
    world.fake.finish(_codex({**AUDITED, "findings": [unused]}))
    await world.launcher.tick()
    await world.launcher.tick()
    assert "Independently check candidate clean-ups" in world.job()["commands"][0]["stdin"]
    world.fake.finish(_by_claude(_round((1, True, True, True, True, None))))
    await world.launcher.tick()
    await world.launcher.tick()
    ended = world.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("succeeded", "findings")
    assert [step["name"] for step in ended["steps"]] == ["prepare", "install", "audit", "verify"]
    (found,) = ended["data"]["result"]["findings"]
    item = backlog.get(UUID(found["item_id"]))
    assert item["facts"]["kind"] == "clean-up" and item["facts"]["lens"] == "maintainability"
    assert item["section"]["key"] == "code-clean" and item["state"] == "proposed"
    assert "Reproduced once, independently" in item["description"]
    # A clean-up is delivered like any item.
    delivery = world.runs.start_item(UUID(item["id"]), toolchain=world.toolchain)
    assert delivery["kind"] == "deliver_issue"
    await world.github.aclose()


def test_the_console_makes_copies_and_brings_flows_in(world: Any) -> None:  # noqa: F811
    from fastapi.testclient import TestClient

    from engineering_control.config import Settings
    from engineering_control.dashboard.app import create_app
    from engineering_control.dashboard.repository import DashboardRepository

    password = "correct horse battery staple"  # noqa: S105 - deterministic test fixture
    repository = DashboardRepository(str(world.sessions.kw["bind"].url))
    repository.initialize_password_sync(password)
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

        # A copy of a flow that exists, under a key and a name of its own.
        made = post(
            "/flows",
            key="careful-delivery",
            name="Careful delivery",
            summary="Deliver with more care.",
            copy_of="deliver-issue",
        )
        assert made.status_code == 201 and made.json()["state"] == "draft"
        assert made.json()["document"]["summary"] == "Deliver with more care."
        assert len(made.json()["document"]["tasks"]) == 10
        assert post("/flows", key="careful-delivery", name="Twice").status_code == 409
        assert post("/flows", key="nope", name="Nope", copy_of="nothing").status_code == 404
        # A flow with nothing in it yet: one task, to build on.
        empty = post("/flows", key="my-flow", name="My flow")
        assert [task["task"] for task in empty.json()["document"]["tasks"]] == ["workspace.clone"]

        detail = client.get("/api/dashboard/flows/careful-delivery").json()
        assert detail["in_force"] is None and detail["draft"]["version"] == 1
        assert detail["builtin"] is False and detail["used_by"] == []
        listed = {f["key"]: f for f in client.get("/api/dashboard/flows").json()["items"]}
        assert listed["careful-delivery"]["engine"]["version"] is None
        assert listed["careful-delivery"]["engine"]["draft"] == 1
        # It can be gone through on paper before it was ever in force.
        paper = post("/flows/careful-delivery/simulate")
        assert paper.json()["ends"]["how"] == "done"

        assert post("/flows/careful-delivery/publish", current_password=password).status_code == 200
        assigned = post("/projects/falcone-workflow/sections/issues/flow", flow="careful-delivery")
        assert assigned.status_code == 200 and assigned.json()["flow"]["name"] == "Careful delivery"
        detail = client.get("/api/dashboard/flows/careful-delivery").json()
        assert detail["in_force"]["version"] == 1
        assert [use["section"] for use in detail["used_by"]] == ["issues"]

        # What is in the editor is checked without being saved.
        broken = client.get("/api/dashboard/flows/deliver-issue").json()["in_force"]["document"]
        next(task for task in broken["tasks"] if task["id"] == "review")["with"] = {}
        checked = post("/flows/check", document=broken)
        assert checked.status_code == 200
        assert [p["rule"] for p in checked.json()["problems"]] == ["other_subscription"]
        assert post("/flows/check", document={"format": "x"}).status_code == 422

        # A task as it is set is kept under a name, with the wording of its prompt.
        saved = post(
            "/flows/saved-tasks",
            name="  Strict   reviewer ",
            task="agent.review",
            settings={"other_than": "implement", "minutes": 30, "unknown": 1},
            wording={"judging": "Be strict in {{repository}}."},
        )
        assert saved.status_code == 201 and saved.json()["name"] == "Strict reviewer"
        (kept,) = client.get("/api/dashboard/flows/catalogue").json()["saved"]
        assert kept["with"] == {"minutes": 30}, "whose work it judges is the flow's to say"
        assert kept["wording"] == {"judging": "Be strict in {{repository}}."}
        again = post("/flows/saved-tasks", name="Strict reviewer", task="agent.review")
        assert again.json()["id"] == kept["id"], "the same name replaces"
        assert (
            post(
                "/flows/saved-tasks",
                name="Bad",
                task="agent.review",
                wording={"judging": "{{secret}}"},
            ).status_code
            == 422
        )
        assert post("/flows/saved-tasks", name="A branch", task="control.when").status_code == 422
        assert post(f"/flows/saved-tasks/{kept['id']}/remove").status_code == 200
        assert client.get("/api/dashboard/flows/catalogue").json()["saved"] == []

        # A document brought in: a new flow when its key is new, else the draft of its flow.
        taken_out = client.get("/api/dashboard/flows/careful-delivery/versions/1").json()[
            "document"
        ]
        brought = post("/flows", document={**taken_out, "key": "brought-in", "name": "Brought in"})
        assert brought.status_code == 201 and brought.json()["flow"] == "brought-in"
        again = post("/flows", document={**taken_out, "budgets": {"rounds": 2}})
        assert again.status_code == 201 and again.json()["flow"] == "careful-delivery"
        assert again.json()["version"] == 2 and again.json()["state"] == "draft"
        assert post("/flows", document={"format": "something else"}).status_code == 422
