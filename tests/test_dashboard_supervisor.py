"""The supervisor in the console: the conversation, its settings, and what it proposed."""

# ruff: noqa: S106 - the passwords here are made up

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient

from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.models import SandboxRun, SandboxStep
from tests.test_dashboard_accounts import PASSWORD, _console

BASE = "/api/dashboard/supervisor"


def _post(client: TestClient, path: str, **fields: Any) -> httpx.Response:
    csrf = client.get("/api/dashboard/session").json()["csrf_token"]
    return client.post(f"{BASE}{path}", json={"csrf_token": csrf, **fields})


def _repository(client: TestClient) -> DashboardRepository:
    repository: DashboardRepository = client.app.state.dashboard_repository  # type: ignore[attr-defined]
    return repository


def test_the_supervisor_needs_a_session(tmp_path: Path) -> None:
    client = _console(tmp_path, None)
    client.cookies.clear()
    for path in ("", "/settings", "/record", "/versions/1"):
        assert client.get(f"{BASE}{path}").status_code == 401, path
    for path in ("/messages", "/stop", "/settings", f"/actions/{uuid4()}/decide"):
        assert client.post(f"{BASE}{path}", json={}).status_code == 401, path


def test_a_message_starts_a_turn_and_the_turn_can_be_stopped(tmp_path: Path) -> None:
    client = _console(tmp_path, None)
    empty = client.get(BASE).json()
    assert empty == {"items": [], "turn": None, "pending": []}
    said = _post(client, "/messages", text="  What is open?  ")
    assert said.status_code == 201 and said.json()["text"] == "What is open?"
    assert _post(client, "/messages", text="   ").status_code == 422
    assert _post(client, "/messages", text="x" * 8001).status_code == 422
    listing = client.get(BASE).json()
    assert [(m["role"], m["channel"], m["text"]) for m in listing["items"]] == [
        ("administrator", "console", "What is open?")
    ]
    assert listing["turn"]["state"] == "queued"
    # Only what was said after a moment, for a page that already has the rest.
    after = listing["items"][0]["at"]
    assert client.get(BASE, params={"after": after}).json()["items"] == []
    assert client.get(BASE, params={"after": "yesterday"}).status_code == 422
    # The turn is a run, but not one of the list of work.
    assert client.get("/api/dashboard/runs").json()["items"] == []
    stopped = _post(client, "/stop")
    assert stopped.status_code == 200 and stopped.json()["cancel_requested"] is True
    with _repository(client).sessions.begin() as session:
        run = session.get(SandboxRun, UUID(listing["turn"]["run_id"]))
        assert run is not None
        run.state = "cancelled"
    assert _post(client, "/stop").status_code == 409


