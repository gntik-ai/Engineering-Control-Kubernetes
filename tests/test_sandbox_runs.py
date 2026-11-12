"""Runs in sandboxes: the pod a step gets, the launcher's passes, what a result means."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.cluster import Cluster
from engineering_control.dashboard.accounts import Accounts
from engineering_control.dashboard.project_records import ProjectConflict, ProjectInvalid
from engineering_control.db.base import Base
from engineering_control.db.models import ProviderAccount, SandboxStep
from engineering_control.sandbox import pods, steps
from engineering_control.sandbox.launcher import Launcher, pod_progress
from engineering_control.sandbox.runs import Runs

NAMESPACE = "engineering-control-sandboxes"
SANDBOXES = pods.Sandboxes(
    namespace=NAMESPACE,
    image="ghcr.io/gntik-ai/engineering-control-kubernetes-sandbox:abc123",
    pull_secret="engineering-control-registry",  # noqa: S106 - a Secret's name
)
NOW = datetime(2026, 11, 12, 20, 0, tzinfo=UTC)


def _result(*commands: dict[str, Any]) -> str:
    encoded = base64.b64encode(json.dumps({"commands": list(commands)}).encode()).decode()
    return f"--- check\nsome output\n\n{steps.MARKER}{encoded}\n"


def _command(**fields: Any) -> dict[str, Any]:
    return {
        "name": "check",
        "exit_code": 0,
        "timed_out": False,
        "seconds": 2.5,
        "stdout": "",
        "stderr": "",
        **fields,
    }


class FakeCluster:
    """Pods and volume claims of the sandbox namespace, as the launcher sees them."""

    def __init__(self) -> None:
        self.pods: dict[str, dict[str, Any]] = {}
        self.logs: dict[str, str] = {}
        self.claims: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.refuse: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.refuse is not None:
            return httpx.Response(self.refuse, json={"message": "pods is forbidden"})
        path = request.url.path.removeprefix(f"/api/v1/namespaces/{NAMESPACE}/")
        kind, _, rest = path.partition("/")
        store = self.pods if kind == "pods" else self.claims
        if request.method == "POST":
            body = json.loads(request.content)
            name = body["metadata"]["name"]
            if name in store:
                return httpx.Response(409, json={})
            store[name] = {**body, "status": {"phase": "Pending"}}
            return httpx.Response(201, json={})
        if rest.endswith("/log"):
            name = rest.removesuffix("/log")
            return httpx.Response(200 if name in self.pods else 404, text=self.logs.get(name, ""))
        if request.method == "GET":
            return httpx.Response(200, json=store[rest]) if rest in store else httpx.Response(404)
        if request.method == "DELETE":
            self.deleted.append(rest)
            return httpx.Response(200 if store.pop(rest, None) else 404, json={})
        return httpx.Response(405)

    def only_pod(self) -> dict[str, Any]:
        assert len(self.pods) == 1, list(self.pods)
        return next(iter(self.pods.values()))

    def finish(self, log: str, phase: str = "Succeeded") -> None:
        pod = self.only_pod()
        pod["status"] = {"phase": phase}
        self.logs[pod["metadata"]["name"]] = log


@pytest.fixture
def world(tmp_path: Path) -> Any:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'runs.db'}")
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

    class World:
        accounts = Accounts(sessions)
        runs = Runs(sessions)
        launcher = Launcher(runs, cluster, SANDBOXES)

        def __init__(self) -> None:
            self.fake = fake
            self.sessions = sessions

        def account(self, name: str, provider: str, **fields: Any) -> UUID:
            created = self.accounts.create(name=name, provider=provider)
            account_id = UUID(created["id"])
            self.accounts.signed_in(account_id, None)
            if fields:
                with sessions.begin() as session:
                    record = session.get(ProviderAccount, account_id)
                    assert record is not None
                    for key, value in fields.items():
                        setattr(record, key, value)
            return account_id

        def state(self, account_id: UUID) -> dict[str, Any]:
            return self.accounts.get(account_id)

    yield World()
    engine.dispose()


# --- the pod ----------------------------------------------------------------------------


def test_a_sandbox_is_unprivileged_has_no_identity_and_runs_on_the_sandbox_node() -> None:
    run_id = uuid4()
    account = pods.AccountMount(uuid4(), keeps_home=True)
    pod = pods.step_pod(
        SANDBOXES,
        run_id=run_id,
        step="check",
        position=1,
        attempt=2,
        job={"commands": [{"name": "check", "argv": ["true"]}]},
        egress="internet",
        account=account,
    )
    spec = pod["spec"]
    assert pod["metadata"]["namespace"] == NAMESPACE
    assert pod["metadata"]["name"] == f"run-{run_id.hex[:16]}-1-2"
    assert pod["metadata"]["labels"]["engineering-control/egress"] == "internet"
    assert pod["metadata"]["labels"]["engineering-control/run"] == str(run_id)
    # What the "restricted" pod security level asks, and more.
    assert spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10002,
        "runAsGroup": 10002,
        "fsGroup": 10002,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    (container,) = spec["containers"]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert spec["restartPolicy"] == "Never" and spec["activeDeadlineSeconds"] > 0
    assert "hostNetwork" not in spec and "hostPID" not in spec
    assert not any("hostPath" in volume for volume in spec["volumes"])
    # Only the node reserved for sandboxes, which it alone tolerates.
    assert spec["nodeSelector"] == {"engineering-control/sandbox": "true"}
    assert spec["tolerations"] == [
        {
            "key": "engineering-control/sandbox",
            "operator": "Equal",
            "value": "true",
            "effect": "NoSchedule",
        }
    ]
    assert container["image"] == SANDBOXES.image and container["args"] == ["sandbox-step"]
    assert container["resources"]["limits"] == {"cpu": "2", "memory": "4Gi"}
    # The job is the only thing in the environment, and it names no secret.
    assert [variable["name"] for variable in container["env"]] == ["SANDBOX_JOB"]
    assert json.loads(container["env"][0]["value"])["commands"][0]["argv"] == ["true"]
    mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
    assert mounts["account"] == {"name": "account", "mountPath": "/run/account", "readOnly": True}
    assert mounts["account-home"]["mountPath"] == "/account-home"
    volumes = {volume["name"]: volume for volume in spec["volumes"]}
    assert volumes["account"]["secret"] == {
        "secretName": f"account-{account.account_id}",
        "defaultMode": 0o440,
    }
    assert volumes["account-home"]["persistentVolumeClaim"]["claimName"] == account.home_claim
    assert "emptyDir" in volumes["workspace"] and "emptyDir" in volumes["home"]


def test_a_step_without_an_account_mounts_none_and_a_run_volume_when_given() -> None:
    run_id = uuid4()
    pod = pods.step_pod(
        SANDBOXES,
        run_id=run_id,
        step="tests",
        position=3,
        attempt=1,
        job={"commands": []},
        egress="none",
        workspace=pods.workspace_claim(run_id),
    )
    names = {volume["name"] for volume in pod["spec"]["volumes"]}
    assert names == {"home", "tmp", "workspace"}
    workspace = next(v for v in pod["spec"]["volumes"] if v["name"] == "workspace")
    assert workspace["persistentVolumeClaim"]["claimName"] == f"run-{run_id.hex[:16]}"
    assert "engineering-control/account" not in pod["metadata"]["labels"]
    claim = pods.claim("run-x", namespace=NAMESPACE, gibibytes=5, labels={"a": "b"})
    assert claim["spec"] == {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "5Gi"}},
    }
    assert "storageClassName" not in claim["spec"]


# --- jobs and results -------------------------------------------------------------------


def test_the_check_job_names_where_the_sign_in_is_and_never_holds_it() -> None:
    claude = steps.check_job("claude", 3)
    assert claude["env_files"] == {"CLAUDE_CODE_OAUTH_TOKEN": "/run/account/token"}
    (command,) = claude["commands"]
    assert command["argv"][0] == "claude" and "--print" in command["argv"]
    assert command["argv"][command["argv"].index("--tools") + 1] == "", "no tools at all"
    codex = steps.check_job("chatgpt", 3)
    assert codex["seed"] == [
        {"from": "/run/account/auth.json", "to": "/account-home/auth.json", "generation": 3}
    ]
    (command,) = codex["commands"]
    assert command["argv"][:2] == ["codex", "exec"] and command["env"] == {
        "CODEX_HOME": "/account-home"
    }
    assert command["argv"][command["argv"].index("--sandbox") + 1] == "read-only"
    with pytest.raises(ValueError, match="no client"):
        steps.check_job("glm", 1)


def test_a_result_is_read_from_the_last_marker_and_garbage_is_no_result() -> None:
    log = "earlier " + steps.MARKER + "not-base64\n" + _result(_command(stdout="ready"))
    assert steps.parse_result(log) == [_command(stdout="ready")]
    assert steps.parse_result("no marker here") is None
    assert steps.parse_result(steps.MARKER + "bm90IGpzb24=") is None
    assert steps.parse_result(steps.MARKER + base64.b64encode(b'{"commands": 3}').decode()) is None


@pytest.mark.parametrize(
    ("command", "outcome", "account_state"),
    [
        (_command(stdout="Ready\n"), "ready", "ready"),
        (
            _command(exit_code=1, stderr="Invalid API key · Please run /login"),
            "sign_in_required",
            "sign_in_required",
        ),
        (
            _command(exit_code=1, stdout="OAuth token has expired"),
            "sign_in_required",
            "sign_in_required",
        ),
        (
            _command(
                exit_code=1, stderr="You've hit your usage limit. Try again in 3 hours 20 minutes."
            ),
            "limited",
            "limited",
        ),
        (_command(exit_code=1, stderr="HTTP 429 Too Many Requests"), "limited", "limited"),
        (_command(exit_code=1, stderr="segmentation fault"), "failed", None),
        (_command(exit_code=0, stdout="I cannot do that"), "failed", None),
        (_command(exit_code=-15, timed_out=True), "timed_out", None),
        (
            _command(name="setup", exit_code=126, stderr="No such file"),
            "sign_in_required",
            "sign_in_required",
        ),
    ],
)
def test_what_a_check_says_about_the_account(
    command: dict[str, Any], outcome: str, account_state: str | None
) -> None:
    verdict = steps.interpret_check([command], NOW)
    assert (verdict.outcome, verdict.account_state) == (outcome, account_state)
    assert verdict.succeeded is (outcome == "ready")


def test_a_limit_lasts_as_long_as_the_provider_says_or_an_hour() -> None:
    said = steps.interpret_check(
        [_command(exit_code=1, stderr="usage limit reached, try again in 3 hours 20 minutes")], NOW
    )
    assert said.limited_until == NOW + timedelta(hours=3, minutes=20)
    silent = steps.interpret_check([_command(exit_code=1, stderr="rate limit")], NOW)
    assert silent.limited_until == NOW + timedelta(hours=1)
    absurd = steps.interpret_check(
        [_command(exit_code=1, stderr="usage limit, resets in 400 days")], NOW
    )
    assert absurd.limited_until == NOW + timedelta(hours=1)
    assert steps.interpret_check(None, NOW).outcome == "no_result"


def test_how_the_cluster_describes_a_pod_becomes_one_of_four_states() -> None:
    assert pod_progress({"status": {"phase": "Succeeded"}}) == ("succeeded", None)
    assert pod_progress({"status": {"phase": "Running"}}) == ("running", None)
    assert pod_progress({"status": {"phase": "Failed", "reason": "DeadlineExceeded"}}) == (
        "failed",
        "The step ran longer than it is allowed to.",
    )
    assert pod_progress({"status": {"phase": "Failed", "reason": "Evicted"}})[1] == "Evicted"
    waiting = {"state": {"waiting": {"reason": "ImagePullBackOff"}}}
    assert pod_progress({"status": {"phase": "Pending", "containerStatuses": [waiting]}}) == (
        "starting",
        "The sandbox image could not be pulled.",
    )
    unscheduled = {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}
    assert pod_progress({"status": {"phase": "Pending", "conditions": [unscheduled]}})[1] == (
        "Waiting for room on the sandbox node."
    )
    assert pod_progress({}) == ("starting", None)


# --- the launcher -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trying_an_account_runs_one_sandbox_and_records_what_it_learned(world: Any) -> None:
    account_id = world.account("Claude one", "claude")
    run = world.runs.start_account_check(account_id)
    assert run["state"] == "queued" and run["title"] == "Try Claude one"
    with pytest.raises(ProjectConflict, match="already being tried"):
        world.runs.start_account_check(account_id)

    assert await world.launcher.tick() == 1
    pod = world.fake.only_pod()
    assert pod["metadata"]["labels"]["engineering-control/account"] == str(account_id)
    assert pod["spec"]["volumes"][-1]["secret"]["secretName"] == f"account-{account_id}"
    assert world.fake.claims == {}, "a Claude account keeps no home"
    detail = world.runs.get(UUID(run["id"]))
    assert detail["state"] == "running" and detail["steps"][0]["state"] == "running"

    await world.launcher.tick()  # still starting: nothing changes
    pod["status"] = {"phase": "Running"}
    world.fake.logs[pod["metadata"]["name"]] = "--- check\nthinking\n"
    await world.launcher.tick()
    assert world.runs.get(UUID(run["id"]))["steps"][0]["log"] == "--- check\nthinking\n"

    world.fake.finish(_result(_command(stdout="ready\n")))
    await world.launcher.tick()
    step = world.runs.get(UUID(run["id"]))["steps"][0]
    assert (step["state"], step["outcome"], step["exit_code"]) == ("succeeded", "ready", 0)
    assert world.fake.pods == {} and world.fake.deleted == [pod["metadata"]["name"]]
    assert world.runs.get(UUID(run["id"]))["state"] == "running"
    await world.launcher.tick()  # no step is left: the run is done
    finished = world.runs.get(UUID(run["id"]))
    assert (finished["state"], finished["outcome"]) == ("succeeded", "done")
    account = world.state(account_id)
    assert account["state"] == "ready" and account["checked_at"] and account["last_used_at"]
    assert await world.launcher.tick() == 0
    world.runs.start_account_check(account_id)  # and it can be tried again


@pytest.mark.asyncio
async def test_a_codex_account_gets_a_volume_of_its_own_for_its_sign_in(world: Any) -> None:
    account_id = world.account("ChatGPT one", "chatgpt")
    world.runs.start_account_check(account_id)
    await world.launcher.tick()
    assert set(world.fake.claims) == {f"account-{account_id}-home"}
    job = json.loads(world.fake.only_pod()["spec"]["containers"][0]["env"][0]["value"])
    assert job["seed"][0]["generation"] == 1
    world.fake.finish(_result(_command(exit_code=1, stderr="Not logged in. Please run /login")))
    await world.launcher.tick()
    account = world.state(account_id)
    assert account["state"] == "sign_in_required"
    assert "Sign the account in again" in account["state_reason"]
    run = world.runs.listing()["items"][0]
    assert (run["state"], run["outcome"]) == ("failed", "sign_in_required")
    # The volume stays: the next sign-in is seeded into it.
    assert set(world.fake.claims) == {f"account-{account_id}-home"}


@pytest.mark.asyncio
async def test_a_limit_is_recorded_with_its_end_and_a_sandbox_that_vanishes_fails(
    world: Any,
) -> None:
    limited = world.account("Claude one", "claude")
    lost = world.account("Claude two", "claude")
    world.runs.start_account_check(limited)
    await world.launcher.tick()
    world.fake.finish(_result(_command(exit_code=1, stderr="usage limit, try again in 2 hours")))
    await world.launcher.tick()
    account = world.state(limited)
    assert account["state"] == "limited" and account["limited_until"]

    run = world.runs.start_account_check(lost)
    await world.launcher.tick()
    world.fake.pods.clear()
    await world.launcher.tick()
    finished = world.runs.get(UUID(run["id"]))
    assert (finished["state"], finished["outcome"]) == ("failed", "lost")
    assert world.state(lost)["state"] == "unchecked", "nothing was learned about the account"

    # A sandbox that ends without a result says why the cluster ended it.
    run = world.runs.start_account_check(lost)
    await world.launcher.tick()
    world.fake.only_pod()["status"] = {"phase": "Failed", "reason": "DeadlineExceeded"}
    await world.launcher.tick()
    assert "longer than it is allowed" in world.runs.get(UUID(run["id"]))["detail"]


@pytest.mark.asyncio
async def test_a_sandbox_that_cannot_start_is_given_up_after_a_while(world: Any) -> None:
    account_id = world.account("Claude one", "claude")
    run = world.runs.start_account_check(account_id)
    await world.launcher.tick()
    waiting = {"state": {"waiting": {"reason": "ImagePullBackOff"}}}
    world.fake.only_pod()["status"] = {"phase": "Pending", "containerStatuses": [waiting]}
    await world.launcher.tick()
    step = world.runs.get(UUID(run["id"]))["steps"][0]
    assert step["state"] == "running" and "could not be pulled" in step["detail"]
    with world.sessions.begin() as session:
        record = session.get(SandboxStep, UUID(step["id"]))
        assert record is not None
        record.started_at = datetime.now(UTC) - timedelta(minutes=11)
    await world.launcher.tick()
    finished = world.runs.get(UUID(run["id"]))
    assert (finished["state"], finished["outcome"]) == ("failed", "not_started")
    assert "could not be pulled" in finished["detail"] and world.fake.pods == {}


@pytest.mark.asyncio
async def test_a_run_is_stopped_on_request_and_its_sandbox_deleted(world: Any) -> None:
    account_id = world.account("Claude one", "claude")
    run = world.runs.start_account_check(account_id)
    await world.launcher.tick()
    assert world.runs.request_cancel(UUID(run["id"]))["cancel_requested"] is True
    await world.launcher.tick()
    finished = world.runs.get(UUID(run["id"]))
    assert finished["state"] == "cancelled" and finished["steps"][0]["state"] == "cancelled"
    assert world.fake.pods == {}
    with pytest.raises(ProjectConflict, match="already ended"):
        world.runs.request_cancel(UUID(run["id"]))
    # Stopped before it started: nothing to delete.
    queued = world.runs.start_account_check(account_id)
    world.runs.request_cancel(UUID(queued["id"]))
    await world.launcher.tick()
    assert world.runs.get(UUID(queued["id"]))["state"] == "cancelled"


@pytest.mark.asyncio
async def test_a_cluster_that_refuses_leaves_the_run_waiting_with_the_reason(world: Any) -> None:
    account_id = world.account("Claude one", "claude")
    run = world.runs.start_account_check(account_id)
    world.fake.refuse = 403
    await world.launcher.tick()
    waiting = world.runs.get(UUID(run["id"]))
    assert waiting["state"] == "queued"
    assert "did not allow" in waiting["steps"][0]["detail"]
    assert "pods is forbidden" in waiting["steps"][0]["detail"]
    world.fake.refuse = None
    await world.launcher.tick()
    assert world.runs.get(UUID(run["id"]))["state"] == "running"


def test_an_account_is_leased_only_while_it_is_free_signed_in_and_not_paused(world: Any) -> None:
    def step(provider: str, account: UUID | None = None) -> UUID:
        with world.sessions.begin() as session:
            from engineering_control.db.models import SandboxRun

            run = SandboxRun(
                kind="test", state="running", title="t", cancel_requested=False, data={}
            )
            session.add(run)
            session.flush()
            record = SandboxStep(
                run_id=run.id,
                position=1,
                name="agent",
                state="pending",
                provider=provider,
                account_id=account,
                attempt=0,
            )
            session.add(record)
            session.flush()
            return record.id

    one = world.account("Claude one", "claude", max_concurrent=1)
    two = world.account("Claude two", "claude", paused=True)
    world.account("Claude three", "claude", state="sign_in_required")
    world.account(
        "Claude four", "claude", state="limited", limited_until=NOW + timedelta(days=3650)
    )
    unsigned = UUID(world.accounts.create(name="Claude five", provider="claude")["id"])

    first = step("claude")
    lease = world.runs.lease(first)
    assert lease is not None and lease.account_id == one and lease.keeps_home is False
    world.runs.step_started(first, "run-x-1-1")
    assert world.runs.lease(step("claude")) is None, "one is busy, the others cannot be used"
    assert world.runs.lease(step("chatgpt")) is None, "no account of that provider"
    world.runs.step_finished(first, state="succeeded", outcome="ok", detail=None)
    assert world.runs.lease(step("claude")) is not None

    # A step that names its account gets it even when paused, but not without a sign-in.
    named = world.runs.lease(step("claude", two))
    assert named is not None and named.account_id == two
    assert world.runs.lease(step("claude", unsigned)) is None
    with pytest.raises(ProjectInvalid, match="Sign the account in first"):
        world.runs.start_account_check(unsigned)
    # A limit that has ended no longer keeps an account out.
    ended = world.account(
        "ChatGPT one", "chatgpt", state="limited", limited_until=NOW - timedelta(days=1)
    )
    lease = world.runs.lease(step("chatgpt"))
    assert lease is not None and lease.account_id == ended and lease.keeps_home is True


# --- the console ------------------------------------------------------------------------


def test_the_console_starts_a_trial_shows_it_and_can_stop_it(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from engineering_control.config import Settings
    from engineering_control.dashboard.app import create_app
    from engineering_control.dashboard.repository import DashboardRepository

    password = "correct horse battery staple"  # noqa: S105 - deterministic test fixture
    repository = DashboardRepository(f"sqlite+pysqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(repository.engine)
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

        def post(path: str) -> httpx.Response:
            csrf = client.get("/api/dashboard/session").json()["csrf_token"]
            return client.post(f"/api/dashboard{path}", json={"csrf_token": csrf})

        created = repository.accounts.create(name="Claude one", provider="claude")
        refused = post(f"/accounts/{created['id']}/try")
        assert (
            refused.status_code == 422 and "Sign the account in first" in refused.json()["detail"]
        )
        repository.accounts.signed_in(UUID(created["id"]), None)

        started = post(f"/accounts/{created['id']}/try")
        assert started.status_code == 201 and started.json()["state"] == "queued"
        assert post(f"/accounts/{created['id']}/try").status_code == 409
        (account,) = client.get("/api/dashboard/accounts").json()["items"]
        assert account["being_tried"] is True

        run_id = started.json()["id"]
        (listed,) = client.get("/api/dashboard/runs").json()["items"]
        assert listed["id"] == run_id and listed["title"] == "Try Claude one"
        detail = client.get(f"/api/dashboard/runs/{run_id}").json()
        assert [step["name"] for step in detail["steps"]] == ["check"]
        assert client.get(f"/api/dashboard/runs/{uuid4()}").status_code == 404

        # An account a run is working with cannot be removed from under it.
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]
        busy = client.post(
            f"/api/dashboard/accounts/{created['id']}/remove",
            json={"csrf_token": csrf, "current_password": password},
        )
        assert busy.status_code == 409 and "in use by a run" in busy.json()["detail"]

        stopped = post(f"/runs/{run_id}/cancel")
        assert stopped.status_code == 200 and stopped.json()["cancel_requested"] is True
        events = client.get("/api/dashboard/activity/recent").text
        assert "Run Try Claude one: run started" in events
        assert "Run Try Claude one: run stopped" in events

        client.cookies.clear()
        assert client.get("/api/dashboard/runs").status_code == 401
    repository.engine.dispose()
