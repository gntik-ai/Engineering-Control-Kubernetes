"""The supervisor: its definition, its conversation, a turn in a sandbox, and the checks
every call to a tool goes through."""

# ruff: noqa: S105, S106 - the tokens here are made up

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from engineering_control.cluster import Cluster
from engineering_control.dashboard.accounts import Accounts
from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
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
    SupervisorAction,
)
from engineering_control.github_app import Issue
from engineering_control.sandbox import steps
from engineering_control.sandbox.launcher import Launcher
from engineering_control.sandbox.runs import Runs
from engineering_control.supervisor import tools as tools_module
from engineering_control.supervisor.conversation import Conversation
from engineering_control.supervisor.definition import (
    CATALOGUE,
    DEFAULT_PROMPT,
    Definitions,
    default_tools,
    merged_tools,
)
from engineering_control.supervisor.service import create_app, toolbox_from
from engineering_control.supervisor.turn import compose, token_digest, turn_answer, turn_job
from tests.test_sandbox_runs import NAMESPACE, SANDBOXES, FakeCluster

REPOSITORY = "gntik-ai/falcone-workflow"
TOOLS_URL = "http://10.43.7.7:8790"


def _log(commands: list[dict[str, Any]]) -> str:
    encoded = base64.b64encode(json.dumps({"commands": commands}).encode()).decode()
    return f"\n{steps.MARKER}{encoded}\n"


def _ran(name: str, stdout: str = "", exit_code: int = 0, stderr: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "exit_code": exit_code,
        "timed_out": False,
        "seconds": 1.0,
        "stdout": stdout,
        "stderr": stderr,
    }


def _claude_said(text: str, *, error: bool = False) -> str:
    return "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "result", "is_error": error, "result": text}),
        ]
    )


