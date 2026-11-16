"""Subscription accounts: added, signed in, paused and removed in the console."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from engineering_control.cluster import Cluster, ClusterError
from engineering_control.config import Settings
from engineering_control.dashboard.accounts import check_credential, provider_catalogue
from engineering_control.dashboard.app import create_app
from engineering_control.dashboard.project_records import ProjectInvalid
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base

PASSWORD = "correct horse battery staple"  # noqa: S105 - deterministic test fixture
NAMESPACE = "engineering-control-sandboxes"
CLAUDE_TOKEN = "sk-ant-oat01-" + "aB3_-" * 20  # noqa: S105 - the shape, not a token
CLUSTER_TOKEN = "cluster-identity-" + "t" * 40  # noqa: S105 - test fixture


def _id_token(**claims: Any) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


def _auth_file(**extra: Any) -> str:
    document: dict[str, Any] = {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _id_token(
                email="someone@example.com",
                **{"https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}},
            ),
            "access_token": "access-" + "a" * 40,
            "refresh_token": "refresh-" + "r" * 40,
            "account_id": "account-1",
        },
        "last_refresh": "2026-11-12T10:00:00Z",
    }
    document.update(extra)
    return json.dumps(document, indent=2)


class FakeCluster:
    """The Secrets of one namespace, with what was asked of the API."""

    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, str | None]] = []
        self.refuse: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            (request.method, request.url.path, request.headers.get("authorization"))
        )
        if self.refuse is not None:
            return httpx.Response(self.refuse, json={"message": "secret material echoed back"})
        prefix = f"/api/v1/namespaces/{NAMESPACE}/secrets"
        name = request.url.path.removeprefix(prefix).lstrip("/")
        if request.method == "PUT":
            if name not in self.secrets:
                return httpx.Response(404, json={})
            self.secrets[name] = json.loads(request.content)
            return httpx.Response(200, json={})
        if request.method == "POST":
            body = json.loads(request.content)
            self.secrets[body["metadata"]["name"]] = body
            return httpx.Response(201, json={})
        if request.method == "DELETE":
            return httpx.Response(200 if self.secrets.pop(name, None) else 404, json={})
        return httpx.Response(405, json={})

    def data(self, name: str) -> dict[str, bytes]:
        return {key: base64.b64decode(value) for key, value in self.secrets[name]["data"].items()}


def _cluster(tmp_path: Path, fake: FakeCluster) -> Cluster:
    token = tmp_path / "token"
    token.write_text(CLUSTER_TOKEN)
    token.chmod(0o600)
    return Cluster(
        server="https://kubernetes.default.svc",
        namespace=NAMESPACE,
        token_file=token,
        ca_file=None,
        transport=httpx.MockTransport(fake),
    )


def _console(tmp_path: Path, cluster: Cluster | None) -> TestClient:
    repository = DashboardRepository(f"sqlite+pysqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(repository.engine)
    repository.initialize_password_sync(PASSWORD)
    client = TestClient(
        create_app(
            settings=Settings(database_url=str(repository.engine.url)),
            repository=repository,
            session_key=b"g" * 48,
            cluster=cluster,
        ),
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    )
    client.__enter__()
    token = client.get("/login/challenge").json()["csrf_token"]
    signed_in = client.post(
        "/login", data={"csrf_token": token, "password": PASSWORD}, follow_redirects=False
    )
    assert signed_in.status_code == 303
    return client


def _post(client: TestClient, path: str, **fields: Any) -> httpx.Response:
    csrf = client.get("/api/dashboard/session").json()["csrf_token"]
    return client.post(f"/api/dashboard/accounts{path}", json={"csrf_token": csrf, **fields})


def _add(client: TestClient, name: str, provider: str) -> dict[str, Any]:
    created = _post(client, "", name=name, provider=provider)
    assert created.status_code == 201, created.text
    account: dict[str, Any] = created.json()
    return account


# --- what may be pasted -----------------------------------------------------------------


def test_a_claude_subscription_token_is_accepted_and_nothing_else() -> None:
    credential = check_credential("claude", f"  {CLAUDE_TOKEN}\n")
    assert credential.data == {"token": CLAUDE_TOKEN.encode()}
    assert credential.identity is None
    for pasted in ("", "   ", "hello", "sk-ant-oat01-short", CLAUDE_TOKEN + " trailing words"):
        with pytest.raises(ProjectInvalid):
            check_credential("claude", pasted)


@pytest.mark.parametrize(
    "key",
    [
        "sk-ant-api03-" + "k" * 90,
        "sk-proj-" + "k" * 60,
        "sk-svcacct-" + "k" * 60,
        "sk-" + "k" * 48,
    ],
)
def test_an_api_key_is_refused_for_every_provider(key: str) -> None:
    for provider in ("claude", "chatgpt"):
        with pytest.raises(ProjectInvalid, match="API key") as refusal:
            check_credential(provider, key)
        assert key not in str(refusal.value)


def test_a_codex_sign_in_file_is_accepted_and_described() -> None:
    pasted = _auth_file()
    credential = check_credential("chatgpt", pasted)
    assert credential.identity == "someone@example.com · plan pro"
    stored = json.loads(credential.data["auth.json"])
    assert stored == json.loads(pasted), "Codex must find the file as it wrote it"


@pytest.mark.parametrize(
    ("pasted", "reason"),
    [
        ("not json at all", "auth.json"),
        ("[1, 2]", "auth.json"),
        (_auth_file(OPENAI_API_KEY="sk-proj-" + "k" * 60), "API key"),
        (json.dumps({"OPENAI_API_KEY": None, "tokens": None}), "no tokens"),
        (json.dumps({"tokens": {"access_token": "a", "refresh_token": ""}}), "no tokens"),
    ],
)
def test_a_sign_in_file_without_a_subscription_is_refused(pasted: str, reason: str) -> None:
    with pytest.raises(ProjectInvalid, match=reason) as refusal:
        check_credential("chatgpt", pasted)
    assert "refresh-" not in str(refusal.value) and "sk-proj" not in str(refusal.value)


def test_an_unreadable_identity_does_not_stop_a_sign_in() -> None:
    document = json.loads(_auth_file())
    document["tokens"]["id_token"] = "not.a.token"  # noqa: S105
    assert check_credential("chatgpt", json.dumps(document)).identity is None


def test_the_catalogue_names_the_providers_asked_for() -> None:
    catalogue = {provider["key"]: provider for provider in provider_catalogue()}
    assert set(catalogue) == {
        "claude",
        "chatgpt",
        "anthropic",
        "openai",
        "google",
        "deepseek",
        "glm",
        "kimi",
        "qwen",
    }
    assert all(provider["available"] and provider["steps"] for provider in catalogue.values())
    # Two are signed in to a subscription; six are given an API key.
    kinds = {key: provider["kind"] for key, provider in catalogue.items()}
    assert {key for key, kind in kinds.items() if kind == "subscription"} == {"claude", "chatgpt"}
    assert all(catalogue[key]["credential"] == "api_key" for key in kinds if kinds[key] == "api")
    assert [e["key"] for e in catalogue["qwen"]["endpoints"]] == ["international", "china"]
    with pytest.raises(ProjectInvalid, match="takes an API key, not a subscription sign-in"):
        check_credential("glm", "anything")


# --- the cluster ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_secret_is_created_then_replaced_and_deleted(tmp_path: Path) -> None:
    fake = FakeCluster()
    cluster = _cluster(tmp_path, fake)
    labels = {"engineering-control/account": "one"}
    await cluster.put_secret("account-one", {"token": b"first"}, labels)
    assert [method for method, _, _ in fake.requests] == ["PUT", "POST"]
    await cluster.put_secret("account-one", {"token": b"second"}, labels)
    assert fake.data("account-one") == {"token": b"second"}
    assert fake.secrets["account-one"]["metadata"] == {
        "name": "account-one",
        "namespace": NAMESPACE,
        "labels": labels,
    }
    assert {authorization for _, _, authorization in fake.requests} == {f"Bearer {CLUSTER_TOKEN}"}
    assert not [method for method, _, _ in fake.requests if method == "GET"], "never read back"
    await cluster.delete_secret("account-one")
    await cluster.delete_secret("account-one")  # already gone is fine
    assert fake.secrets == {}
    await cluster.aclose()


@pytest.mark.asyncio
async def test_a_refusal_says_what_failed_and_not_what_the_cluster_said(tmp_path: Path) -> None:
    fake = FakeCluster()
    fake.refuse = 403
    cluster = _cluster(tmp_path, fake)
    with pytest.raises(ClusterError, match="did not allow") as refusal:
        await cluster.put_secret("account-one", {"token": b"first"}, {})
    assert "echoed" not in str(refusal.value)
    fake.refuse = 500
    with pytest.raises(ClusterError, match="status 500"):
        await cluster.delete_secret("account-one")
    for bad in ("Account", "a/b", "", "-a"):
        with pytest.raises(ValueError, match="valid name"):
            await cluster.put_secret(bad, {}, {})
    await cluster.aclose()


@pytest.mark.asyncio
async def test_an_identity_everyone_can_read_is_not_used(tmp_path: Path) -> None:
    fake = FakeCluster()
    cluster = _cluster(tmp_path, fake)
    (tmp_path / "token").chmod(0o644)
    with pytest.raises(ClusterError, match="identity could not be read"):
        await cluster.put_secret("account-one", {"token": b"first"}, {})
    assert fake.requests == []
    await cluster.aclose()


def test_the_cluster_is_used_only_when_a_sandbox_namespace_is_set() -> None:
    assert Cluster.from_settings(Settings()) is None
    with pytest.raises(ValueError, match="sandbox_namespace"):
        Settings(sandbox_namespace="Not_A_Name")


# --- the console ------------------------------------------------------------------------


def test_an_account_is_added_signed_in_changed_and_removed(tmp_path: Path) -> None:
    fake = FakeCluster()
    client = _console(tmp_path, _cluster(tmp_path, fake))
    try:
        empty = client.get("/api/dashboard/accounts").json()
        assert empty["items"] == [] and empty["sign_in_available"] is True
        assert [provider["key"] for provider in empty["providers"]][:2] == ["claude", "chatgpt"]

        claude = _add(client, "  Claude   one ", "claude")
        assert claude["name"] == "Claude one" and claude["state"] == "signed_out"
        assert claude["max_concurrent"] == 2 and claude["paused"] is False
        codex = _add(client, "ChatGPT one", "chatgpt")

        # Waiting for a sign-in is the first thing the inbox says.
        inbox = client.get("/api/dashboard/inbox").json()
        assert [entry["kind"] for entry in inbox["items"]][:2] == ["account_sign_in"] * 2

        signed = _post(client, f"/{claude['id']}/sign-in", credential=CLAUDE_TOKEN)
        assert signed.status_code == 200, signed.text
        assert signed.json()["state"] == "unchecked" and signed.json()["signed_in_at"]
        assert CLAUDE_TOKEN not in signed.text
        assert fake.data(f"account-{claude['id']}") == {"token": CLAUDE_TOKEN.encode()}
        labels = fake.secrets[f"account-{claude['id']}"]["metadata"]["labels"]
        assert labels["engineering-control/provider"] == "claude"

        signed = _post(client, f"/{codex['id']}/sign-in", credential=_auth_file())
        assert signed.json()["identity"] == "someone@example.com · plan pro"
        assert "refresh-" not in signed.text
        assert set(fake.data(f"account-{codex['id']}")) == {"auth.json"}
        assert client.get("/api/dashboard/inbox").json()["total"] == 0

        changed = _post(client, f"/{claude['id']}/update", paused=True, max_concurrent=1)
        assert changed.json()["paused"] is True and changed.json()["max_concurrent"] == 1
        renamed = _post(client, f"/{claude['id']}/update", name="Claude main")
        assert renamed.json()["name"] == "Claude main" and renamed.json()["paused"] is True

        # Removing asks for the password; a wrong one changes nothing.
        refused = _post(client, f"/{claude['id']}/remove", current_password="wrong")  # noqa: S106
        assert refused.status_code == 403
        assert f"account-{claude['id']}" in fake.secrets
        removed = _post(client, f"/{claude['id']}/remove", current_password=PASSWORD)
        assert removed.status_code == 200
        assert f"account-{claude['id']}" not in fake.secrets
        listed = client.get("/api/dashboard/accounts").json()
        assert [item["name"] for item in listed["items"]] == ["ChatGPT one"]
        assert _post(client, f"/{claude['id']}/update", paused=False).status_code == 404

        # What was done is on record, without what was pasted.
        events = client.get("/api/dashboard/activity/recent").text
        assert "Account Claude main: account removed" in events
        assert CLAUDE_TOKEN not in events and "refresh-" not in events
    finally:
        client.__exit__(None, None, None)


def test_what_cannot_be_an_account_is_refused_with_the_reason(tmp_path: Path) -> None:
    fake = FakeCluster()
    client = _console(tmp_path, _cluster(tmp_path, fake))
    try:
        _add(client, "Claude one", "claude")
        assert _post(client, "", name="claude ONE", provider="chatgpt").status_code == 409
        elsewhere = _post(client, "", name="GLM one", provider="glm", endpoint="mars")
        assert elsewhere.status_code == 422 and "no address mars" in elsewhere.json()["detail"]
        assert _post(client, "", name="X", provider="unknown").status_code == 422
        assert _post(client, "", name="   ", provider="claude").status_code == 422
        other = _add(client, "Claude two", "claude")
        clash = _post(client, f"/{other['id']}/update", name="Claude One")
        assert clash.status_code == 409
        assert _post(client, f"/{other['id']}/update", max_concurrent=9).status_code == 422

        key = _post(client, f"/{other['id']}/sign-in", credential="sk-ant-api03-" + "k" * 90)
        assert key.status_code == 422 and "API key" in key.json()["detail"]
        assert fake.secrets == {} and not fake.requests

        # A cluster that refuses leaves the account as it was.
        fake.refuse = 403
        refused = _post(client, f"/{other['id']}/sign-in", credential=CLAUDE_TOKEN)
        assert refused.status_code == 502 and "did not allow" in refused.json()["detail"]
        listed = {
            item["name"]: item for item in client.get("/api/dashboard/accounts").json()["items"]
        }
        assert listed["Claude two"]["state"] == "signed_out"
        gone = _post(client, f"/{other['id']}/remove", current_password=PASSWORD)
        assert gone.status_code == 502
        assert "Claude two" in {
            item["name"] for item in client.get("/api/dashboard/accounts").json()["items"]
        }
    finally:
        client.__exit__(None, None, None)


def test_without_a_sandbox_namespace_accounts_are_listed_but_not_signed_in(tmp_path: Path) -> None:
    client = _console(tmp_path, None)
    try:
        account = _add(client, "Claude one", "claude")
        assert client.get("/api/dashboard/accounts").json()["sign_in_available"] is False
        refused = _post(client, f"/{account['id']}/sign-in", credential=CLAUDE_TOKEN)
        assert refused.status_code == 422 and "no sandbox namespace" in refused.json()["detail"]
        removed = _post(client, f"/{account['id']}/remove", current_password=PASSWORD)
        assert removed.status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_accounts_need_a_session(tmp_path: Path) -> None:
    client = _console(tmp_path, None)
    try:
        client.cookies.clear()
        assert client.get("/api/dashboard/accounts").status_code == 401
        assert client.post("/api/dashboard/accounts", json={}).status_code == 401
    finally:
        client.__exit__(None, None, None)
