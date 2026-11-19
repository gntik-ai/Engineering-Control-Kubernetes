"""The gateway: the client of an agent in a sandbox, paid by an API key it never holds."""

# ruff: noqa: S105, S108

from __future__ import annotations

import copy
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from starlette.testclient import TestClient

from engineering_control.config import Settings
from engineering_control.dashboard.workspace_metrics import WorkspaceMetrics
from engineering_control.engine import validate
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import parse
from engineering_control.engine.records import FlowRecords
from engineering_control.gateway import grants
from engineering_control.gateway.service import Gateway, create_app
from engineering_control.models.sealing import seal
from engineering_control.sandbox import deliver
from engineering_control.sandbox.pods import GATEWAY_LABEL
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    HEAD,
    MAKER_DONE,
    _implemented,
    _log,
    _prepared,
    _ran,
    world,
)

SEALING = b"s" * 48
GRANTS = b"g" * 48
AT = "http://10.43.7.9:8794"
OPENAI_KEY = "sk-openai-" + "k" * 40
ANTHROPIC_KEY = "sk-ant-" + "k" * 40


def _answer(
    status: int, body: bytes | dict[str, Any], headers: dict[str, str] | None = None
) -> httpx.Response:
    """What a provider answers, as it comes over the wire: a stream nobody has read."""
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    kind = {} if isinstance(body, bytes) else {"content-type": "application/json"}
    return httpx.Response(status, headers={**kind, **(headers or {})}, stream=httpx.ByteStream(raw))


# --- grants ----------------------------------------------------------------------------------


def test_a_grant_names_one_account_and_one_step_and_only_the_platform_can_issue_it() -> None:
    account, step = uuid4(), uuid4()
    grant = grants.issue(GRANTS, account_id=account, step_id=step)
    assert grant.startswith("ecg1.") and OPENAI_KEY not in grant
    assert grants.read(GRANTS, grant) == (account, step)
    assert grants.read(GRANTS, f"  {grant}\n") == (account, step)
    with pytest.raises(grants.GrantError, match="not issued by this platform"):
        grants.read(b"x" * 48, grant)
    # A grant for another step is another grant: nothing of one can be put into another.
    other = grants.issue(GRANTS, account_id=account, step_id=uuid4())
    mixed = ".".join([*grant.split(".")[:2], other.split(".")[2], *grant.split(".")[3:]])
    with pytest.raises(grants.GrantError, match="not issued by this platform"):
        grants.read(GRANTS, mixed)
    later = ".".join([*grant.split(".")[:3], "9999999999", grant.split(".")[4]])
    with pytest.raises(grants.GrantError, match="not issued by this platform"):
        grants.read(GRANTS, later)
    with pytest.raises(grants.GrantError, match="time is over"):
        grants.read(GRANTS, grant, now=4_000_000_000)
    past = grants.issue(GRANTS, account_id=account, step_id=step, seconds=-5)
    with pytest.raises(grants.GrantError, match="time is over"):
        grants.read(GRANTS, past)
    for garbage in ("", "sk-ant-api03-abc", "ecg1.a.b.c", "ecg2." + grant[5:], OPENAI_KEY):
        with pytest.raises(grants.GrantError, match="not a grant"):
            grants.read(GRANTS, garbage)


# --- the client's set-up ----------------------------------------------------------------------


