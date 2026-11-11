from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx

from engineering_control.cluster_bootstrap import (
    ADMIN_PASSWORD_KEY,
    ADMIN_SECRET,
    SERVICE_SECRET,
    UPLOADER_IDENTITIES,
    NamespaceSecrets,
    bootstrap_admin_password,
    bootstrap_service_secrets,
    service_secret_generators,
)
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base

NAMESPACE = "engineering-control"


class FakeCluster:
    """The two Secret routes of the cluster API, in memory."""

    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, str]] = {}
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        prefix = f"/api/v1/namespaces/{NAMESPACE}/secrets"
        assert request.headers["Authorization"] == "Bearer service-account-token"
        if request.method == "POST" and request.url.path == prefix:
            body = json.loads(request.content)
            name = body["metadata"]["name"]
            if name in self.secrets:
                return httpx.Response(409, json={"reason": "AlreadyExists"})
            self.secrets[name] = dict(body["data"])
            return httpx.Response(201, json=body)
        name = request.url.path.removeprefix(prefix + "/")
        if request.method == "GET":
            if name not in self.secrets:
                return httpx.Response(404, json={"reason": "NotFound"})
            return httpx.Response(200, json={"data": self.secrets[name]})
        if request.method == "PATCH":
            assert request.headers["Content-Type"] == "application/merge-patch+json"
            self.secrets[name].update(json.loads(request.content)["data"])
            return httpx.Response(200, json={"data": self.secrets[name]})
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    def store(self) -> NamespaceSecrets:
        client = httpx.Client(
            transport=httpx.MockTransport(self.handler),
            base_url="https://cluster.invalid",
            headers={"Authorization": "Bearer service-account-token"},
        )
        return NamespaceSecrets(client, NAMESPACE)


def test_service_secret_is_created_once_with_every_key() -> None:
    cluster = FakeCluster()
    store = cluster.store()
    added = bootstrap_service_secrets(store)
    expected = {"api.token", "evidence-ingest.token", "session.key"} | {
        f"{identity}.token" for identity in UPLOADER_IDENTITIES
    }
    assert set(added) == expected == set(service_secret_generators())
    values = {
        key: base64.b64decode(value) for key, value in cluster.secrets[SERVICE_SECRET].items()
    }
    assert len(values["session.key"]) == 64
    assert all(len(values[key]) >= 48 for key in expected - {"session.key"})
    assert len(set(values.values())) == len(values)  # every key has its own value

    # Running it again changes nothing.
    before = dict(cluster.secrets[SERVICE_SECRET])
    assert bootstrap_service_secrets(store) == []
    assert cluster.secrets[SERVICE_SECRET] == before
    assert cluster.requests[-1][0] == "GET"


def test_a_missing_key_is_added_without_touching_the_others() -> None:
    cluster = FakeCluster()
    store = cluster.store()
    bootstrap_service_secrets(store)
    kept = dict(cluster.secrets[SERVICE_SECRET])
    del cluster.secrets[SERVICE_SECRET]["scan-runner.token"]
    del kept["scan-runner.token"]

    assert bootstrap_service_secrets(store) == ["scan-runner.token"]
    after = cluster.secrets[SERVICE_SECRET]
    assert {key: after[key] for key in kept} == kept
    assert "scan-runner.token" in after


def test_a_secret_created_by_a_concurrent_run_is_completed_not_replaced() -> None:
    cluster = FakeCluster()
    store = cluster.store()
    original = cluster.handler
    raced = {"done": False}

    def racing(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and not raced["done"]:
            raced["done"] = True
            cluster.secrets[SERVICE_SECRET] = {"api.token": base64.b64encode(b"theirs").decode()}
        return original(request)

    store.client = httpx.Client(
        transport=httpx.MockTransport(racing),
        base_url="https://cluster.invalid",
        headers={"Authorization": "Bearer service-account-token"},
    )
    added = bootstrap_service_secrets(store)
    assert "api.token" not in added
    assert base64.b64decode(cluster.secrets[SERVICE_SECRET]["api.token"]) == b"theirs"
    assert set(cluster.secrets[SERVICE_SECRET]) == set(service_secret_generators())


def test_admin_password_is_stored_before_it_is_applied_and_only_once(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'bootstrap.db'}"
    repository = DashboardRepository(url)
    Base.metadata.create_all(repository.engine)
    cluster = FakeCluster()
    store = cluster.store()

    assert bootstrap_admin_password(store, url) is True
    password = base64.b64decode(cluster.secrets[ADMIN_SECRET][ADMIN_PASSWORD_KEY]).decode()
    assert len(password) >= 32
    assert asyncio.run(repository.verify_password(password)) is not None

    # A second run leaves both the Secret and the database alone.
    assert bootstrap_admin_password(store, url) is False
    assert base64.b64decode(cluster.secrets[ADMIN_SECRET][ADMIN_PASSWORD_KEY]).decode() == password
    repository.engine.dispose()


def test_a_retry_after_a_failed_database_step_reuses_the_stored_password(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'retry.db'}"
    repository = DashboardRepository(url)
    Base.metadata.create_all(repository.engine)
    cluster = FakeCluster()
    # The first run stored the password and then failed before reaching the database.
    cluster.secrets[ADMIN_SECRET] = {
        ADMIN_PASSWORD_KEY: base64.b64encode(b"stored-by-the-first-attempt-0123456789").decode()
    }
    assert bootstrap_admin_password(cluster.store(), url) is True
    stored = "stored-by-the-first-attempt-0123456789"
    assert asyncio.run(repository.verify_password(stored)) is not None
    repository.engine.dispose()


def test_in_cluster_reads_the_service_account_and_addresses_ipv6(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "token").write_text("service-account-token\n")
    (tmp_path / "namespace").write_text(NAMESPACE)
    (tmp_path / "ca.crt").write_text("")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fd00::1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "6443")
    monkeypatch.setattr(httpx, "Client", lambda **options: options)
    store = NamespaceSecrets.in_cluster(tmp_path)
    assert store.base == f"/api/v1/namespaces/{NAMESPACE}/secrets"
    assert store.client["base_url"] == "https://[fd00::1]:6443"
    assert store.client["headers"] == {"Authorization": "Bearer service-account-token"}
    assert store.client["verify"] == str(tmp_path / "ca.crt")
