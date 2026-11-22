from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.models import CommandProfile, ProjectRecord
from engineering_control.engine.builtin import BUILTIN
from engineering_control.sandbox import deliver
from engineering_control.sandbox.runs import commands_of_project
from tests.test_dashboard_web import PASSWORD, _authenticated_client


def post(client: TestClient, path: str, **body: Any) -> Any:
    token = client.get("/api/dashboard/session").json()["csrf_token"]
    return client.post("/api/dashboard/" + path, json={"csrf_token": token, **body})


def test_global_catalog_tracks_published_draft_and_private_sources(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        key = "deliver-issue"
        draft = BUILTIN[key].as_dict()
        draft["prompts"] = {"implementer": {"role": "Implement this issue."}}
        clone = next(node for node in draft["tasks"] if node["task"] == "workspace.install")
        clone["with"]["commands"] = ["pnpm install"]
        repo.flows.save_draft(key, draft)
        private = post(
            client,
            "flows?scope_project=falcone",
            key="local-catalog",
            name="Private flow",
            copy_of=key,
        ).json()["flow"]
        local = repo.flows.document(private, 1)
        assert local
        raw = local.as_dict()
        next(n for n in raw["tasks"] if n["task"] == "workspace.install")["with"]["commands"] = [
            "pytest private-check"
        ]
        repo.flows.save_draft(private, raw)
        repo.flows.save_task(
            name="Shared checks", task="code.run", params={"commands": ["pytest -q"]}, wording={}
        )

        inventory = client.get("/api/dashboard/agents").json()
        maker = next(x for x in inventory["roles"] if x["prompt"]["name"] == "implementer")
        assert {
            (r["draft"], r["custom_wording"]) for r in maker["used_by"] if r["flow"] == key
        } == {(True, True), (False, False)}
        assert all(r["flow"] != private for r in maker["used_by"])
        assert client.get("/api/dashboard/agents?scope_project=falcone").json()["roles"] == []
        catalog = client.get("/api/dashboard/command-profiles").json()
        assert catalog["items"] == []
        assert {
            (r["name"], r["draft"], tuple(r["commands"])) for r in catalog["task_commands"]
        } == {("Deliver issue", True, ("pnpm install",)), ("Shared checks", False, ("pytest -q",))}
        assert (
            "manifests"
            not in client.get("/api/dashboard/command-profiles?scope_project=falcone").json()
        )
        with repo.sessions() as db:
            assert db.scalar(select(CommandProfile.id)) is None
    finally:
        client.close()
        repo.engine.dispose()


def test_shared_manifest_profile_keeps_execution_metadata_and_selection(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        add_project(client)
        base = "/api/dashboard/command-profiles"

        def manifest() -> Any:
            return next(
                m for m in client.get(base).json()["manifests"] if m["project"] == "falcone"
            )

        catalog = manifest()
        chain = deliver.toolchain_for("falcone")
        assert catalog["using"] == "manifest"
        fields = dict(
            name="CI from Falcone",
            current_password=PASSWORD,
            manifest="falcone",
            install=[c["line"] for c in catalog["install"]],
            tests=[c["line"] for c in catalog["tests"]],
        )
        assert fields["install"] == [deliver.line_of(c) for c in chain.prepare]
        assert fields["tests"] == [deliver.line_of(c) for c in chain.tests]
        assert post(client, "command-profiles?scope_project=other", **fields).status_code == 422
        created = post(client, "command-profiles", **fields)
        assert created.status_code == 201
        profile = created.json()["id"]
        path = "projects/falcone/command-profile"
        assert manifest()["using"] == "manifest"
        assert post(client, path, profile_id=profile, current_password=PASSWORD).status_code == 200
        assert manifest()["using"] == "profile"

        def effective() -> dict[str, Any]:
            with repo.sessions() as db:
                project = db.scalar(select(ProjectRecord).where(ProjectRecord.name == "falcone"))
                assert project
                return commands_of_project(project, chain.as_dict())

        assert effective() == chain.as_dict()
        changed = {**fields, "tests": ["pytest new-tests", fields["tests"][0]]}
        del changed["manifest"]
        assert post(client, "command-profiles/" + profile + "/update", **changed).status_code == 200
        actual = effective()
        assert actual["prepare"] == chain.as_dict()["prepare"]
        assert actual["tests"] == [
            deliver.command_of("pytest new-tests").as_dict(),
            chain.tests[0].as_dict(),
        ]
        assert post(client, path, profile_id=None, current_password=PASSWORD).status_code == 200
        assert manifest()["using"] == "manifest" and effective() == chain.as_dict()
    finally:
        client.close()
        repo.engine.dispose()


def add_project(client: TestClient) -> None:
    assert post(client, "projects", name="other", repository="example/other").status_code == 201


def exercise_flows(client: TestClient, repo: DashboardRepository) -> None:
    add_project(client)
    body = {"key": "private-delivery", "name": "Delivery", "copy_of": "deliver-issue"}
    local = post(client, "flows?scope_project=falcone", **body)
    assert local.status_code == 201, local.text
    key = local.json()["flow"]
    second = post(client, "flows?scope_project=other", **body)
    assert second.status_code == 201 and second.json()["flow"] != key
    assert post(client, "flows?scope_project=falcone", **body).status_code == 409
    assert key not in {f["key"] for f in client.get("/api/dashboard/flows").json()["items"]}
    assert client.get("/api/dashboard/flows/" + key).status_code == 404
    assert (
        client.get("/console/flow?key=" + key, follow_redirects=False).headers["location"]
        == "/console/workspace/falcone/automation/flow?key=" + key
    )
    assert post(client, "flows/" + key + "/publish?scope_project=other").status_code == 404
    assert (
        post(
            client, "flows/" + key + "/publish?scope_project=falcone", current_password=PASSWORD
        ).status_code
        == 200
    )
    assert post(client, "projects/other/sections/issues/flow", flow=key).status_code == 422
    assignment = post(client, "projects/falcone/sections/issues/flow", flow=key, version=1)
    assert assignment.status_code == 200 and assignment.json()["flow"]["version"] == 1
    document = repo.flows.document(key, 1)
    assert document
    assert post(client, "flows", document=document.as_dict()).status_code == 422
    assert (
        post(
            client, "flows?scope_project=other", key="foreign-copy", name="Copy", copy_of=key
        ).status_code
        == 404
    )
    assert (
        post(client, "flows?scope_project=falcone", document=document.as_dict()).json()["flow"]
        == key
    )
    repo.flows.save_draft(key, {**document.as_dict(), "summary": "Second version"})
    repo.flows.publish(key)
    item = repo.backlog.add("falcone", title="Check the pinned workflow", section="issues")
    started = repo.runs.start_item(UUID(item["id"]), toolchain={})
    assert repo.runs.get(UUID(started["id"]))["data"]["flow"]["version"] == 1
    assert (
        next(s for s in repo.sections.of_project("falcone") if s["key"] == "issues")["flow"][
            "version"
        ]
        == 1
    )
    assert (
        post(client, "projects/falcone/sections/issues/flow", flow=key).json()["flow"]["version"]
        == 2
    )
    assert (
        post(client, "projects/falcone/sections/issues/flow", flow=key, version=3).status_code
        == 422
    )
    agent = dict(name="Maintainer", instructions="Maintain the test suite.")
    private = post(client, "agents?scope_project=falcone", **agent)
    assert private.status_code == 201
    assert post(client, "agents", **agent).status_code == 201
    agent_id = private.json()["id"]
    assert agent_id not in {
        r["id"]
        for r in client.get("/api/dashboard/flows/catalogue?scope_project=other").json()["agents"]
    }
    assert post(client, f"agents/{agent_id}/remove?scope_project=other").status_code == 404


def test_automation_workspaces(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        exercise_flows(client, repo)
        global_op = post(
            client, "operations", name="Release audit", description="Review the release."
        )
        assert global_op.status_code == 201, global_op.text
        key = global_op.json()["key"]
        post(
            client,
            f"projects/falcone/sections/{key}/update",
            description="Review our release notes.",
        )
        assert (
            post(
                client,
                f"operations/{key}/update",
                name="Release audit",
                description="Review the release and changelog.",
            ).status_code
            == 200
        )

        def operation(name: str) -> dict[str, Any]:
            return next(s for s in repo.sections.of_project(name) if s["key"] == key)

        assert operation("falcone")["description"] == "Review our release notes."
        assert operation("other")["description"] == "Review the release and changelog."
        assert (
            post(client, "projects/other/sections/" + key + "/flow", flow="audit-code").status_code
            == 200
        )
        assert post(client, f"operations/{key}/remove").status_code == 409

        fields = dict(name="Unit tests", install=[], tests=["pytest -q"], current_password=PASSWORD)
        shared = post(client, "command-profiles", **fields)
        assert shared.status_code == 201, shared.text
        profile = shared.json()["id"]
        private = post(client, "command-profiles?scope_project=falcone", **fields).json()["id"]
        assert (
            post(
                client,
                "projects/other/command-profile",
                profile_id=private,
                current_password=PASSWORD,
            ).status_code
            == 404
        )
        assert (
            post(
                client,
                "projects/other/command-profile",
                profile_id=profile,
                current_password=PASSWORD,
            ).status_code
            == 200
        )
        assert repo.projects.commands("other")["profile"]["id"] == profile
        with repo.sessions() as db:
            record = db.scalar(select(ProjectRecord).where(ProjectRecord.name == "other"))
            assert record is not None and commands_of_project(record, {})["tests"]
        assert (
            post(
                client, "command-profiles/" + profile + "/remove", current_password=PASSWORD
            ).status_code
            == 409
        )
        assert repo.command_profiles.choose("other", None)["profile"] is None
        assert repo.command_profiles.remove(None, UUID(profile))
    finally:
        client.__exit__(None, None, None)
        repo.engine.dispose()