def test_the_definition_is_saved_with_the_password_as_a_new_version(tmp_path: Path) -> None:
    client = _console(tmp_path, None)
    settings = client.get(f"{BASE}/settings").json()
    definition = settings["definition"]
    assert definition["version"] == 0 and settings["versions"] == []
    assert settings["providers"] == ["claude", "chatgpt"] and settings["accounts"] == []
    by_name = {tool["name"]: tool for tool in settings["catalogue"]}
    assert by_name["list_items"]["modes"] == ["automatic"]
    assert by_name["route_item"]["modes"] == ["automatic", "confirm"]
    assert by_name["start_item"]["default_mode"] == "confirm"
    fields = {
        "prompt": "You plan work, briefly.",
        "skills": definition["skills"][:1],
        "tools": {"start_item": {"enabled": True, "mode": "automatic"}},
        "providers": ["chatgpt", "claude"],
        "models": {"claude": "claude-opus-5-5"},
        "note": "Starts work without asking",
    }
    assert _post(client, "/settings", **fields).status_code == 422  # no password
    refused = _post(client, "/settings", current_password="not the password", **fields)
    assert refused.status_code == 403
    saved = _post(client, "/settings", current_password=PASSWORD, **fields)
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 1
    assert saved.json()["tools"]["start_item"]["mode"] == "automatic"
    wrong = _post(
        client, "/settings", current_password=PASSWORD, **{**fields, "providers": ["deepseek"]}
    )
    assert wrong.status_code == 422 and "providers" in wrong.json()["detail"]
    again = client.get(f"{BASE}/settings").json()
    assert again["definition"]["prompt"] == "You plan work, briefly."
    assert [v["version"] for v in again["versions"]] == [1]
    assert again["versions"][0]["note"] == "Starts work without asking"
    assert client.get(f"{BASE}/versions/1").json()["providers"] == ["chatgpt", "claude"]
    assert client.get(f"{BASE}/versions/0").json()["version"] == 0
    # The model and how hard it reasons are offered by provider and chosen without the
    # password: the choice changes nothing of what the supervisor may do.
    assert [m["name"] for m in again["choices"]["claude"]["models"]][:2] == ["fable", "opus"]
    assert "ultra" in again["choices"]["chatgpt"]["efforts"]
    chosen = _post(client, "/model", provider="chatgpt", model="gpt-6.1-sol", effort="high")
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["version"] == 2
    assert chosen.json()["models"] == {"claude": "claude-opus-5-5", "chatgpt": "gpt-6.1-sol"}
    assert chosen.json()["efforts"] == {"chatgpt": "high"}
    assert chosen.json()["tools"]["start_item"]["mode"] == "automatic"
    refused = _post(client, "/model", provider="chatgpt", model="gpt-6-luna", effort="ultra")
    assert refused.status_code == 422 and "GPT-6-Luna" in refused.json()["detail"]
    assert _post(client, "/model", provider="mistral").status_code == 422
    # A whole definition carries the levels too.
    whole = _post(
        client,
        "/settings",
        current_password=PASSWORD,
        **{**fields, "efforts": {"claude": "max"}},
    )
    assert whole.status_code == 200 and whole.json()["efforts"] == {"claude": "max"}
    listed = client.get(f"{BASE}/settings").json()["versions"]
    assert listed[1]["note"] == "Model on chatgpt: gpt-6.1-sol, high reasoning"


def test_what_the_supervisor_proposed_is_decided_in_the_console(tmp_path: Path) -> None:
    client = _console(tmp_path, None)
    repository = _repository(client)
    # The registry's projects are there from the start; their sections are made here.
    repository.sections.sync_core()
    item = repository.backlog.add(
        "falcone-workflow", title="Show why a sign-in failed", section=None
    )
    _post(client, "/messages", text="Route it.")
    with repository.sessions.begin() as session:
        run = session.query(SandboxRun).one()
        run.state = "running"
        session.query(SandboxStep).one().state = "running"
        run_id = run.id
    definition = repository.definitions.current()

    async def ask(confidence: str) -> str:
        outcome = await repository.toolbox.call(
            run_id,
            definition,
            "route_item",
            {
                "item": item["id"],
                "section": "enhancement",
                "reason": "New behaviour.",
                "confidence": confidence,
            },
        )
        return outcome.text

    assert asyncio.run(ask("medium")).startswith("Not done:")
    listing = client.get(BASE).json()
    (waiting,) = listing["pending"]
    assert waiting["summary"] == "Route “Show why a sign-in failed” to Enhancement."
    inbox = client.get("/api/dashboard/inbox").json()["items"]
    assert [entry["kind"] for entry in inbox if entry["kind"] == "supervisor_action"] == [
        "supervisor_action"
    ]
    path = f"/actions/{waiting['action_id']}/decide"
    assert _post(client, path).status_code == 422
    accepted = _post(client, path, accept=True)
    assert accepted.status_code == 200 and accepted.json()["outcome"] == "accepted"
    assert _post(client, path, accept=False).status_code == 409
    assert _post(client, f"/actions/{uuid4()}/decide", accept=True).status_code == 404
    assert repository.backlog.get(UUID(item["id"]))["section"]["key"] == "enhancement"
    after = client.get(BASE).json()
    assert after["pending"] == []
    assert after["items"][-1]["role"] == "platform"
    assert after["items"][-1]["text"].startswith("The administrator accepted")
    record = client.get(f"{BASE}/record").json()["items"]
    assert [(entry["tool"], entry["outcome"], entry["decided_via"]) for entry in record] == [
        ("route_item", "accepted", "console")
    ]
    assert record[0]["title"] == "Route an item to a section"
    assert record[0]["project"] == "falcone-workflow"
    assert client.get(f"{BASE}/record", params={"everything": "true"}).json()["total"] == 1