@pytest.fixture
def world(tmp_path: Path) -> Any:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'supervisor.db'}")
    Base.metadata.create_all(engine)
    sessions: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)
    token = tmp_path / "token"
    token.write_text("cluster-identity-" + "t" * 40)
    token.chmod(0o600)
    fake = FakeCluster()
    cluster = Cluster(
        server="https://kubernetes.default.svc",
        namespace=NAMESPACE,
        token_file=token,
        ca_file=None,
        transport=httpx.MockTransport(fake),
    )
    accounts = Accounts(sessions)
    projects = ProjectRecords(sessions)
    sections = SectionRecords(sessions)
    projects.create(
        name="falcone-workflow",
        display_name="Falcone workflow",
        repository=REPOSITORY,
        default_branch="main",
    )
    sections.sync_core()
    with sessions.begin() as session:
        project = session.query(ProjectRecord).filter_by(name="falcone-workflow").one()
        issues = session.query(ProjectSection).filter_by(project_id=project.id, key="issues").one()
        issues.flow_key = "deliver-issue"
        item = BacklogItem(
            project_id=project.id,
            title="Signing in fails with an expired cookie",
            source="github",
            state="proposed",
            repository=REPOSITORY,
            issue_number=11,
            issue_url=f"https://github.com/{REPOSITORY}/issues/11",
            issue_labels=[],
        )
        session.add(item)
        session.flush()
        item_id = item.id

    class World:
        def __init__(self) -> None:
            self.fake = fake
            self.sessions = sessions
            self.item_id = item_id
            self.accounts = accounts
            self.runs = Runs(sessions)
            self.backlog = BacklogItems(sessions)
            self.definitions = Definitions(sessions)
            self.conversation = Conversation(sessions, self.definitions)
            self.toolbox = toolbox_from(sessions, None)
            self.launcher = Launcher(
                self.runs,
                cluster,
                SANDBOXES,
                conversation=self.conversation,
                tools_url=TOOLS_URL,
            )
            self.client = TestClient(create_app(sessions))

        def account(self, name: str, provider: str) -> UUID:
            account_id = UUID(accounts.create(name=name, provider=provider)["id"])
            accounts.signed_in(account_id, None)
            return account_id

        def turns(self) -> list[SandboxRun]:
            with sessions() as session:
                return list(
                    session.scalars(
                        select(SandboxRun)
                        .where(SandboxRun.kind == "supervisor_turn")
                        .order_by(SandboxRun.created_at)
                    )
                )

        def job(self) -> dict[str, Any]:
            container = self.fake.only_pod()["spec"]["containers"][0]
            env = {entry["name"]: entry["value"] for entry in container["env"]}
            return json.loads(env["SANDBOX_JOB"])

        async def running_turn(self, text: str = "What is open?") -> tuple[UUID, str]:
            """Post a message, start its turn, and return the turn with its token."""
            self.conversation.post(text, channel="console")
            await self.launcher.tick()
            argv = self.job()["commands"][0]["argv"]
            servers = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
            bearer = servers["platform"]["headers"]["Authorization"]
            return self.turns()[-1].id, bearer.removeprefix("Bearer ")

        def rpc(
            self, run_id: UUID, token: str, method: str, params: dict[str, Any] | None = None
        ) -> Any:
            response = self.client.post(
                f"/mcp/{run_id}",
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200, response.text
            return response.json()["result"]

        def call(self, run_id: UUID, token: str, name: str, **arguments: Any) -> tuple[str, bool]:
            result = self.rpc(run_id, token, "tools/call", {"name": name, "arguments": arguments})
            return result["content"][0]["text"], result["isError"]

        def actions(self, *, outcome: str | None = None) -> list[SupervisorAction]:
            with sessions() as session:
                query = select(SupervisorAction).order_by(SupervisorAction.created_at)
                if outcome is not None:
                    query = query.where(SupervisorAction.outcome == outcome)
                return list(session.scalars(query))

    yield World()
    engine.dispose()


# --- the definition ---------------------------------------------------------------------


def test_the_definition_starts_as_the_platforms_own_and_every_save_is_a_version(world: Any) -> None:
    current = world.definitions.current()
    assert current["version"] == 0 and current["prompt"] == DEFAULT_PROMPT
    assert current["providers"] == ["claude", "chatgpt"]
    assert set(current["tools"]) == {tool.name for tool in CATALOGUE}
    # Reading is done without asking; starting work is confirmed unless said otherwise.
    assert current["tools"]["list_items"] == {"enabled": True, "mode": "automatic"}
    assert current["tools"]["start_item"] == {"enabled": True, "mode": "confirm"}

    def save(**changes: Any) -> dict[str, Any]:
        return world.definitions.save(
            **{
                "prompt": "You plan work.",
                "skills": current["skills"],
                "tools": current["tools"],
                "providers": ["chatgpt"],
                "account_id": None,
                "exclusive": False,
                "models": {"chatgpt": "gpt-5.5"},
                "note": "Shorter",
                **changes,
            }
        )

    first = save()
    assert (first["version"], first["providers"], first["models"]) == (
        1,
        ["chatgpt"],
        {"chatgpt": "gpt-5.5"},
    )
    second = save(tools={"route_item": {"enabled": False, "mode": "confirm"}})
    assert second["version"] == 2 and second["tools"]["route_item"]["enabled"] is False
    # A tool the stored choices do not mention has its default.
    assert second["tools"]["list_items"] == {"enabled": True, "mode": "automatic"}
    assert [v["version"] for v in world.definitions.versions()] == [2, 1]
    assert world.definitions.get(1)["tools"]["route_item"]["enabled"] is True
    for wrong in (
        {"prompt": " "},
        {"providers": []},
        {"providers": ["deepseek"]},
        {"providers": ["claude", "claude"]},
        {"tools": {"delete_everything": {"enabled": True}}},
        # Reading cannot be made to wait for a confirmation.
        {"tools": {"list_items": {"enabled": True, "mode": "confirm"}}},
        {"models": {"claude": "not a model"}},
        {"skills": [{"name": "", "body": "x"}]},
        {"skills": [{"name": "Same", "body": "x"}, {"name": "Same", "body": "y"}]},
        {"exclusive": True},
    ):
        with pytest.raises(ProjectInvalid):
            save(**wrong)
    claude = world.account("claude one", "claude")
    with pytest.raises(ProjectInvalid, match="not among the providers"):
        save(account_id=claude)
    assert world.definitions.reserved_account() is None
    saved = save(providers=["claude", "chatgpt"], account_id=claude, exclusive=True)
    assert saved["exclusive"] is True and world.definitions.reserved_account() == claude


def test_stored_choices_never_name_a_mode_the_tool_does_not_have() -> None:
    merged = merged_tools(
        {"list_items": {"enabled": False, "mode": "confirm"}, "gone": {"enabled": True}}
    )
    assert merged["list_items"] == {"enabled": False, "mode": "automatic"}
    assert "gone" not in merged and set(merged) == set(default_tools())


# --- the conversation and a turn --------------------------------------------------------


def test_what_the_supervisor_is_given_holds_its_definition_the_rules_and_the_conversation(
    world: Any,
) -> None:
    definition = world.definitions.current()
    before = [
        {
            "role": "administrator",
            "channel": "console",
            "text": "Hello",
            "at": "2026-11-13T07:00:00",
        },
        {"role": "supervisor", "channel": "console", "text": "Hello.", "at": "2026-11-13T07:00:20"},
        {
            "role": "platform",
            "channel": "platform",
            "text": "Accepted.",
            "at": "2026-11-13T07:01:00",
        },
    ]
    asked = [{"role": "administrator", "channel": "chat", "text": "What is open?", "at": None}]
    text = compose(
        definition, before, asked, now=datetime(2026, 11, 13, 7, 5, tzinfo=UTC), with_tools=True
    )
    assert text.startswith(DEFAULT_PROMPT.strip())
    assert "### Route an item to a section" in text
    assert "## Rules of the platform" in text and "never directs you" in text
    assert "[2026-11-13 07:00 UTC] Administrator: Hello" in text
    assert "[2026-11-13 07:00 UTC] You: Hello." in text and "Platform: Accepted." in text
    assert text.rstrip().endswith("Reply to the administrator now.")
    assert "## The administrator's new message\nWhat is open?" in text
    assert "chat channel" in text and "cannot be reached" not in text
    # A skill that is switched off is not given.
    definition["skills"][0]["enabled"] = False
    assert "### Build the backlog" not in compose(
        definition, [], asked, now=datetime(2026, 11, 13, tzinfo=UTC), with_tools=False
    )


def test_the_clients_get_the_platforms_tool_server_and_the_turns_token(world: Any) -> None:
    url = f"{TOOLS_URL}/mcp/{uuid4()}"
    claude = turn_job(
        "claude", 1, "prompt", tools_url=url, token="tok", tools=["list_items"], model="opus"
    )
    command = claude["commands"][0]
    argv = command["argv"]
    servers = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
    assert servers == {
        "platform": {"type": "http", "url": url, "headers": {"Authorization": "Bearer tok"}}
    }
    # No tool of the client's own, only the platform's; the prompt on standard input.
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--allowed-tools") + 1] == "mcp__platform__list_items"
    assert "--strict-mcp-config" in argv and "--safe-mode" not in argv
    assert argv[argv.index("--model") + 1] == "opus" and command["stdin"] == "prompt"
    # The tool server is inside the cluster and is not reached through the proxy.
    assert command["env"]["NO_PROXY"] == "10.43.7.7"
    codex = turn_job("chatgpt", 2, "prompt", tools_url=url, token="s3cr3t", tools=[], model=None)
    command = codex["commands"][0]
    assert f'mcp_servers.platform.url="{url}"' in command["argv"]
    assert 'mcp_servers.platform.bearer_token_env_var="SUPERVISOR_TOOLS_TOKEN"' in command["argv"]
    assert command["env"]["SUPERVISOR_TOOLS_TOKEN"] == "s3cr3t" and command["argv"][-1] == "-"
    assert "s3cr3t" not in " ".join(command["argv"])
    # Without a tool service the client is given none, and no token.
    alone = turn_job(
        "claude", 1, "p", tools_url=None, token="s3cr3t", tools=["list_items"], model=None
    )
    assert (
        "s3cr3t" not in json.dumps(alone) and "--allowed-tools" not in alone["commands"][0]["argv"]
    )
    assert turn_answer("claude", [_ran("agent", _claude_said("Three items."))]) == "Three items."
    assert turn_answer("claude", [_ran("agent", _claude_said("Nope", error=True))]) is None
    assert turn_answer("chatgpt", [_ran("agent"), _ran("answer", " Two.\n")]) == "Two."
    assert turn_answer("chatgpt", [_ran("agent", exit_code=1), _ran("answer", exit_code=1)]) is None


async def test_a_message_is_answered_by_a_turn_in_a_sandbox(world: Any) -> None:
    world.account("claude one", "claude")
    asked = world.conversation.post("  What is open?  ", channel="console")
    assert asked["text"] == "What is open?" and asked["role"] == "administrator"
    listing = world.conversation.listing()
    assert listing["turn"]["state"] == "queued"
    # A second message while the first is being answered waits for the next turn.
    world.conversation.post("And what failed?", channel="chat")
    assert len(world.turns()) == 1
    # The supervisor's turns are a conversation, not work: the list of runs leaves them out.
    assert world.runs.listing()["items"] == []
    assert len(world.runs.listing(conversation=True)["items"]) == 1

    await world.launcher.tick()
    pod = world.fake.only_pod()
    assert pod["metadata"]["labels"]["engineering-control/tools"] == "supervisor"
    assert pod["metadata"]["labels"]["engineering-control/egress"] == "internet"
    assert pod["metadata"]["labels"]["engineering-control/step"] == "answer"
    job = world.job()
    assert job["env_files"] == {"CLAUDE_CODE_OAUTH_TOKEN": "/run/account/token"}
    command = job["commands"][0]
    assert "## The administrator's new message\nWhat is open?" in command["stdin"]
    assert "And what failed?" not in command["stdin"]
    argv = command["argv"]
    servers = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
    run = world.turns()[0]
    assert servers["platform"]["url"] == f"{TOOLS_URL}/mcp/{run.id}"
    token = servers["platform"]["headers"]["Authorization"].removeprefix("Bearer ")
    # The platform keeps the token's digest only.
    assert run.data["tools_token"] == token_digest(token) and token not in json.dumps(run.data)

    world.fake.finish(_log([_ran("agent", _claude_said("Three items are open."))]))
    # One pass reads the result, the next ends the run, the third starts what waited.
    for _ in range(3):
        await world.launcher.tick()
    first, second = world.turns()
    assert (first.state, first.outcome) == ("succeeded", "answered")
    messages = world.conversation.listing()["items"]
    assert [(m["role"], m["text"]) for m in messages] == [
        ("administrator", "What is open?"),
        ("administrator", "And what failed?"),
        ("supervisor", "Three items are open."),
    ]
    # The message that waited now has its turn, which is given what was said before.
    assert second.state in ("queued", "running")
    await world.launcher.tick()
    prompt = world.job()["commands"][0]["stdin"]
    assert "You: Three items are open." in prompt
    assert "## The administrator's new message\nAnd what failed?" in prompt
    for wrong in ("", "   ", "x" * 8001):
        with pytest.raises(ProjectInvalid):
            world.conversation.post(wrong, channel="console")
    with pytest.raises(ProjectInvalid):
        world.conversation.post("Hello", channel="email")


async def test_a_turn_that_ends_without_an_answer_is_said_so_once(world: Any) -> None:
    world.account("claude one", "claude")
    world.conversation.post("What is open?", channel="console")
    await world.launcher.tick()
    world.fake.finish(_log([_ran("agent", "boom", exit_code=1, stderr="segfault")]))
    await world.launcher.tick()
    await world.launcher.tick()
    await world.launcher.tick()
    (turn,) = world.turns()
    assert turn.state == "failed"
    messages = world.conversation.listing()["items"]
    assert [m["role"] for m in messages] == ["administrator", "platform"]
    assert messages[1]["text"].startswith("The supervisor could not answer.")
    assert world.conversation.listing()["turn"] is None


async def test_a_turn_refused_by_the_subscription_is_given_to_the_other_account(
    world: Any,
) -> None:
    claude = world.account("claude one", "claude")
    world.account("codex one", "chatgpt")
    world.conversation.post("What is open?", channel="console")
    await world.launcher.tick()
    refused = json.dumps({"type": "result", "is_error": True, "result": "Usage limit reached"})
    world.fake.finish(_log([_ran("agent", refused, exit_code=1)]))
    await world.launcher.tick()
    assert world.accounts.get(claude)["state"] == "limited"
    await world.launcher.tick()
    job = world.job()
    assert job["commands"][0]["argv"][0] == "codex"
    world.fake.finish(_log([_ran("agent"), _ran("answer", "Three items.")]))
    await world.launcher.tick()
    await world.launcher.tick()
    assert world.turns()[0].state == "succeeded"
    assert world.conversation.listing()["items"][-1]["text"] == "Three items."


def test_an_account_can_be_preferred_and_one_can_be_the_supervisors_alone(world: Any) -> None:
    first = world.account("claude one", "claude")
    second = world.account("claude two", "claude")
    current = world.definitions.current()
    world.definitions.save(
        prompt=current["prompt"],
        skills=current["skills"],
        tools=current["tools"],
        providers=["claude"],
        account_id=second,
        exclusive=True,
        models={},
        note=None,
    )
    # Other work never gets the supervisor's own account, however often it asks.
    for _ in range(3):
        run = world.runs.start_account_check(first)
        with world.sessions.begin() as session:
            step = session.scalars(
                select(SandboxStep).where(SandboxStep.run_id == UUID(run["id"]))
            ).one()
            step.account_id = None
            step.provider = "claude"
        lease = world.runs.lease(step.id)
        assert lease is not None and lease.account_id == first
        world.runs.run_finished(UUID(run["id"]), state="succeeded", outcome="ready", detail=None)
    # A turn of the supervisor gets it, although the other was used longer ago.
    world.conversation.post("Hello", channel="console")
    with world.sessions() as session:
        step = session.scalars(
            select(SandboxStep).where(SandboxStep.run_id == world.turns()[0].id)
        ).one()
    lease = world.runs.lease(step.id)
    assert lease is not None and lease.account_id == second


# --- the tool service -------------------------------------------------------------------


async def test_the_tool_service_answers_only_the_running_turn_with_its_token(world: Any) -> None:
    world.account("claude one", "claude")
    run_id, token = await world.running_turn()
    url = f"/mcp/{run_id}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert world.client.post(url, json=body).status_code == 401
    wrong = {"Authorization": "Bearer " + "x" * 43}
    assert world.client.post(url, json=body, headers=wrong).status_code == 401
    good = {"Authorization": f"Bearer {token}"}
    assert world.client.post(f"/mcp/{uuid4()}", json=body, headers=good).status_code == 401
    assert world.client.post("/mcp/not-a-run", json=body, headers=good).status_code == 404
    # No stream is offered; a notification is taken and not answered.
    assert world.client.get(url, headers=good).status_code == 405
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert world.client.post(url, json=notification, headers=good).status_code == 202
    assert world.client.post(url, json=[body], headers=good).status_code == 400
    started = world.rpc(run_id, token, "initialize", {"protocolVersion": "2025-06-18"})
    assert started["protocolVersion"] == "2025-06-18" and "tools" in started["capabilities"]
    assert (
        world.rpc(run_id, token, "initialize", {"protocolVersion": "1999"})["protocolVersion"]
        == "2025-11-25"
    )
    listed = world.rpc(run_id, token, "tools/list")["tools"]
    assert {tool["name"] for tool in listed} == {tool.name for tool in CATALOGUE}
    by_name = {tool["name"]: tool for tool in listed}
    assert by_name["list_items"]["annotations"]["readOnlyHint"] is True
    assert by_name["route_item"]["annotations"]["readOnlyHint"] is False
    assert by_name["route_item"]["inputSchema"]["required"] == [
        "item",
        "section",
        "reason",
        "confidence",
    ]
    unknown = world.client.post(
        url, json={"jsonrpc": "2.0", "id": 2, "method": "resources/list"}, headers=good
    ).json()
    assert unknown["error"]["code"] == -32601
    # Once the turn has ended its token opens nothing.
    world.fake.finish(_log([_ran("agent", _claude_said("Done."))]))
    await world.launcher.tick()
    assert world.client.post(url, json=body, headers=good).status_code == 401


async def test_reading_is_answered_and_recorded(world: Any) -> None:
    world.account("claude one", "claude")
    run_id, token = await world.running_turn()
    text, error = world.call(run_id, token, "list_projects")
    assert not error
    assert {
        "project": "falcone-workflow",
        "name": "Falcone workflow",
        "repository": REPOSITORY,
        "open_items": 1,
    } in json.loads(text)
    project = json.loads(world.call(run_id, token, "get_project", project="falcone-workflow")[0])
    assert {"key": "issues", "name": "Issues", "flow": "Deliver issue"} in project["sections"]
    assert "Every piece of work goes to exactly one section" in project["routing_brief"]
    items = json.loads(world.call(run_id, token, "list_items", project="falcone-workflow")[0])
    assert items["total"] == 1 and items["items"][0]["issue"] == 11
    assert items["items"][0]["section"] is None
    assert json.loads(world.call(run_id, token, "list_items", section="none")[0])["total"] == 1
    item = json.loads(world.call(run_id, token, "get_item", item=str(world.item_id))[0])
    assert item["title"] == "Signing in fails with an expired cookie" and item["run"] is None
    accounts = json.loads(world.call(run_id, token, "list_accounts")[0])
    assert accounts[0]["name"] == "claude one" and "identity" not in accounts[0]
    assert json.loads(world.call(run_id, token, "list_runs")[0]) == []
    inbox = json.loads(world.call(run_id, token, "get_inbox")[0])
    assert any(entry["kind"] == "unrouted" for entry in inbox)
    # What does not exist is refused in words, not with an exception.
    for name, arguments in (
        ("get_project", {"project": "nothing"}),
        ("get_item", {"item": str(uuid4())}),
        ("get_item", {"item": "eleven"}),
        ("get_run", {"run": str(uuid4())}),
        ("delete_everything", {}),
    ):
        text, error = world.call(run_id, token, name, **arguments)
        assert error and text.startswith("Refused:"), (name, text)
    recorded = world.actions()
    assert [action.outcome for action in recorded].count("read") == 8
    assert [action.outcome for action in recorded].count("refused") == 5
    assert all(action.run_id == run_id for action in recorded)


async def test_routing_is_applied_only_when_sure_and_never_over_the_administrators_choice(
    world: Any,
) -> None:
    world.account("claude one", "claude")
    run_id, token = await world.running_turn()
    item = str(world.item_id)

    def route(**changes: Any) -> tuple[str, bool]:
        arguments = {
            "item": item,
            "section": "issues",
            "reason": "A defect in existing behaviour.",
            "confidence": "high",
            **changes,
        }
        return world.call(run_id, token, "route_item", **arguments)

    # What cannot be: a section of no project, no reason, no confidence, an unknown item.
    for changes in (
        {"section": "nearest"},
        {"reason": " "},
        {"confidence": "certain"},
        {"item": str(uuid4())},
    ):
        text, error = route(**changes)
        assert error and text.startswith("Refused:"), text
    assert "Its sections: " in route(section="nearest")[0]
    # Not sure: it waits for the administrator and nothing is changed.
    text, error = route(confidence="medium")
    assert not error and text.startswith("Not done:") and "confidence: medium" in text
    assert world.backlog.get(world.item_id)["section"] is None
    (waiting,) = world.toolbox.pending()
    assert waiting["kind"] == "supervisor_action" and waiting["project"] == "falcone-workflow"
    assert waiting["summary"] == "Route “Signing in fails with an expired cookie” to Issues."
    assert waiting["reason"] == "A defect in existing behaviour."
    # Sure: applied, with the reason kept on the item.
    text, error = route()
    assert not error and text.startswith("Done: Route")
    routed = world.backlog.get(world.item_id)
    assert routed["section"]["key"] == "issues" and routed["routed_by"] == "supervisor"
    assert routed["routing_reason"] == "A defect in existing behaviour."
    assert route()[0] == "Refused: The item is already in that section"
    # The administrator's own choice is changed only with their confirmation.
    world.backlog.route(world.item_id, "enhancement")
    text, error = route()
    assert text.startswith("Not done:") and "chose yourself" in text
    assert world.backlog.get(world.item_id)["section"]["key"] == "enhancement"
    assert [a.outcome for a in world.actions() if a.tool == "route_item"].count("pending") == 2


async def test_the_administrator_accepts_or_rejects_what_waits(world: Any) -> None:
    world.account("claude one", "claude")
    run_id, token = await world.running_turn()
    item = str(world.item_id)
    world.call(
        run_id,
        token,
        "route_item",
        item=item,
        section="issues",
        reason="A defect.",
        confidence="low",
    )
    # Starting work is confirmed unless the definition says otherwise.
    text, _ = world.call(run_id, token, "start_item", item=item, reason="You asked for it.")
    assert text.startswith("Refused: The item has no section yet")
    (route,) = world.toolbox.pending()
    accepted = world.toolbox.decide(UUID(route["action_id"]), accept=True, via="console")
    assert accepted["outcome"] == "accepted"
    assert world.backlog.get(world.item_id)["section"]["key"] == "issues"
    with pytest.raises(ProjectConflict, match="already been decided"):
        world.toolbox.decide(UUID(route["action_id"]), accept=False, via="console")
    text, _ = world.call(run_id, token, "start_item", item=item, reason="You asked for it.")
    assert text.startswith("Not done:") and "confirm this kind of action yourself" in text
    assert world.runs.listing()["items"] == []
    (start,) = world.toolbox.pending()
    rejected = world.toolbox.decide(UUID(start["action_id"]), accept=False, via="chat")
    assert rejected["outcome"] == "rejected" and world.runs.listing()["items"] == []
    world.call(run_id, token, "start_item", item=item, reason="You asked again.")
    (start,) = world.toolbox.pending()
    world.toolbox.decide(UUID(start["action_id"]), accept=True, via="console")
    (run,) = world.runs.listing()["items"]
    assert run["kind"] == "deliver_issue" and run["state"] == "queued"
    assert world.toolbox.pending() == []
    # Both read what was decided in the conversation.
    notes = [m["text"] for m in world.conversation.listing()["items"] if m["role"] == "platform"]
    assert notes == [
        "The administrator accepted the supervisor's proposal: Route “Signing in fails with "
        "an expired cookie” to Issues.",
        "The administrator rejected the supervisor's proposal: Start work on “Signing in "
        "fails with an expired cookie” (falcone-workflow).",
        "The administrator accepted the supervisor's proposal: Start work on “Signing in "
        "fails with an expired cookie” (falcone-workflow).",
    ]
    # What was accepted but can no longer be done is said, and recorded as failed.
    world.call(run_id, token, "start_item", item=item, reason="Once more.")
    (again,) = world.toolbox.pending()
    with pytest.raises(ProjectConflict, match="already under way"):
        world.toolbox.decide(UUID(again["action_id"]), accept=True, via="console")
    assert world.actions(outcome="failed")[0].tool == "start_item"
    record = world.toolbox.record()["items"]
    assert [entry["outcome"] for entry in record][:2] == ["failed", "accepted"]
    assert all(entry["outcome"] != "read" for entry in record)


async def test_proposed_items_wait_in_the_backlog_and_are_not_repeated(world: Any) -> None:
    world.account("claude one", "claude")
    run_id, token = await world.running_turn()
    text, error = world.call(
        run_id,
        token,
        "propose_items",
        project="falcone-workflow",
        items=[
            {
                "title": "Show why a sign-in failed",
                "section": "enhancement",
                "reason": "You said users cannot tell why.",
            },
            {"title": "signing in fails with an expired cookie", "reason": "From issue 11."},
            {"title": "Decide what to do with the old importer", "reason": "No section fits."},
        ],
    )
    assert not error and text.startswith("Done: Propose 3 items for falcone-workflow")
    result = json.loads(text[text.index("{") :])
    assert [made["title"] for made in result["proposed"]] == [
        "Show why a sign-in failed",
        "Decide what to do with the old importer",
    ]
    assert result["already_in_the_backlog"] == ["signing in fails with an expired cookie"]
    listing = world.backlog.listing(project="falcone-workflow", search="sign-in failed")
    (made,) = listing["items"]
    assert (made["state"], made["source"], made["proposed_by"], made["routed_by"]) == (
        "proposed",
        "manual",
        "supervisor",
        "supervisor",
    )
    assert made["section"]["key"] == "enhancement"
    assert made["routing_reason"] == "You said users cannot tell why."
    (unplaced,) = world.backlog.listing(search="old importer")["items"]
    assert unplaced["section"] is None and unplaced["routed_by"] is None
    for wrong in (
        {"items": []},
        {"items": [{"title": "No reason"}]},
        {"items": [{"title": "Elsewhere", "section": "nearest", "reason": "x"}]},
        {"project": "nothing", "items": [{"title": "T", "reason": "x"}]},
    ):
        arguments = {"project": "falcone-workflow", **wrong}
        text, error = world.call(run_id, token, "propose_items", **arguments)
        assert error and text.startswith("Refused:"), text


async def test_what_is_switched_off_is_not_offered_and_past_the_limit_the_next_waits(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.account("claude one", "claude")
    current = world.definitions.current()
    world.definitions.save(
        prompt=current["prompt"],
        skills=current["skills"],
        tools={"start_item": {"enabled": False, "mode": "confirm"}},
        providers=current["providers"],
        account_id=None,
        exclusive=False,
        models={},
        note="No starting",
    )
    run_id, token = await world.running_turn()
    listed = {tool["name"] for tool in world.rpc(run_id, token, "tools/list")["tools"]}
    assert "start_item" not in listed and "route_item" in listed
    argv = world.job()["commands"][0]["argv"]
    assert "mcp__platform__start_item" not in argv and "mcp__platform__route_item" in argv
    text, error = world.call(run_id, token, "start_item", item=str(world.item_id), reason="x")
    assert error and text == "Refused: you have no such tool."
    monkeypatch.setattr(tools_module, "AUTOMATIC_PER_TURN", 1)
    proposal = {"project": "falcone-workflow", "items": [{"title": "One", "reason": "x"}]}
    assert world.call(run_id, token, "propose_items", **proposal)[0].startswith("Done:")
    proposal["items"][0]["title"] = "Two"
    text, _ = world.call(run_id, token, "propose_items", **proposal)
    assert text.startswith("Not done:") and "limit of actions" in text
    assert world.backlog.listing(search="Two")["total"] == 0


def test_an_import_keeps_the_supervisors_section_unless_a_label_decides(world: Any) -> None:
    world.backlog.route_as_supervisor(world.item_id, "issues", "A defect.")

    def imported(labels: tuple[str, ...]) -> dict[str, Any]:
        issue = Issue(
            number=11,
            title="Signing in fails with an expired cookie",
            url=f"https://github.com/{REPOSITORY}/issues/11",
            labels=labels,
            updated_at=datetime(2026, 11, 13, tzinfo=UTC),
        )
        world.backlog.apply_import("falcone-workflow", [issue], complete=True)
        return world.backlog.get(world.item_id)

    kept = imported(())
    assert kept["section"]["key"] == "issues" and kept["routed_by"] == "supervisor"
    with world.sessions.begin() as session:
        section = session.query(ProjectSection).filter_by(key="enhancement").one()
        section.labels = ["enhancement"]
    moved = imported(("enhancement",))
    assert moved["section"]["key"] == "enhancement" and moved["routed_by"] == "label"
