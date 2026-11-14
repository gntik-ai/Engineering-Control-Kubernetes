"""Flows that look at a project's running application in a browser: where it runs and
the identity to sign in with, what such a step may reach, and the UI and UX audits."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from engineering_control.cluster import Cluster
from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.dashboard.project_records import (
    ProjectInvalid,
    ProjectRecords,
    application_host,
    clean_application_hosts,
    clean_application_url,
)
from engineering_control.dashboard.project_sections import SectionRecords
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.dashboard.service import DashboardService
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.prompts import CONTRACTS
from engineering_control.sandbox import pods
from engineering_control.sandbox.egress import granted
from engineering_control.sandbox.launcher import Launcher
from tests.test_sandbox_delivery import REPOSITORY, world  # noqa: F401 - ``world`` is a fixture
from tests.test_sandbox_producers import AUDITED, _by_claude, _checked_out, _codex, _round
from tests.test_sandbox_runs import NAMESPACE, SANDBOXES

PROJECT = "falcone-workflow"
KEY = b"g" * 64


def test_an_applications_address_is_https_a_host_by_name_and_carries_no_sign_in() -> None:
    assert clean_application_url(" https://Staging.Example.com ") == "https://staging.example.com/"
    assert clean_application_url("https://staging.example.com:443/app?tenant=demo") == (
        "https://staging.example.com/app?tenant=demo"
    )
    assert clean_application_url("") is None and clean_application_url(None) is None
    for wrong, said in (
        ("http://staging.example.com", "https://"),
        ("https://user:secret@staging.example.com", "carries no sign-in"),
        ("https://staging.example.com:8443", "port 443 only"),
        ("https://10.0.0.7/", "by its name"),
        ("https://localhost/", "by its name"),
        ("https://" + "a" * 300 + ".example.com", "too long"),
    ):
        with pytest.raises(ProjectInvalid, match=said):
            clean_application_url(wrong)
    assert application_host("https://staging.example.com/app") == "staging.example.com"
    assert application_host(None) is None
    # The other hosts an application uses: names, or the hosts under a domain of its own.
    assert clean_application_hosts(" API.staging.example.com, *.cdn.staging.example.com\n") == [
        "api.staging.example.com",
        "*.cdn.staging.example.com",
    ]
    assert clean_application_hosts([]) == [] and clean_application_hosts("") == []
    for wrong, said in (
        (["*.example.com"], "too wide"),
        (["*.com"], "not a host's name"),
        (["https://api.example.com"], "not a host's name"),
        (["10.0.0.7"], "not a host's name"),
        ([f"h{n}.example.com" for n in range(9)], "at most 8"),
    ):
        with pytest.raises(ProjectInvalid, match=said):
            clean_application_hosts(wrong)


def test_the_tasks_that_look_at_the_application_say_so() -> None:
    looking = {key for key, task in TASKS.items() if task.application}
    assert looking == {"agent.audit_application", "agent.verify_application"}
    for key in looking:
        assert TASKS[key].account and TASKS[key].where == "sandbox" and not TASKS[key].writes
    for key in ("ui-audit", "ux-audit"):
        flow = BUILTIN[key]
        assert [node.task for node in flow.nodes] == [
            "workspace.clone",
            "agent.audit_application",
            "agent.verify_application",
            "findings.propose",
        ]
        assert flow.items == "request"
    assert BUILTIN["ui-audit"].node("decide").params == {"kind": "ui-finding"}
    assert BUILTIN["ux-audit"].node("decide").params == {"kind": "ux-finding"}
    # What a step is told of the application is the platform's to say, in every flow.
    values = {
        "repository": REPOSITORY,
        "base_sha": "a" * 40,
        "max_findings": 8,
        "application": {
            "url": "https://staging.example.com/",
            "hosts": ["staging.example.com", "iam.example.com"],
            "identity": True,
        },
        "scope": {"title": "", "text": "The sign-in page"},
    }
    told = CONTRACTS["application_auditor"].render(
        {"role": "Look at {{repository}}.", "what_counts": "Anything."}, values
    )
    assert told.startswith(f"Look at {REPOSITORY}.")
    assert "The application runs at https://staging.example.com/" in told
    assert "require('sandbox-browser')" in told
    assert "can be reached (staging.example.com, iam.example.com)" in told
    assert "/run/application/username and /run/application/password" in told
    assert "never print the password" in told
    assert "<scope>\nThe sign-in page\n</scope>" in told
    without = CONTRACTS["application_auditor"].render(
        {}, {**values, "application": {"url": "https://staging.example.com/", "identity": False}}
    )
    assert "No test identity is stored" in without and "/run/application" not in without
    nowhere = CONTRACTS["application_auditor"].render({}, {**values, "application": None})
    assert "has no application address" in nowhere


def _secrets() -> tuple[Any, dict[str, dict[str, Any]]]:
    """A cluster that keeps Secrets, to see what is handed to it."""
    kept: dict[str, dict[str, Any]] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if request.method == "PUT":
            if name not in kept:
                return httpx.Response(404, json={})
            kept[name] = json.loads(request.content)
            return httpx.Response(200, json={})
        if request.method == "POST":
            body = json.loads(request.content)
            kept[body["metadata"]["name"]] = body
            return httpx.Response(201, json={})
        kept.pop(name, None)
        return httpx.Response(200, json={})

    return answer, kept


@pytest.mark.asyncio
async def test_the_identity_goes_to_the_cluster_and_the_record_only_says_there_is_one(
    world: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    answer, kept = _secrets()
    token = tmp_path / "cluster-token"
    token.write_text("cluster-identity-" + "t" * 40)
    token.chmod(0o600)
    cluster = Cluster(
        server="https://kubernetes.default.svc",
        namespace=NAMESPACE,
        token_file=token,
        ca_file=None,
        transport=httpx.MockTransport(answer),
    )
    repository = DashboardRepository(str(world.sessions.kw["bind"].url))
    service = DashboardService(repository)

    secret = "s3cret"  # noqa: S105 - a made-up test identity

    async def change(**fields: Any) -> dict[str, Any]:
        given = {"url": "", "username": "", "password": "", "forget_identity": False, **fields}
        return dict(await service.set_application(PROJECT, cluster=cluster, **given))

    with pytest.raises(ProjectInvalid, match="where the application runs before"):
        await change(username="tester", password=secret)
    with pytest.raises(ProjectInvalid, match="a user name and a password"):
        await change(url="https://staging.example.com", username="tester")
    with pytest.raises(ProjectInvalid, match="no sandbox namespace"):
        await service.set_application(
            PROJECT,
            url="https://staging.example.com",
            username="tester",
            password=secret,
            forget_identity=False,
            cluster=None,
        )
    assert kept == {}

    set_ = await change(url="https://staging.example.com/app")
    assert set_["application"] == {
        "url": "https://staging.example.com/app",
        "hosts": [],
        "identity": False,
    }
    stored = await change(url="https://staging.example.com/app", username="tester", password=secret)
    assert stored["application"]["identity"] is True
    project_id = ProjectRecords(world.sessions).identity_of(PROJECT)
    (name,) = kept
    assert name == pods.identity_secret(project_id) == f"application-{project_id}"
    assert base64.b64decode(kept[name]["data"]["password"]) == b"s3cret"
    # The record holds the address and that there is an identity: never the identity.
    assert "s3cret" not in json.dumps(stored) and "tester" not in json.dumps(stored)
    # Changing the address keeps the identity; forgetting it takes it out of the cluster.
    moved = await change(url="https://app.example.com", hosts=["iam.example.com"])
    assert moved["application"] == {
        "url": "https://app.example.com/",
        "hosts": ["iam.example.com"],
        "identity": True,
    }
    with pytest.raises(ProjectInvalid, match="too wide"):
        await change(url="https://app.example.com", hosts=["*.example.com"])
    forgotten = await change(url="https://app.example.com", forget_identity=True)
    assert forgotten["application"]["identity"] is False and kept == {}
    # Without an address there is no application, and no identity either.
    await change(url="https://app.example.com", username="tester", password=secret)
    gone = await change(url="")
    assert gone["application"] is None and kept == {}
    await cluster.aclose()


@pytest.mark.asyncio
async def test_a_ui_audit_looks_at_the_application_and_proposes_what_is_confirmed(
    world: Any,  # noqa: F811
) -> None:
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    backlog = BacklogItems(world.sessions)
    world.launcher = Launcher(
        world.runs,
        world.launcher.cluster,
        SANDBOXES,
        world.github,
        backlog=backlog,
        grant_key=KEY,
    )
    sections = SectionRecords(world.sessions)
    projects = ProjectRecords(world.sessions)
    shown = sections.assign(PROJECT, "ui-audit", "ui-audit")
    assert shown["flow"]["section_action"] == "Audit the interface"
    assert shown["flow"]["needs_application"] is True
    # Nothing to look at until the project says where its application runs.
    with pytest.raises(ProjectInvalid, match="where the project's application runs"):
        world.runs.start_for_section(PROJECT, "ui-audit", scope="", toolchain={})
    projects.set_application(
        PROJECT,
        url="https://staging.example.com/app",
        hosts=["iam.example.com", "*.api.staging.example.com"],
        identity=True,
    )

    run = world.runs.start_for_section(PROJECT, "ui-audit", scope="The sign-in page", toolchain={})
    assert (run["kind"], run["title"]) == ("flow", "UI audit: The sign-in page")
    project_id = projects.identity_of(PROJECT)
    assert run["data"]["application"] == {
        "url": "https://staging.example.com/app",
        "hosts": ["staging.example.com", "iam.example.com", "*.api.staging.example.com"],
        "identity": True,
        "secret": f"application-{project_id}",
    }
    await world.launcher.tick()
    assert [c["name"] for c in world.job()["commands"]] == ["checkout"], "nothing is installed"
    world.fake.finish(_checked_out())
    await world.launcher.tick()

    # The auditor's sandbox: the address, a grant for that one host, the identity as files.
    await world.launcher.tick()
    pod = world.fake.only_pod()
    job = world.job()
    agent = job["commands"][0]
    assert agent["env"]["APPLICATION_URL"] == "https://staging.example.com/app"
    assert "tester" not in json.dumps(job) and 'password":' not in json.dumps(job)
    env = {entry["name"]: entry["value"] for entry in pod["spec"]["containers"][0]["env"]}
    for host in ("staging.example.com", "iam.example.com", "v2.api.staging.example.com"):
        assert granted(KEY, env["SANDBOX_BROWSER_GRANT"], host), host
    assert not granted(KEY, env["SANDBOX_BROWSER_GRANT"], "github.com"), "those hosts only"
    volume = next(v for v in pod["spec"]["volumes"] if v["name"] == "identity")
    assert volume["secret"] == {
        "secretName": f"application-{project_id}",
        "defaultMode": 0o440,
        "optional": True,
    }
    mount = next(m for m in pod["spec"]["containers"][0]["volumeMounts"] if m["name"] == "identity")
    assert mount == {"name": "identity", "mountPath": "/run/application", "readOnly": True}
    prompt = agent["stdin"]
    assert prompt.startswith(f"You audit the interface of the running application of {REPOSITORY}")
    assert "The application runs at https://staging.example.com/app" in prompt
    assert "<scope>\nThe sign-in page\n</scope>" in prompt
    assert "A finding is a page or a control that does not do what it is meant to do" in prompt
    broken = {
        **AUDITED["findings"][0],
        "title": "The sign-in form accepts an empty password",
        "lens": "correctness",
        "files": [],
    }
    world.fake.finish(_codex({**AUDITED, "findings": [broken]}))
    await world.launcher.tick()

    # The verifier: the other subscription, in a browser of its own, with the same reach.
    await world.launcher.tick()
    pod = world.fake.only_pod()
    job = world.job()
    assert job["commands"][0]["argv"][0] == "claude"
    assert job["commands"][0]["env"]["APPLICATION_URL"] == "https://staging.example.com/app"
    assert (
        "Independently verify candidate findings about the running application"
        in (job["commands"][0]["stdin"])
    )
    assert "The sign-in form accepts an empty password" in job["commands"][0]["stdin"]
    env = {entry["name"]: entry["value"] for entry in pod["spec"]["containers"][0]["env"]}
    assert granted(KEY, env["SANDBOX_BROWSER_GRANT"], "staging.example.com")
    world.fake.finish(_by_claude(_round((1, True, True, True, True, None))))
    await world.launcher.tick()
    await world.launcher.tick()
    ended = world.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("succeeded", "findings")
    (found,) = ended["data"]["result"]["findings"]
    item = backlog.get(UUID(found["item_id"]))
    assert item["facts"]["kind"] == "ui-finding" and item["section"]["key"] == "ui-audit"
    # What it found is delivered; anything else of the section asks for an audit of
    # what it names.
    delivery = world.runs.start_item(UUID(item["id"]), toolchain=world.toolchain)
    assert delivery["kind"] == "deliver_issue"
    await world.github.aclose()


def test_a_step_that_does_not_look_at_the_application_gets_neither_grant_nor_identity() -> None:
    usual = pods.step_pod(
        SANDBOXES,
        run_id=UUID(int=1),
        step="audit",
        position=2,
        attempt=1,
        job={"commands": []},
        egress="internet",
    )
    names = {entry["name"] for entry in usual["spec"]["containers"][0]["env"]}
    assert "SANDBOX_BROWSER_GRANT" not in names
    assert "identity" not in {volume["name"] for volume in usual["spec"]["volumes"]}
    assert asyncio.iscoroutinefunction(Launcher.tick)
