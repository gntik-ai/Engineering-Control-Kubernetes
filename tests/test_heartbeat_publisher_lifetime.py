from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from engineering_control.config import Settings
from engineering_control.observability.heartbeat import WorkerHeartbeatPublisher


class FixtureClient:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.closed = 0
        self.fail_first = fail_first

    async def post(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        self.calls.append((url, headers))
        if self.fail_first and len(self.calls) == 1:
            raise httpx.ConnectError("fixture unavailability")
        return httpx.Response(200, request=httpx.Request("POST", url))

    async def aclose(self) -> None:
        self.closed += 1


def settings(**overrides: Any) -> Any:
    values = dict(
        evidence_ingest_url="http://127.0.0.1:9876/v1/evidence",
        environment="test",
        worker_heartbeat_seconds=30,
        read_evidence_ingest_token=lambda: "fixture-placeholder",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def cancel_after(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    calls = 0

    async def sleep(seconds: float) -> None:
        nonlocal calls
        assert seconds == 30
        calls += 1
        if calls == count:
            raise asyncio.CancelledError

    monkeypatch.setattr("engineering_control.observability.heartbeat.asyncio.sleep", sleep)


async def test_serve_reuses_one_client_refreshes_token_and_closes_on_cancel(monkeypatch):
    client = FixtureClient(fail_first=True)
    created = []
    tokens = iter(["fixture-one", "fixture-two", "fixture-three"])
    publisher = WorkerHeartbeatPublisher(
        settings(read_evidence_ingest_token=lambda: next(tokens)), "fixture-runner"
    )

    def factory():
        created.append(client)
        return client

    monkeypatch.setattr(publisher, "_client", factory)
    cancel_after(monkeypatch, 3)
    with pytest.raises(asyncio.CancelledError):
        await publisher.serve()
    assert len(created) == 1 and client.closed == 1
    assert [headers["Authorization"] for _, headers in client.calls] == [
        "Bearer fixture-one",
        "Bearer fixture-two",
        "Bearer fixture-three",
    ]
    assert all(url == "http://127.0.0.1:9876/v1/workers/heartbeat" for url, _ in client.calls)
    assert all(headers["X-Engineering-Actor"] == "fixture-runner" for _, headers in client.calls)


async def test_client_creation_failure_is_retried_instead_of_silently_ending_heartbeat(monkeypatch):
    client = FixtureClient()
    attempts = 0
    publisher = WorkerHeartbeatPublisher(settings(), "fixture-runner")

    def factory():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("fixture constructor failure")
        return client

    monkeypatch.setattr(publisher, "_client", factory)
    cancel_after(monkeypatch, 3)
    with pytest.raises(asyncio.CancelledError):
        await publisher.serve()
    assert attempts == 2 and len(client.calls) == 2 and client.closed == 1


async def test_one_shot_owns_its_client_but_does_not_close_borrowed_client(monkeypatch):
    owned = FixtureClient(fail_first=True)
    borrowed = FixtureClient()
    publisher = WorkerHeartbeatPublisher(settings(), "fixture-runner")
    monkeypatch.setattr(publisher, "_client", lambda: owned)
    with pytest.raises(httpx.ConnectError):
        await publisher.publish_once()
    assert owned.closed == 1
    await publisher.publish_once(client=borrowed)  # type: ignore[arg-type]
    assert borrowed.closed == 0 and len(borrowed.calls) == 1


async def test_cancellation_during_http_closes_owned_lifetime_client(monkeypatch):
    class CancelClient(FixtureClient):
        async def post(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
            raise asyncio.CancelledError

    client = CancelClient()
    publisher = WorkerHeartbeatPublisher(settings(), "fixture-runner")
    monkeypatch.setattr(publisher, "_client", lambda: client)
    with pytest.raises(asyncio.CancelledError):
        await publisher.serve()
    assert client.closed == 1


@pytest.mark.parametrize("environment", ["test", "production"])
async def test_absent_target_never_constructs_client_or_reads_credentials(environment, monkeypatch):
    def forbidden():
        raise AssertionError("fixture must not create client or read credential")

    publisher = WorkerHeartbeatPublisher(
        settings(
            evidence_ingest_url=None, environment=environment, read_evidence_ingest_token=forbidden
        ),
        "fixture-runner",
    )
    monkeypatch.setattr(publisher, "_client", forbidden)
    cancel_after(monkeypatch, 2)
    with pytest.raises(asyncio.CancelledError):
        await publisher.serve()


def test_client_policy_still_disallows_proxy_environment_and_redirects(monkeypatch):
    observed = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: observed.append(kwargs))
    WorkerHeartbeatPublisher._client()
    assert observed == [dict(timeout=10, follow_redirects=False, trust_env=False)]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/v1/evidence",
        "http://8.8.8.8/v1/evidence",
        "http://user:fixture@127.0.0.1/v1/evidence",
        "http://127.0.0.1/v1/evidence?override=yes",
    ],
)
def test_origin_boundary_remains_fail_closed(url):
    with pytest.raises(ValueError):
        WorkerHeartbeatPublisher(Settings(evidence_ingest_url=url), "fixture-runner")
