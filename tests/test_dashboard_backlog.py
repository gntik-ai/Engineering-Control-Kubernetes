"""The backlog: items from GitHub issues and from the administrator, routed to sections."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engineering_control.dashboard.backlog_items import route_by_labels
from engineering_control.db.models import ProjectSection
from engineering_control.github_app import GitHubAppError
from tests.test_github_app import PASSWORD, GitHub, _app, _console, _repository

FALCONE = "gntik-ai/falcone"
NOT_IT = "not it"  # a wrong answer to "confirm with your password"


def _issue(number: int, title: str, *labels: str, **extra: Any) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/{FALCONE}/issues/{number}",
        "labels": [{"name": label} for label in labels],
        "updated_at": f"2026-11-{number % 28 + 1:02d}T10:00:00Z",
        **extra,
    }


def _csrf(client: TestClient) -> str:
    return str(client.get("/api/dashboard/session").json()["csrf_token"])


def _post(client: TestClient, path: str, **body: Any) -> Any:
    return client.post(f"/api/dashboard/{path}", json={"csrf_token": _csrf(client), **body})


def _items(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    response = client.get("/api/dashboard/items", params=params)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def _by_number(client: TestClient, **params: Any) -> dict[int, dict[str, Any]]:
    return {item["issue_number"]: item for item in _items(client, **params)}


@pytest.fixture
def github() -> GitHub:
    fake = GitHub([_repository("falcone"), _repository("docs")])
    fake.issues[FALCONE] = [
        _issue(1, "Signing in fails with an expired cookie", "bug"),
        _issue(2, "Export reports to CSV", "enhancement", "good first issue"),
        _issue(3, "Unclear which way this goes", "bug", "enhancement"),
        _issue(4, "No label at all"),
        _issue(5, "A pull request, not an issue", "bug", pull_request={"url": "x"}),
        _issue(6, "  Tidy   the helpers ", "Refactor"),
    ]
    return fake


def test_issues_are_imported_and_routed_by_their_labels(tmp_path: Path, github: GitHub) -> None:
    client = _console(tmp_path, _app(tmp_path, github))
    try:
        before = client.get("/api/dashboard/projects/falcone/backlog").json()
        assert before == {"github": True, "last_sync": None}
        synced = _post(client, "projects/falcone/backlog/sync")
        assert synced.status_code == 200
        result = synced.json()
        assert (result["imported"], result["updated"], result["closed"]) == (5, 0, 0)
        assert result["routed"] == 3 and result["open_issues"] == 5 and result["complete"] is True

        items = _by_number(client, project="falcone")
        assert set(items) == {1, 2, 3, 4, 6}  # the pull request is not an item
        assert all(
            item["state"] == "proposed" and item["source"] == "github" for item in items.values()
        )
        # One label of one section decides without a judgement, whatever its case.
        assert (items[1]["section"]["key"], items[1]["routed_by"]) == ("issues", "label")
        assert items[1]["routing_reason"] == "Label bug"
        assert items[2]["section"]["key"] == "enhancement"  # "good first issue" belongs nowhere
        assert (
            items[6]["section"]["key"] == "code-clean" and items[6]["title"] == "Tidy the helpers"
        )
        # Labels of two sections decide nothing: that is a judgement, and it is said why.
        assert items[3]["section"] is None and items[3]["routed_by"] is None
        assert items[3]["routing_reason"] == (
            "Its labels point to more than one section: Enhancement, Issues"
        )
        assert items[4]["section"] is None and items[4]["routing_reason"] is None
        assert items[1]["issue_url"] == f"https://github.com/{FALCONE}/issues/1"
        assert items[2]["labels"] == ["enhancement", "good first issue"]

        status = client.get("/api/dashboard/projects/falcone/backlog").json()["last_sync"]
        assert status["open_issues"] == 5 and status["error"] is None and status["synced_at"]
        # The project list counts what is still to do.
        projects = client.get("/api/dashboard/projects").json()["items"]
        assert next(p for p in projects if p["project"] == "falcone")["open_items"] == 5
        assert next(p for p in projects if p["project"] == "musematic")["open_items"] == 0
    finally:
        client.__exit__(None, None, None)


def test_filters_and_search(tmp_path: Path, github: GitHub) -> None:
    client = _console(tmp_path, _app(tmp_path, github))
    try:
        _post(client, "projects/falcone/backlog/sync")
        assert set(_by_number(client, project="falcone", section="issues")) == {1}
        assert set(_by_number(client, section="none")) == {3, 4}
        assert set(_by_number(client, search="csv")) == {2}
        assert set(_by_number(client, search="#4")) == {4}
        assert set(_by_number(client, search="100%")) == set()
        assert _items(client, project="musematic") == []
        assert _items(client, state="done") == []
        page = client.get("/api/dashboard/items", params={"page_size": 2, "page": 3}).json()
        assert (page["total"], len(page["items"]), page["page"]) == (5, 1, 3)
        for bad in ({"project": "absent"}, {"state": "running"}, {"section": "Not A Key"}):
            assert client.get("/api/dashboard/items", params=bad).status_code == 422, bad
    finally:
        client.__exit__(None, None, None)


def test_a_later_import_follows_github_and_keeps_what_the_administrator_decided(
    tmp_path: Path, github: GitHub
) -> None:
    client = _console(tmp_path, _app(tmp_path, github))
    try:
        _post(client, "projects/falcone/backlog/sync")
        items = _by_number(client, project="falcone")
        # The administrator overrules a label, routes an unrouted item, accepts and skips.
        assert (
            _post(client, f"items/{items[1]['id']}/route", section="code-audit").status_code == 200
        )
        routed = _post(client, f"items/{items[4]['id']}/route", section="ux-audit")
        assert routed.json()["section"]["key"] == "ux-audit"
        assert routed.json()["routed_by"] == "administrator"
        assert _post(client, f"items/{items[2]['id']}/state", state="accepted").status_code == 200
        assert _post(client, f"items/{items[6]['id']}/state", state="skipped").status_code == 200

        github.issues[FALCONE] = [
            _issue(1, "Signing in fails with an expired cookie (again)", "bug"),
            _issue(3, "Now clearly a defect", "bug"),
            _issue(4, "No label at all", "enhancement"),
            _issue(6, "Tidy the helpers", "refactor"),
            _issue(7, "A new one", "regression"),
        ]
        result = _post(client, "projects/falcone/backlog/sync").json()
        assert (result["imported"], result["updated"], result["closed"]) == (1, 4, 1)
        items = _by_number(client, project="falcone")
        # Title and labels follow GitHub; the administrator's sections do not move.
        assert items[1]["title"].endswith("(again)") and items[1]["section"]["key"] == "code-audit"
        assert items[4]["section"]["key"] == "ux-audit" and items[4]["routed_by"] == "administrator"
        # An item nobody routed follows its labels as they change.
        assert (items[3]["section"]["key"], items[3]["routing_reason"]) == ("issues", "Label bug")
        assert items[7]["section"]["key"] == "issues" and items[7]["state"] == "proposed"
        # Closed on GitHub: done, whatever it was. Skipped stays skipped.
        assert (items[2]["state"], items[2]["issue_state"]) == ("done", "closed")
        assert items[6]["state"] == "skipped"
        closed_again = _post(client, f"items/{items[2]['id']}/state", state="accepted")
        assert (
            closed_again.status_code == 409 and "closed on GitHub" in closed_again.json()["detail"]
        )

        # Reopened on GitHub: to be decided on again.
        github.issues[FALCONE].append(_issue(2, "Export reports to CSV", "enhancement"))
        reopened = _post(client, "projects/falcone/backlog/sync").json()
        assert reopened["reopened"] == 1
        assert _by_number(client, project="falcone")[2]["state"] == "proposed"
        # The administrator can hand an item back to the labels by clearing its section...
        cleared = _post(client, f"items/{items[1]['id']}/route", section=None)
        assert cleared.json()["section"] is None and cleared.json()["routed_by"] == "administrator"
        # ...and "no section" is then their decision too: the next import leaves it.
        _post(client, "projects/falcone/backlog/sync")
        assert _by_number(client, project="falcone")[1]["section"] is None
    finally:
        client.__exit__(None, None, None)


def test_items_written_by_the_administrator(tmp_path: Path, github: GitHub) -> None:
    client = _console(tmp_path, _app(tmp_path, github))
    try:
        added = _post(
            client, "projects/falcone/items", title="  Write the  runbook ", section="issues"
        )
        assert added.status_code == 201
        item = added.json()
        assert (item["title"], item["source"], item["state"]) == (
            "Write the runbook",
            "manual",
            "accepted",
        )
        assert item["section"]["key"] == "issues" and item["routed_by"] == "administrator"
        assert item["issue_number"] is None and item["issue_url"] is None
        unrouted = _post(client, "projects/falcone/items", title="Think about pricing")
        assert unrouted.json()["section"] is None and unrouted.json()["routed_by"] is None
        assert _post(client, "projects/falcone/items", title="   ").status_code == 422
        assert (
            _post(client, "projects/falcone/items", title="x", section="absent").status_code == 422
        )
        assert _post(client, "projects/absent/items", title="x").status_code == 404
        # An import does not touch them.
        _post(client, "projects/falcone/backlog/sync")
        manual = [i for i in _items(client, project="falcone") if i["source"] == "manual"]
        assert {i["title"] for i in manual} == {"Write the runbook", "Think about pricing"}
        done = _post(client, f"items/{item['id']}/state", state="done")
        assert done.status_code == 200 and done.json()["state"] == "done"
        assert _post(client, f"items/{item['id']}/state", state="running").status_code == 422
        missing = "00000000-0000-0000-0000-000000000000"
        assert _post(client, f"items/{missing}/state", state="done").status_code == 404
        assert _post(client, f"items/{missing}/route", section=None).status_code == 404
        # A section that is removed leaves its items unrouted, not lost.
        _post(client, "projects/falcone/sections", name="Runbooks", description="Operating notes.")
        _post(client, f"items/{item['id']}/route", section="runbooks")
        _post(client, "projects/falcone/sections/runbooks/remove")
        kept = next(i for i in _items(client, project="falcone") if i["id"] == item["id"])
        assert kept["section"] is None
    finally:
        client.__exit__(None, None, None)


def test_what_goes_wrong_is_said_and_recorded(tmp_path: Path, github: GitHub) -> None:
    client = _console(tmp_path, _app(tmp_path, github))
    try:
        # The app cannot see the repository of this project.
        unseen = _post(client, "projects/musematic/backlog/sync")
        assert unseen.status_code == 502
        assert (
            unseen.json()["detail"] == "The GitHub App cannot see the repository gntik-ai/musematic"
        )
        status = client.get("/api/dashboard/projects/musematic/backlog").json()["last_sync"]
        assert status["error"] == unseen.json()["detail"]
        # A repository with issues switched off simply has none.
        github.issues_disabled.add("gntik-ai/llmwiki")
        empty = _post(client, "projects/llmwiki/backlog/sync")
        assert empty.status_code == 200 and empty.json()["open_issues"] == 0
        assert _post(client, "projects/absent/backlog/sync").status_code == 404
        assert client.get("/api/dashboard/projects/absent/backlog").status_code == 404
        # A change needs the session and its token.
        assert (
            client.post(
                "/api/dashboard/projects/falcone/backlog/sync", json={"csrf_token": "x" * 32}
            ).status_code
            == 403
        )
        client.cookies.clear()
        assert client.get("/api/dashboard/items").status_code == 401
        assert client.get("/api/dashboard/projects/falcone/backlog").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_without_the_app_items_are_written_by_hand(tmp_path: Path) -> None:
    client = _console(tmp_path, None)
    try:
        assert client.get("/api/dashboard/projects/falcone/backlog").json() == {
            "github": False,
            "last_sync": None,
        }
        refused = _post(client, "projects/falcone/backlog/sync")
        assert refused.status_code == 409 and "not configured" in refused.json()["detail"]
        assert _post(client, "projects/falcone/items", title="By hand").status_code == 201
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_an_incomplete_listing_closes_nothing(tmp_path: Path, github: GitHub) -> None:
    client = _console(tmp_path, _app(tmp_path, github))
    try:
        _post(client, "projects/falcone/backlog/sync")
        backlog = client.app.state.dashboard_repository.backlog
        # GitHub had more open issues than were read: absence proves nothing.
        result = backlog.apply_import("falcone", [], complete=False)
        assert result["closed"] == 0
        assert all(item["state"] == "proposed" for item in _items(client, project="falcone"))
        app = client.app.state.github_app
        with pytest.raises(GitHubAppError, match="not the name of a repository"):
            await app.open_issues("../../etc/passwd")
    finally:
        client.__exit__(None, None, None)


def test_label_routing_rules() -> None:
    def section(name: str, *labels: str) -> ProjectSection:
        return ProjectSection(key=name.lower(), name=name, labels=list(labels))

    sections = [section("Issues", "bug", "defect"), section("Enhancement", "enhancement")]
    assert route_by_labels(sections, ["BUG"]) == (sections[0], "Label bug")
    assert route_by_labels(sections, ["question"]) == (None, None)
    assert route_by_labels(sections, []) == (None, None)
    chosen, reason = route_by_labels(sections, ["defect", "Enhancement"])
    assert chosen is None and reason and "more than one section" in reason
    # Two labels of the same section are still one section.
    assert route_by_labels(sections, ["bug", "defect"])[0] is sections[0]


def test_the_inbox_lists_what_waits_and_where(tmp_path: Path, github: GitHub) -> None:
    client = _console(tmp_path, _app(tmp_path, github))
    try:
        assert client.get("/api/dashboard/inbox").json() == {"items": [], "total": 0}
        _post(client, "projects/falcone/backlog/sync")
        _post(client, "projects/musematic/backlog/sync")  # the app cannot see that repository
        items = _by_number(client, project="falcone")
        _post(
            client, f"items/{items[4]['id']}/route", section="ux-audit"
        )  # a section without a flow
        _post(client, f"items/{items[1]['id']}/state", state="accepted")

        inbox = client.get("/api/dashboard/inbox").json()
        entries = inbox["items"]
        assert inbox["total"] == len(entries) == 5
        kinds = [
            (e["kind"], e["project"], (e["section"] or {}).get("key"), e["count"]) for e in entries
        ]
        assert kinds == [
            # What stops work comes first, then what waits for a decision.
            ("sync_failed", "musematic", None, 1),
            ("section_without_flow", "falcone", "code-clean", 1),
            ("section_without_flow", "falcone", "ux-audit", 1),
            ("unrouted", "falcone", None, 1),
            ("undecided", "falcone", None, 4),
        ]
        assert entries[0]["detail"] == "The GitHub App cannot see the repository gntik-ai/musematic"
        assert (
            entries[1]["section"]["name"] == "Code clean"
            and entries[1]["project_name"] == "falcone"
        )
        # Giving the section a flow, and deciding, empties those entries.
        _post(client, "projects/falcone/sections/ux-audit/flow", flow="change-request")
        left = [e["kind"] for e in client.get("/api/dashboard/inbox").json()["items"]]
        assert left.count("section_without_flow") == 1
        client.cookies.clear()
        assert client.get("/api/dashboard/inbox").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_one_change_to_every_item_a_filter_matches(tmp_path: Path, github: GitHub) -> None:
    client = _console(tmp_path, _app(tmp_path, github))
    try:
        _post(client, "projects/falcone/backlog/sync")

        def bulk(**body: Any) -> Any:
            return _post(client, "projects/falcone/items/bulk", current_password=PASSWORD, **body)

        # Route the two unrouted items to one section.
        routed = bulk(section="none", expected=2, route_to="code-audit")
        assert routed.status_code == 200
        assert routed.json() == {"project": "falcone", "changed": 2, "unchanged": 0}
        items = _by_number(client, project="falcone")
        assert {items[3]["section"]["key"], items[4]["section"]["key"]} == {"code-audit"}
        assert items[3]["routed_by"] == items[4]["routed_by"] == "administrator"
        assert items[1]["section"]["key"] == "issues"  # outside the filter: untouched

        # Accept everything that is proposed; doing it twice changes nothing more.
        accepted = bulk(state="proposed", expected=5, state_to="accepted")
        assert accepted.json()["changed"] == 5
        assert all(i["state"] == "accepted" for i in _items(client, project="falcone"))
        again = bulk(state="accepted", expected=5, state_to="accepted")
        assert again.json() == {"project": "falcone", "changed": 0, "unchanged": 5}
        # Hand a section's items back: no section, by the administrator's decision.
        cleared = bulk(section="code-audit", expected=2, clear_route=True)
        assert cleared.json()["changed"] == 2
        assert _by_number(client, section="none").keys() == {3, 4}
        # An item whose issue is closed on GitHub is not reopened by a bulk change.
        github.issues[FALCONE] = [i for i in github.issues[FALCONE] if i["number"] != 2]
        _post(client, "projects/falcone/backlog/sync")
        reopened = bulk(search="csv", expected=1, state_to="proposed")
        assert reopened.json() == {"project": "falcone", "changed": 0, "unchanged": 1}

        # The number shown must still be the number matched.
        stale = bulk(section="none", expected=7, route_to="issues")
        assert stale.status_code == 409 and "now matches 2 items, not 7" in stale.json()["detail"]
        for body in (
            {"expected": 2, "section": "none"},  # no change named
            {"expected": 2, "section": "none", "route_to": "issues", "state_to": "done"},  # two
            {"expected": 2, "section": "none", "route_to": "absent"},
            {"expected": 0, "state_to": "done"},
            {"expected": 2, "section": "none", "state_to": "running"},
        ):
            assert bulk(**body).status_code == 422, body
        assert (
            _post(
                client,
                "projects/absent/items/bulk",
                current_password=PASSWORD,
                expected=1,
                state_to="done",
            ).status_code
            == 404
        )
        # It needs the password again, and the right one.
        without = _post(
            client, "projects/falcone/items/bulk", section="none", expected=2, state_to="skipped"
        )
        assert without.status_code == 422
        wrong = _post(
            client,
            "projects/falcone/items/bulk",
            current_password=NOT_IT,
            section="none",
            expected=2,
            state_to="skipped",
        )
        assert wrong.status_code == 403
        assert all(i["state"] == "accepted" for i in _items(client, section="none"))
    finally:
        client.__exit__(None, None, None)
