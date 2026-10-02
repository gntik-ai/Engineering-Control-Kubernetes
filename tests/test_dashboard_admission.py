from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from engineering_control.dashboard import admission as module
from engineering_control.dashboard.admission import AdmissionReader


def payload(mode: str = "synthetic-only", running: Any = False) -> dict[str, Any]:
    return {
        "status": "ready",
        "admission": {
            "scope": "private-api-only",
            "mode": mode,
            "pending_operation_dispatcher_running": running,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,running,expected",
    [
        ("synthetic-only", False, "SYNTHETIC_ONLY"),
        ("diagnostics-only", True, "DIAGNOSTICS_ONLY"),
        ("diagnostics-only", False, "DIAGNOSTICS_STOPPED"),
        ("normal", True, "DISPATCHING"),
        ("normal", False, "DISPATCHER_STOPPED"),
    ],
)
async def test_admission_fixed_read_path_and_scope(
    mode: str, running: bool, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://198.51.100.1:9999")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == "http://127.0.0.1:8740/health/ready"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        assert request.content == b""
        return httpx.Response(200, json=payload(mode, running))

    reader = AdmissionReader("localhost", 8740, transport=httpx.MockTransport(handler))
    try:
        value = await reader.read()
        assert value["status"] == expected
        assert value["scope"] == "private-api-only"
        assert value["observed_at"] and value["freshness_seconds"] < 1
        assert "approval still apply" in value["detail"]
        assert len(requests) == 1
    finally:
        await reader.aclose()
    assert reader.client.is_closed


@pytest.mark.parametrize(
    "host,port",
    [
        ("138.199.173.27", 8740),
        ("10.10.0.2", 8740),
        ("ade.musematic.ai", 8740),
        ("localhost.attacker.invalid", 8740),
        ("127.0.0.1@remote.invalid", 8740),
        ("127.0.0.1", 80),
        ("127.0.0.1", 65536),
    ],
)
def test_non_loopback_or_invalid_target_rejected(host: str, port: int) -> None:
    with pytest.raises(ValueError, match="loopback"):
        AdmissionReader(host, port)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b"{}",
        b"x" * 4097,
        json.dumps(payload("typo")).encode(),
        json.dumps(payload("normal", "false")).encode(),
        json.dumps(payload("synthetic-only", True)).encode(),
        b'{"status":"ready","admission":{"scope":"all-projects"}}',
        b'{"status":"ready","admission":{"scope":"private-api-only","mode":"normal",'
        b'"pending_operation_dispatcher_running":true,"secret":"inert-value"}}',
    ],
)
async def test_unknown_for_invalid_or_oversized_body(body: bytes) -> None:
    reader = AdmissionReader(
        "127.0.0.1",
        8740,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body)),
    )
    try:
        value = await reader.read()
        assert value["status"] == "UNKNOWN" and value["observed_at"] is None
        assert "inert-value" not in str(value)
    finally:
        await reader.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [301, 302, 401, 403, 500, 503])
async def test_no_redirect_or_success_from_non200(status_code: int) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            status_code,
            json=payload("normal", True),
            headers={
                "Location": "https://198.51.100.1/sentinel",
            },
        )

    reader = AdmissionReader("127.0.0.1", 8740, transport=httpx.MockTransport(handler))
    try:
        assert (await reader.read())["status"] == "UNKNOWN"
        assert calls == ["http://127.0.0.1:8740/health/ready"]
    finally:
        await reader.aclose()


@pytest.mark.asyncio
async def test_cache_coalesces_and_failed_refresh_drops_previous_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        await asyncio.sleep(0)
        if len(calls) > 1:
            raise httpx.ConnectError("inert-error-must-not-be-emitted")
        return httpx.Response(200, json=payload("normal", True))

    reader = AdmissionReader("127.0.0.1", 8740, transport=httpx.MockTransport(handler))
    try:
        observations = await asyncio.gather(*(reader.read() for _ in range(5)))
        assert len(calls) == 1
        assert all(item["status"] == "DISPATCHING" for item in observations)
        observations[0]["status"] = "tampered"
        assert (await reader.read())["status"] == "DISPATCHING"
        reader._until = 0
        value = await reader.read()
        assert value["status"] == "UNKNOWN" and value["observed_at"] is None
        assert "inert-error" not in str(value)
        assert len(calls) == 2
    finally:
        await reader.aclose()


@pytest.mark.asyncio
async def test_slow_response_hits_whole_request_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return httpx.Response(200, json=payload())

    monkeypatch.setattr(module, "REQUEST_SECONDS", 0.01)
    reader = AdmissionReader("127.0.0.1", 8740, transport=httpx.MockTransport(handler))
    try:
        assert (await reader.read())["status"] == "UNKNOWN"
        assert cancelled
    finally:
        await reader.aclose()