def test_a_client_paid_by_a_key_is_given_the_gateway_and_a_grant_and_no_sign_in() -> None:
    gateway = {"url": AT, "grant": "ecg1.grant"}
    claude = deliver.implement_job("claude", 3, "Do it", "A change")
    assert claude["env_files"] == {"CLAUDE_CODE_OAUTH_TOKEN": "/run/account/token"}
    through = deliver.through_gateway(claude, "claude", gateway)
    assert "env_files" not in through and "seed" not in through
    agent, commit = through["commands"][0], through["commands"][1]
    assert agent["name"] == "agent" and agent["argv"] == claude["commands"][0]["argv"]
    assert agent["stdin"] == "Do it"
    assert agent["env"]["ANTHROPIC_BASE_URL"] == f"{AT}/anthropic"
    assert agent["env"]["ANTHROPIC_AUTH_TOKEN"] == "ecg1.grant"
    assert agent["env"]["NO_PROXY"] == agent["env"]["no_proxy"] == "10.43.7.9"
    assert agent["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    # Only the client is told: what commits the change knows nothing of the gateway.
    assert commit == claude["commands"][1] and "ecg1.grant" not in json.dumps(commit)
    assert through["files"] == []

    codex = deliver.implement_job("chatgpt", 3, "Do it", "A change")
    assert codex["seed"][0]["to"].endswith("/auth.json")
    through = deliver.through_gateway(codex, "chatgpt", gateway)
    assert "env_files" not in through and "seed" not in through
    agent = through["commands"][0]
    # Its set-up is read from a folder without a sign-in, and names the gateway as the
    # provider, with the grant read from the environment where a key would be.
    assert agent["env"]["CODEX_HOME"] == "/tmp"
    assert agent["env"][deliver.GATEWAY_GRANT] == "ecg1.grant"
    assert agent["env"]["NO_PROXY"] == "10.43.7.9"
    assert [file["path"] for file in through["files"]] == [
        "/tmp/answer-schema.json",
        "/tmp/config.toml",
    ]
    said = through["files"][1]["content"]
    assert 'model_provider = "gateway"' in said and "[model_providers.gateway]" in said
    assert f'base_url = "{AT}/openai/v1"' in said
    assert f'env_key = "{deliver.GATEWAY_GRANT}"' in said and 'wire_api = "responses"' in said
    assert "ecg1.grant" not in said
    assert [c["name"] for c in through["commands"]] == [c["name"] for c in codex["commands"]]
    assert "CODEX_HOME" not in (through["commands"][1].get("env") or {})


def test_every_task_an_agent_works_in_may_be_paid_by_a_key_and_nothing_else_may() -> None:
    for task in TASKS.values():
        named = [param for param in task.params if param.name == "paid"]
        assert bool(named) == bool(task.account), task.key
        if named:
            assert named[0].kind == "choice" and named[0].default == "subscription"
            assert named[0].choices == ("subscription", "api")
    raw = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    by_id = {task["id"]: task for task in raw["tasks"]}
    by_id["implement"]["with"]["paid"] = "api"
    assert validate.errors(parse(raw)) == []
    by_id["implement"]["with"]["paid"] = "credit card"
    assert validate.errors(parse(raw)) != []


# --- a run -----------------------------------------------------------------------------------


def _paid_by_key(world: Any, *tasks: str, **more: Any) -> None:  # noqa: F811
    raw = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    for task in raw["tasks"]:
        if task["id"] in tasks:
            task["with"] = {**(task.get("with") or {}), "paid": "api", **more}
    assert validate.errors(parse(raw)) == []
    flows = FlowRecords(world.sessions)
    flows.save_draft("deliver-issue", raw)
    flows.publish("deliver-issue")


def _key(world: Any, name: str, provider: str, key: str) -> UUID:  # noqa: F811
    account_id = UUID(world.accounts.create(name=name, provider=provider)["id"])
    world.accounts.key_stored(account_id, seal(SEALING, account_id, key))
    return account_id


def _with_gateway(world: Any) -> None:  # noqa: F811
    world.launcher.grant_key = GRANTS
    world.launcher.gateway_url = AT


CODEX_USED = (
    '{"type":"turn.completed","usage":{"input_tokens":1000,"cached_input_tokens":400,'
    '"output_tokens":200}}\n'
)


@pytest.mark.asyncio
async def test_a_step_paid_by_a_key_takes_an_account_with_one_and_reaches_only_the_gateway(
    world: Any,  # noqa: F811
) -> None:
    _paid_by_key(world, "implement", models={"chatgpt": {"model": "gpt-5.5"}})
    _with_gateway(world)
    world.account("ChatGPT one", "chatgpt")
    claude = world.account("Claude one", "claude")
    openai = _key(world, "OpenAI key", "openai", OPENAI_KEY)
    world.accounts.update(openai, prices={"gpt-5.5": {"input": 2, "output": 10, "cached": 0.5}})
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.step("prepare", _prepared())

    # The agent that implements: Codex, but paid by OpenAI's key and not the subscription.
    await world.launcher.tick()
    pod = world.fake.only_pod()
    assert pod["metadata"]["labels"]["engineering-control/step"] == "implement"
    assert pod["metadata"]["labels"]["engineering-control/gateway"] == "client"
    assert GATEWAY_LABEL == {"engineering-control/gateway": "client"}
    # No sign-in is mounted and no volume of an account's: there is nothing to mount.
    assert "account" not in {volume["name"] for volume in pod["spec"]["volumes"]}
    assert not [claim for claim in world.fake.claims if claim.startswith("account-")]
    job = world.job()
    assert "seed" not in job and "env_files" not in job
    agent = job["commands"][0]
    assert agent["argv"][:2] == ["codex", "exec"] and "gpt-5.5" in agent["argv"]
    assert agent["env"]["CODEX_HOME"] == "/tmp" and agent["env"]["NO_PROXY"] == "10.43.7.9"
    grant = agent["env"][deliver.GATEWAY_GRANT]
    step = world.run(run["id"])["steps"][-1]
    assert grants.read(GRANTS, grant) == (openai, UUID(step["id"]))
    # The key itself is nowhere in what the sandbox is given.
    assert OPENAI_KEY not in json.dumps(pod)
    # The step is the client's, whoever pays: the rules go by whose model works.
    assert (step["provider"], step["account_id"]) == ("chatgpt", str(openai))
    assert step["input"]["paid"] == "api"

    # While the step runs, the gateway sends what the client asks on with the real key.
    asked: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        if request.url.path.endswith("/unreachable"):
            raise httpx.ConnectError("no route")
        if request.url.path.endswith("/limited"):
            return _answer(
                429,
                {"error": {"message": "slow down"}},
                {"retry-after": "7", "x-should-retry": "true"},
            )
        return _answer(
            200,
            b'event: one\ndata: {"a":1}\n\nevent: two\ndata: {"a":2}\n\n',
            {"content-type": "text/event-stream", "x-request-id": "req_1"},
        )

    def gateway() -> TestClient:
        return TestClient(
            create_app(
                Gateway(
                    world.sessions,
                    sealing_key=SEALING,
                    grant_key=GRANTS,
                    http=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
                )
            )
        )

    with gateway() as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        body = {"model": "gpt-5.5", "input": "hello", "stream": True}
        answer = client.post(
            "/openai/v1/responses?beta=true",
            json=body,
            headers={
                "Authorization": f"Bearer {grant}",
                "OpenAI-Beta": "responses=v1",
                "Cookie": "session=1",
            },
        )
        assert answer.status_code == 200 and answer.headers["x-request-id"] == "req_1"
        assert answer.headers["content-type"].startswith("text/event-stream")
        assert answer.text == 'event: one\ndata: {"a":1}\n\nevent: two\ndata: {"a":2}\n\n'
        [sent] = asked
        assert str(sent.url) == "https://api.openai.com/v1/responses?beta=true"
        assert sent.headers["authorization"] == f"Bearer {OPENAI_KEY}"
        assert sent.headers["openai-beta"] == "responses=v1" and "cookie" not in sent.headers
        assert json.loads(sent.content) == body and grant not in str(sent.headers)

        # What the provider refuses comes back as it said it, with what tells the client
        # when to try again.
        limited = client.post("/openai/v1/limited", headers={"Authorization": f"Bearer {grant}"})
        assert limited.status_code == 429 and limited.headers["retry-after"] == "7"
        assert limited.headers["x-should-retry"] == "true"
        assert limited.json() == {"error": {"message": "slow down"}}
        down = client.post("/openai/v1/unreachable", headers={"x-api-key": grant})
        assert (
            down.status_code == 502 and "could not reach openai" in down.json()["error"]["message"]
        )

        # The grant is for OpenAI's key: not for another provider, and no other address
        # is reachable at all.
        asked.clear()
        wrong = client.post("/anthropic/v1/messages", headers={"x-api-key": grant}, json={})
        assert wrong.status_code == 403 and wrong.json()["type"] == "error"
        assert wrong.json()["error"]["type"] == "permission_error"
        assert client.post("/google/v1/x", headers={"x-api-key": grant}).status_code == 404
        for shown in ("", "Bearer nothing", f"Bearer {OPENAI_KEY}", f"Basic {grant}"):
            refused = client.post("/openai/v1/responses", headers={"Authorization": shown})
            assert refused.status_code == 401, shown
            assert refused.json()["error"]["type"] == "invalid_request_error"
        stolen = grants.issue(GRANTS, account_id=claude, step_id=UUID(step["id"]))
        other = client.post("/openai/v1/responses", headers={"Authorization": f"Bearer {stolen}"})
        assert other.status_code == 401
        assert asked == []

    # The step ends: what Codex used is counted at the account's prices for the model.
    world.fake.finish(
        _log(
            [
                _ran("agent", CODEX_USED),
                _ran("answer", json.dumps(MAKER_DONE)),
                _ran("commit", f"{HEAD}\n"),
            ]
        )
    )
    await world.launcher.tick()
    done = next(s for s in world.run(run["id"])["steps"] if s["name"] == "implement")
    assert done["state"] == "succeeded"
    assert done["usage"] == {
        "tokens": 800,
        "input": 600,
        "output": 200,
        "cached": 400,
        "provider": "openai",
        "account": "OpenAI key",
        "cost": round((600 * 2 + 200 * 10 + 400 * 0.5) / 1_000_000, 6),
    }
    assert world.run(run["id"])["usage"]["cost"] == done["usage"]["cost"]

    # With the step, the grant has ended, whatever its time says.
    with gateway() as client:
        ended = client.post("/openai/v1/responses", headers={"Authorization": f"Bearer {grant}"})
        assert ended.status_code == 401 and "not running" in ended.json()["error"]["message"]
        assert asked == []

    # The review was not set to be paid by a key: Claude's subscription, mounted as ever.
    await world.launcher.tick()
    pod = world.fake.only_pod()
    assert pod["metadata"]["labels"]["engineering-control/step"] == "review"
    assert "engineering-control/gateway" not in pod["metadata"]["labels"]
    assert "account" in {volume["name"] for volume in pod["spec"]["volumes"]}
    assert world.job()["env_files"] == {"CLAUDE_CODE_OAUTH_TOKEN": "/run/account/token"}
    review = world.run(run["id"])["steps"][-1]
    assert (review["provider"], review["account_id"]) == ("claude", str(claude))
    assert "paid" not in review["input"]


def _claude_used(cost: float) -> str:
    events = [
        {"type": "system", "subtype": "init"},
        {
            "type": "result",
            "subtype": "success",
            "result": "done",
            "structured_output": MAKER_DONE,
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 50,
                "cache_read_input_tokens": 900,
                "output_tokens": 30,
            },
            "total_cost_usd": cost,
        },
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


@pytest.mark.asyncio
async def test_claude_paid_by_a_key_says_what_it_cost_and_on_a_subscription_that_is_no_price(
    world: Any,  # noqa: F811
) -> None:
    _paid_by_key(world, "implement", providers=["claude"])
    _with_gateway(world)
    world.account("Claude one", "claude")
    world.account("ChatGPT one", "chatgpt")
    anthropic = _key(world, "Anthropic key", "anthropic", ANTHROPIC_KEY)
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.step("prepare", _prepared())
    await world.launcher.tick()
    pod, job = world.fake.only_pod(), world.job()
    assert pod["metadata"]["labels"]["engineering-control/gateway"] == "client"
    assert "account" not in {volume["name"] for volume in pod["spec"]["volumes"]}
    agent = job["commands"][0]
    assert agent["argv"][0] == "claude" and "env_files" not in job
    assert agent["env"]["ANTHROPIC_BASE_URL"] == f"{AT}/anthropic"
    step = world.run(run["id"])["steps"][-1]
    assert grants.read(GRANTS, agent["env"]["ANTHROPIC_AUTH_TOKEN"]) == (
        anthropic,
        UUID(step["id"]),
    )
    assert ANTHROPIC_KEY not in json.dumps(pod)

    # Claude Code shows the grant where a sign-in would be: Authorization: Bearer.
    asked: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return _answer(200, {"type": "message"})

    app = create_app(
        Gateway(
            world.sessions,
            sealing_key=SEALING,
            grant_key=GRANTS,
            http=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
        )
    )
    with TestClient(app) as client:
        grant = agent["env"]["ANTHROPIC_AUTH_TOKEN"]
        answer = client.post(
            "/anthropic/v1/messages?beta=true",
            json={"model": "claude-opus-5-5", "messages": []},
            headers={
                "Authorization": f"Bearer {grant}",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "claude-code-20250219",
            },
        )
        assert answer.status_code == 200 and answer.json() == {"type": "message"}
        [sent] = asked
        assert str(sent.url) == "https://api.anthropic.com/v1/messages?beta=true"
        assert sent.headers["x-api-key"] == ANTHROPIC_KEY and "authorization" not in sent.headers
        assert sent.headers["anthropic-version"] == "2023-06-01"
        assert sent.headers["anthropic-beta"] == "claude-code-20250219"
        wrong = client.post("/openai/v1/responses", headers={"Authorization": f"Bearer {grant}"})
        assert wrong.status_code == 403 and len(asked) == 1

    world.fake.finish(_log([_ran("agent", _claude_used(0.4321)), _ran("commit", f"{HEAD}\n")]))
    await world.launcher.tick()
    done = next(s for s in world.run(run["id"])["steps"] if s["name"] == "implement")
    assert done["usage"] == {
        "tokens": 180,
        "input": 150,
        "output": 30,
        "cached": 900,
        "provider": "anthropic",
        "account": "Anthropic key",
        "cost": 0.4321,
    }

    # The review is ChatGPT's, on its subscription: what the client reckons is no price.
    await world.launcher.tick()
    assert world.fake.only_pod()["metadata"]["labels"]["engineering-control/step"] == "review"
    assert world.run(run["id"])["steps"][-1]["provider"] == "chatgpt"


@pytest.mark.asyncio
async def test_what_a_subscription_reports_as_cost_is_not_counted(world: Any) -> None:  # noqa: F811
    raw = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    for task in raw["tasks"]:
        if task["id"] == "implement":
            task["with"] = {"providers": ["claude"]}
    flows = FlowRecords(world.sessions)
    flows.save_draft("deliver-issue", raw)
    flows.publish("deliver-issue")
    _with_gateway(world)
    world.account("Claude one", "claude")
    _key(world, "Anthropic key", "anthropic", ANTHROPIC_KEY)
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.step("prepare", _prepared())
    await world.launcher.tick()
    pod = world.fake.only_pod()
    assert "engineering-control/gateway" not in pod["metadata"]["labels"]
    assert "ANTHROPIC_BASE_URL" not in world.job()["commands"][0]["env"]
    world.fake.finish(_log([_ran("agent", _claude_used(0.4321)), _ran("commit", f"{HEAD}\n")]))
    await world.launcher.tick()
    done = next(s for s in world.run(run["id"])["steps"] if s["name"] == "implement")
    assert done["usage"] == {"tokens": 180, "input": 150, "output": 30, "cached": 900}
    assert world.run(run["id"])["usage"]["cost"] == 0
    day = WorkspaceMetrics(world.sessions).daily("falcone-workflow", 7)["series"][-1]
    assert float(day["estimated_eur"]) * float(day["usd_per_eur"]) == pytest.approx(
        0.4321, abs=0.000001
    )


@pytest.mark.asyncio
async def test_without_an_account_with_a_key_the_step_waits_and_without_a_gateway_it_fails(
    world: Any,  # noqa: F811
) -> None:
    _paid_by_key(world, "implement")
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    # A key of another company's pays for neither client.
    _key(world, "Google key", "google", "AIza" + "k" * 35)
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.step("prepare", _prepared())
    await world.launcher.tick()
    assert world.fake.pods == {}
    waiting = world.run(run["id"])["steps"][-1]
    assert waiting["name"] == "implement" and waiting["state"] == "pending"
    assert waiting["detail"].startswith("Waiting for an account with an API key")

    # The key is there, but this installation has no gateway: the run says so and ends.
    _key(world, "Anthropic key", "anthropic", ANTHROPIC_KEY)
    await world.launcher.tick()
    assert world.fake.pods == {}
    ended = world.run(run["id"])
    assert ended["state"] == "failed"
    assert "has no gateway" in ended["steps"][-1]["detail"]


def test_the_gateway_listens_where_it_is_told_and_never_on_every_interface_by_itself() -> None:
    settings = Settings(
        network_boundary="cluster",
        gateway_host="0.0.0.0",  # noqa: S104 - inside the cluster's own boundary
        gateway_url="http://engineering-control-gateway:8794",
    )
    assert settings.gateway_port == 8794
    assert Settings().gateway_url is None and Settings().gateway_host == "127.0.0.1"
    with pytest.raises(ValueError, match="gateway cannot bind"):
        Settings(gateway_host="0.0.0.0")  # noqa: S104 - refused outside a cluster
    with pytest.raises(ValueError):
        Settings(gateway_url="https://api.openai.com")


def test_a_gateway_without_its_keys_refuses_everything(world: Any) -> None:  # noqa: F811
    app = create_app(Gateway(world.sessions, sealing_key=None, grant_key=None))
    with TestClient(app) as client:
        grant = grants.issue(GRANTS, account_id=uuid4(), step_id=uuid4())
        refused = client.post("/anthropic/v1/messages", headers={"x-api-key": grant})
        assert refused.status_code == 503 and refused.json()["error"]["type"] == "api_error"
    assert _implemented() != ""
