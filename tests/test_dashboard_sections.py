"""Sections: six core ones per project, the administrator's own, and the flow of each."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from engineering_control.dashboard.project_sections import CORE_SECTIONS, section_key
from engineering_control.flow_library import FLOWS
from tests.test_dashboard_web import PASSWORD, _authenticated_client

CORE_KEYS = [section.key for section in CORE_SECTIONS]


def _csrf(client: TestClient) -> str:
    return str(client.get("/api/dashboard/session").json()["csrf_token"])


def _post(client: TestClient, path: str, **body: Any) -> Any:
    return client.post(
        f"/api/dashboard/projects/{path}", json={"csrf_token": _csrf(client), **body}
    )


def _sections(client: TestClient, project: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/dashboard/projects/{project}")
    assert response.status_code == 200
    return list(response.json()["sections"])


def test_every_project_has_the_six_core_sections(tmp_path: Path) -> None:
    client, repository = _authenticated_client(tmp_path)
    try:
        assert CORE_KEYS == [
            "code-audit",
            "ui-audit",
            "ux-audit",
            "code-clean",
            "enhancement",
            "issues",
        ]
        falcone = client.get("/api/dashboard/projects/falcone").json()
        assert falcone["project"]["project"] == "falcone"
        assert falcone["project"]["source_repository"] == "gntik-ai/falcone"
        sections = falcone["sections"]
        assert [section["key"] for section in sections] == CORE_KEYS
        assert all(section["kind"] == "core" and section["description"] for section in sections)
        # Every core section comes with a whole routing guide, and no label is claimed twice.
        for section in sections:
            assert section["use_when"] and section["avoid_when"], section["key"]
            assert section["labels"] and section["examples"], section["key"]
        labels = [label for section in sections for label in section["labels"]]
        assert len(labels) == len(set(labels))
        assert "bug" in next(s for s in sections if s["key"] == "issues")["labels"]
        assert "enhancement" in next(s for s in sections if s["key"] == "enhancement")["labels"]
        # The work that already runs for the built-in projects is shown as its flow.
        flows = {section["key"]: (section["flow"] or {}).get("key") for section in sections}
        assert flows == {
            "code-audit": "audit-code",
            "ui-audit": None,
            "ux-audit": None,
            "code-clean": None,
            "enhancement": "discover-enhancements",
            "issues": "deliver-issue",
        }
        # A project made in the console has the same sections and no flow: none is guessed.
        created = client.post(
            "/api/dashboard/projects",
            json={"csrf_token": _csrf(client), "name": "docs", "repository": "gntik-ai/docs"},
        )
        assert created.status_code == 201
        docs = _sections(client, "docs")
        assert [section["key"] for section in docs] == CORE_KEYS
        assert all(section["flow"] is None for section in docs)
        assert repository.sections.sync_core() == 0  # nothing is added twice
        assert client.get("/api/dashboard/projects/absent").status_code == 404
        assert client.get("/api/dashboard/projects/Not%20A%20Name").status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_the_flow_library_and_assigning_a_flow(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        library = client.get("/api/dashboard/flows").json()["items"]
        assert [flow["key"] for flow in library] == list(FLOWS) + [
            flow["key"] for flow in library if (flow["engine"] or {}).get("review_action")
        ]
        assert {"deliver-issue", "audit-code", "discover-enhancements"} <= set(FLOWS)
        assert all(flow["name"] and flow["summary"] for flow in library)

        assigned = _post(client, "falcone/sections/code-clean/flow", flow="change-request")
        assert assigned.status_code == 200
        assert assigned.json()["flow"]["name"] == "Change request"
        assert _post(client, "falcone/sections/code-clean/flow", flow="made-up").status_code == 422
        cleared = _post(client, "falcone/sections/issues/flow", flow=None)
        assert cleared.status_code == 200 and cleared.json()["flow"] is None
        flows = {s["key"]: (s["flow"] or {}).get("key") for s in _sections(client, "falcone")}
        assert flows["code-clean"] == "change-request" and flows["issues"] is None
        assert _post(client, "falcone/sections/absent/flow", flow=None).status_code == 404
        assert _post(client, "absent/sections/issues/flow", flow=None).status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_sections_of_the_administrator(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        added = _post(
            client,
            "falcone/sections",
            name="  Release   notes ",
            description="What changed in a release, written for users.",
            use_when="A release is about to go out.",
            avoid_when="The change itself is still being made (use Enhancement).",
            labels=["Release Notes", "changelog", "changelog", "  "],
            examples=["Write the notes for version 2.4.", ""],
        )
        assert added.status_code == 201
        assert added.json() == {
            "key": "release-notes",
            "name": "Release notes",
            "description": "What changed in a release, written for users.",
            "use_when": "A release is about to go out.",
            "avoid_when": "The change itself is still being made (use Enhancement).",
            "labels": ["release notes", "changelog"],
            "examples": ["Write the notes for version 2.4."],
            "kind": "custom",
            "flow": None,
        }
        assert [s["key"] for s in _sections(client, "falcone")] == [*CORE_KEYS, "release-notes"]
        # Its own sections belong to one project only.
        assert [s["key"] for s in _sections(client, "musematic")] == CORE_KEYS

        # A section must say what belongs in it: that is how work is recognised.
        nameless = _post(client, "falcone/sections", name="Docs")
        assert nameless.status_code == 422 and "needs a description" in nameless.json()["detail"]
        for name in ("release notes", "RELEASE NOTES", "Issues", "issues"):
            duplicate = _post(client, "falcone/sections", name=name, description="x")
            assert duplicate.status_code == 409, name
        for name in ("!!!", " ", "x" * 81):
            assert _post(client, "falcone/sections", name=name, description="x").status_code == 422
        for field, value in (
            ("description", "x" * 501),
            ("use_when", "x" * 601),
            ("avoid_when", "x" * 601),
            ("labels", ["x" * 41]),
            ("labels", ["a,b"]),
            ("labels", [f"label-{n}" for n in range(13)]),
            ("examples", ["x" * 201]),
            ("examples", [f"Example {n}." for n in range(7)]),
        ):
            body = {"name": "Docs", "description": "Documentation.", field: value}
            assert _post(client, "falcone/sections", **body).status_code == 422, field
        # A label sends work to one section only.
        taken = _post(
            client, "falcone/sections", name="Docs", description="Documentation.", labels=["BUG"]
        )
        assert taken.status_code == 409
        assert taken.json()["detail"] == "The label bug already belongs to the section Issues"

        renamed = _post(client, "falcone/sections/release-notes/update", name="Changelog")
        assert renamed.status_code == 200
        assert (renamed.json()["name"], renamed.json()["key"]) == ("Changelog", "release-notes")
        assert renamed.json()["labels"] == ["release notes", "changelog"]  # untouched
        assert (
            _post(client, "falcone/sections/release-notes/update", name="Issues").status_code == 409
        )
        # A core section keeps its name; its guide can be rewritten.
        assert _post(client, "falcone/sections/issues/update", name="Bugs").status_code == 409
        guided = _post(
            client,
            "falcone/sections/issues/update",
            description="Only defects, never tasks.",
            labels=["bug", "crash"],
            examples=[],
        )
        assert guided.status_code == 200
        assert guided.json()["description"] == "Only defects, never tasks."
        assert (guided.json()["labels"], guided.json()["examples"]) == (["bug", "crash"], [])
        assert guided.json()["name"] == "Issues" and guided.json()["use_when"]  # left as it was
        # The description can be rewritten but not emptied.
        emptied = _post(client, "falcone/sections/issues/update", description="  ")
        assert emptied.status_code == 422
        # "regression" was given up by Issues above, so another section can take it.
        assert (
            _post(
                client, "falcone/sections/release-notes/update", labels=["regression"]
            ).status_code
            == 200
        )

        assert _post(client, "falcone/sections/issues/remove").status_code == 409
        removed = _post(client, "falcone/sections/release-notes/remove")
        assert removed.status_code == 200 and removed.json()["key"] == "release-notes"
        assert _post(client, "falcone/sections/release-notes/remove").status_code == 404
        assert [s["key"] for s in _sections(client, "falcone")] == CORE_KEYS

        kinds = [
            e["event_type"] for e in client.get("/api/dashboard/activity/recent").json()["items"]
        ]
        for expected in (
            "dashboard.section_added",
            "dashboard.section_changed",
            "dashboard.section_removed",
        ):
            assert expected in kinds
    finally:
        client.__exit__(None, None, None)


def test_the_brief_is_what_the_supervisor_reads(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        response = client.get("/api/dashboard/projects/falcone/routing")
        assert response.status_code == 200 and response.json()["project"] == "falcone"
        brief = response.json()["brief"]
        # The rules come first: one section, never the nearest, never a neighbour's flow.
        rules, _, sections = brief.partition("## ")
        assert "exactly one section" in rules
        assert "never choose the nearest one" in rules
        assert "never send the work to another section instead" in rules
        for name in ("Code audit", "UI audit", "UX audit", "Code clean", "Enhancement", "Issues"):
            assert f"## {name} (section key: " in brief
        issues = brief[brief.index("## Issues") :]
        for part in (
            "What belongs here: Issues reported on the repository",
            "Use it when: Something is wrong",
            "Do not use it when: The request asks for something new (use Enhancement)",
            "- Signing in fails when the session cookie has expired.",
            "belongs here without further judgement: bug, defect, regression, fix.",
            "Flow: Deliver issue. Implement what an item asks for",
        ):
            assert part in issues, part
        ui_audit = brief[brief.index("## UI audit") : brief.index("## UX audit")]
        assert "Flow: none assigned. This section cannot run work yet." in ui_audit
        # The brief follows the records: a rewritten guide and a new flow show up in it.
        _post(client, "falcone/sections/ui-audit/update", use_when="Only after a redesign.")
        _post(client, "falcone/sections/ui-audit/flow", flow="plan-ui-migration")
        changed = client.get("/api/dashboard/projects/falcone/routing").json()["brief"]
        assert "Use it when: Only after a redesign." in changed
        assert "Flow: Plan UI migration." in changed
        assert client.get("/api/dashboard/projects/absent/routing").status_code == 404
        client.cookies.clear()
        assert client.get("/api/dashboard/projects/falcone/routing").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_there_is_a_limit_and_a_removed_project_has_no_sections(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        client.post(
            "/api/dashboard/projects",
            json={"csrf_token": _csrf(client), "name": "docs", "repository": "gntik-ai/docs"},
        )
        for index in range(20):
            added = _post(client, "docs/sections", name=f"Section {index}", description="Work.")
            assert added.status_code == 201
        too_many = _post(client, "docs/sections", name="One more", description="Work.")
        assert too_many.status_code == 409 and "20 sections" in too_many.json()["detail"]
        assert _post(client, "docs/archive", current_password=PASSWORD).status_code == 200
        assert client.get("/api/dashboard/projects/docs").status_code == 404
        assert _post(client, "docs/sections", name="Late", description="x").status_code == 404
        assert _post(client, "docs/sections/issues/flow", flow=None).status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_a_section_change_needs_the_session_and_its_token(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        path = "/api/dashboard/projects/falcone/sections"
        assert client.post(path, json={"csrf_token": "x" * 32, "name": "Docs"}).status_code == 403
        extra = _post(client, "falcone/sections", name="Docs", description="x", extra="field")
        assert extra.status_code == 422
        token = _csrf(client)
        client.cookies.clear()
        assert client.post(path, json={"csrf_token": token, "name": "Docs"}).status_code == 401
        assert client.get("/api/dashboard/projects/falcone").status_code == 401
        assert client.get("/api/dashboard/flows").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_section_keys_are_made_from_names() -> None:
    assert section_key("Release notes") == "release-notes"
    assert section_key("Révision d'été 2026") == "revision-d-ete-2026"
    assert len(section_key("x" * 80)) == 40
