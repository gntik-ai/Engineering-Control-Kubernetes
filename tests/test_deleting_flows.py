"""Deleting a flow: one made here goes with its versions, one of the platform's own is
kept out until it is put back, and nothing is left that would follow a flow that is gone."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
)
from engineering_control.dashboard.project_sections import SectionRecords
from engineering_control.db.models import (
    Evaluation,
    EvaluationCase,
    FlowVersion,
    ProjectRecord,
    ProjectSection,
    RemovedFlow,
    SandboxRun,
)
from engineering_control.engine import library
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.records import FlowRecords, in_force
from engineering_control.engine.task_library import TaskLibrary
from tests.test_parts import _caller, _in_force, _loop
from tests.test_sandbox_delivery import world  # noqa: F401 - a fixture

PROJECT = "falcone-workflow"


def _copy(of: str, key: str, name: str) -> dict[str, Any]:
    return {**copy.deepcopy(BUILTIN[of].as_dict()), "key": key, "name": name}


def _flow_of(world: Any, section: str) -> str | None:  # noqa: F811
    with world.sessions() as session:
        project = session.query(ProjectRecord).filter_by(name=PROJECT).one()
        return (
            session.query(ProjectSection).filter_by(project_id=project.id, key=section).one()
        ).flow_key


def test_a_flow_made_here_is_deleted_with_all_that_is_its_own(world: Any) -> None:  # noqa: F811
    flows = FlowRecords(world.sessions)
    sections = SectionRecords(world.sessions)
    _in_force(flows, _copy("deliver-issue", "careful-delivery", "Careful delivery"))
    flows.save_draft("careful-delivery", _copy("deliver-issue", "careful-delivery", "More care"))
    sections.assign(PROJECT, "issues", "careful-delivery")
    with world.sessions.begin() as session:
        tried = Evaluation(
            flow_key="careful-delivery",
            candidate_version=2,
            baseline_version=1,
            state="finished",
            document={},
            sha256="0" * 64,
        )
        session.add(tried)
        session.flush()
        session.add(EvaluationCase(evaluation_id=tried.id, arm="candidate", subject={}))

    gone = flows.remove("careful-delivery")
    assert (gone["name"], gone["builtin"], gone["versions"], gone["evaluations"]) == (
        "More care",
        False,
        2,
        1,
    )
    # The section that had it is said, and is left without a flow.
    assert gone["sections"] == [{"project": PROJECT, "section": "issues", "section_name": "Issues"}]
    assert _flow_of(world, "issues") is None
    assert "careful-delivery" not in flows.keys() and not flows.exists("careful-delivery")
    assert flows.removed() == []
    with world.sessions() as session:
        assert (
            session.scalar(select(FlowVersion.id).where(FlowVersion.flow_key == gone["key"]))
            is None
        )
        assert session.scalars(select(Evaluation)).all() == []
        assert session.scalars(select(EvaluationCase)).all() == []
    with pytest.raises(ProjectNotFound):
        flows.remove("careful-delivery")
    with pytest.raises(ProjectNotFound):
        flows.remove("never-was")
    # The key is free again.
    assert flows.create(_copy("deliver-issue", "careful-delivery", "Again"))["version"] == 1


def test_a_flow_that_something_still_follows_is_not_deleted(world: Any) -> None:  # noqa: F811
    flows = FlowRecords(world.sessions)
    # Another flow runs it as a part.
    _in_force(flows, _loop())
    _in_force(flows, {**_caller(), "key": "with-the-loop", "name": "With the loop"})
    with pytest.raises(ProjectConflict, match="With the loop runs it as a part"):
        flows.remove("loop")
    flows.remove("with-the-loop")
    assert flows.remove("loop")["name"] == "The loop"

    # A run that follows it has not ended.
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    with pytest.raises(ProjectConflict, match="A run that follows it has not ended"):
        flows.remove("deliver-issue")
    assert flows.exists("deliver-issue")
    with world.sessions.begin() as session:
        record = session.get(SandboxRun, UUID(run["id"]))
        record.state, record.finished_at = "cancelled", datetime.now(UTC)
    assert flows.remove("deliver-issue")["builtin"] is True


def test_a_flow_of_the_platforms_own_is_kept_out_until_it_is_put_back(world: Any) -> None:  # noqa: F811
    flows = FlowRecords(world.sessions)
    sections = SectionRecords(world.sessions)
    sections.assign(PROJECT, "issues", "deliver-issue")
    # A version made of it here goes with it.
    flows.save_draft("deliver-issue", {**BUILTIN["deliver-issue"].as_dict(), "summary": "Mine."})
    flows.publish("deliver-issue")
    assert flows.version_in_force("deliver-issue") == 1

    gone = flows.remove("deliver-issue")
    assert (gone["name"], gone["builtin"], gone["versions"]) == ("Deliver issue", True, 1)
    assert [use["section"] for use in gone["sections"]] == ["issues"]
    assert "deliver-issue" not in flows.keys() and not flows.exists("deliver-issue")
    assert [row["key"] for row in flows.summary()].count("deliver-issue") == 0
    assert flows.removed() == [
        {
            "key": "deliver-issue",
            "name": "Deliver issue",
            "summary": BUILTIN["deliver-issue"].summary,
            "runs": True,
        }
    ]
    with world.sessions() as session:
        assert in_force(session, "deliver-issue") is None
        assert library.document_in_force(session, "deliver-issue") is None
        assert library.describe(session, "deliver-issue") is None
        assert library.plan_for_item(session, "deliver-issue", produced=False) is None
    # Nothing follows it: no version, no draft, no section, no new flow under its key.
    for refused in (
        lambda: flows.version_in_force("deliver-issue"),
        lambda: flows.versions("deliver-issue"),
        lambda: flows.save_draft("deliver-issue", BUILTIN["deliver-issue"].as_dict()),
        lambda: flows.remove("deliver-issue"),
    ):
        with pytest.raises(ProjectNotFound):
            refused()
    with pytest.raises(ProjectInvalid, match="was deleted: put it back first"):
        sections.assign(PROJECT, "issues", "deliver-issue")
    with pytest.raises(ProjectConflict, match="was deleted: put that one back"):
        flows.create(BUILTIN["deliver-issue"].as_dict())
    # The item's section has no flow now, so nothing starts for it.
    with pytest.raises((ProjectConflict, ProjectInvalid)):
        world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    # A run that followed it still shows what it followed.
    assert flows.document("deliver-issue", 0) is BUILTIN["deliver-issue"]

    back = flows.put_back("deliver-issue")
    assert (back["key"], back["name"]) == ("deliver-issue", "Deliver issue")
    assert flows.removed() == [] and flows.keys()[0] == "deliver-issue"
    # As the platform made it: the version made here did not come back.
    assert flows.version_in_force("deliver-issue") == 0
    assert [row["version"] for row in flows.versions("deliver-issue")] == [0]
    sections.assign(PROJECT, "issues", "deliver-issue")
    with pytest.raises(ProjectNotFound):
        flows.put_back("deliver-issue")
    with pytest.raises(ProjectNotFound):
        flows.put_back("never-was")


def test_a_name_of_the_library_is_deleted_and_taken_by_a_flow_made_of_it(world: Any) -> None:  # noqa: F811
    flows = FlowRecords(world.sessions)
    sections = SectionRecords(world.sessions)
    sections.assign(PROJECT, "issues", "recover-deployment")
    gone = flows.remove("recover-deployment")
    assert (gone["name"], gone["builtin"], gone["versions"]) == ("Recover deployment", True, 0)
    assert _flow_of(world, "issues") is None
    assert [(row["key"], row["runs"]) for row in flows.removed()] == [("recover-deployment", False)]
    with world.sessions() as session:
        assert library.describe(session, "recover-deployment") is None
    with pytest.raises(ProjectInvalid, match="was deleted"):
        sections.assign(PROJECT, "issues", "recover-deployment")
    # A flow made under that name takes it: it is no deleted name any more.
    flows.create(_copy("deliver-issue", "recover-deployment", "Recover deployment"))
    assert flows.removed() == [] and "recover-deployment" in flows.keys()
    with world.sessions() as session:
        assert session.scalars(select(RemovedFlow)).all() == []


def test_a_task_of_the_platforms_own_is_deleted_and_put_back(world: Any) -> None:  # noqa: F811
    tasks = TaskLibrary(world.sessions, FlowRecords(world.sessions))

    def deleted() -> set[str]:
        return {kind["key"] for kind in tasks.listing()["kinds"] if kind["hidden"]}

    retired = {key for key, task in TASKS.items() if task.retired}
    # What is not offered any more counts as deleted from the start, and stays so.
    assert deleted() == retired
    with pytest.raises(ProjectInvalid, match="took its place"):
        tasks.hide(next(iter(retired)), hidden=False)
    assert tasks.hide("workspace.clone", hidden=True)["hidden"] is True
    assert deleted() == retired | {"workspace.clone"}
    # A flow that has it keeps it, and says so.
    [kind] = [kind for kind in tasks.listing()["kinds"] if kind["key"] == "workspace.clone"]
    assert "deliver-issue" in {use["flow"] for use in kind["used_by"]}
    assert tasks.hide("workspace.clone", hidden=False)["hidden"] is False
    assert deleted() == retired


def test_the_console_deletes_flows_and_puts_the_platforms_own_back(world: Any) -> None:  # noqa: F811
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

        def listed() -> dict[str, Any]:
            return client.get("/api/dashboard/flows").json()

        assert listed()["removed"] == []
        assert post("/flows", key="my-flow", name="My flow").status_code == 201
        gone = post("/flows/my-flow/remove")
        assert gone.status_code == 200 and gone.json()["builtin"] is False
        assert "my-flow" not in {flow["key"] for flow in listed()["items"]}
        assert client.get("/api/dashboard/flows/my-flow").status_code == 404
        assert post("/flows/my-flow/remove").status_code == 404

        # One of the platform's own, and a name of the library that runs nothing.
        assert post("/flows/ux-audit/remove").json()["name"] == "UX audit"
        assert post("/flows/change-request/remove").json()["builtin"] is True
        after = listed()
        assert {"ux-audit", "change-request"} & {flow["key"] for flow in after["items"]} == set()
        assert [(flow["key"], flow["runs"]) for flow in after["removed"]] == [
            ("change-request", False),
            ("ux-audit", True),
        ]
        assert client.get("/api/dashboard/flows/ux-audit").status_code == 404
        refused = post("/projects/falcone-workflow/sections/issues/flow", flow="ux-audit")
        assert refused.status_code == 422 and "was deleted" in refused.json()["detail"]

        assert post("/flows/ux-audit/put-back").status_code == 200
        assert post("/flows/ux-audit/put-back").status_code == 404
        assert "ux-audit" in {flow["key"] for flow in listed()["items"]}
        assert [flow["key"] for flow in listed()["removed"]] == ["change-request"]
        # Nobody deletes a flow without a session.
        client.cookies.clear()
        assert client.post("/api/dashboard/flows/ux-audit/remove", json={}).status_code in (
            401,
            403,
        )
