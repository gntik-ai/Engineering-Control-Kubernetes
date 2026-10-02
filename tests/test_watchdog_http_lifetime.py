from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest

from engineering_control.config import Settings
from engineering_control.watchdog import OperationalWatchdog, serve


@pytest.fixture
def watchdog():
    instance = OperationalWatchdog(Settings(database_url="sqlite:///:memory:"))
    try:
        yield instance
    finally:
        instance.close()


def install_client(monkeypatch, handler):
    client_type = httpx.Client
    clients = []
    options = []

    def factory(**kwargs):
        options.append(kwargs)
        client = client_type(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("engineering_control.watchdog.httpx.Client", factory)
    return clients, options


def test_http_client_is_reused_and_closed_once(monkeypatch, watchdog):
    clients, options = install_client(monkeypatch, lambda _request: httpx.Response(200))
    for _ in range(20):
        assert watchdog._http("fixture", "http://127.0.0.1:8740/health/ready").healthy
    assert len(clients) == 1
    assert options[0]["timeout"] == 5
    assert options[0]["trust_env"] is False
    assert options[0]["limits"].max_connections == 3
    assert options[0]["limits"].max_keepalive_connections == 3
    close = Mock(wraps=clients[0].close)
    monkeypatch.setattr(clients[0], "close", close)
    watchdog.close()
    watchdog.close()
    assert clients[0].is_closed
    close.assert_called_once_with()
    with pytest.raises(RuntimeError, match="watchdog is closed"):
        watchdog._http("fixture", "http://127.0.0.1:8740/health/ready")
    assert len(clients) == 1


def test_construction_failure_is_retried(monkeypatch, watchdog):
    client_type = httpx.Client
    attempts = []

    def factory(**kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("synthetic construction failure")
        return client_type(transport=httpx.MockTransport(lambda _r: httpx.Response(200)), **kwargs)

    monkeypatch.setattr("engineering_control.watchdog.httpx.Client", factory)
    assert watchdog._http("fixture", "http://127.0.0.1:8740/health/ready").status == "UNREACHABLE"
    assert watchdog._http("fixture", "http://127.0.0.1:8740/health/ready").healthy
    assert len(attempts) == 2


def test_http_timeout_recovers_without_new_client(monkeypatch, watchdog):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(200)

    clients, _ = install_client(monkeypatch, handler)
    assert watchdog._http("fixture", "http://127.0.0.1:8740/health/ready").status == "UNREACHABLE"
    assert watchdog._http("fixture", "http://127.0.0.1:8740/health/ready").healthy
    assert len(clients) == 1


def test_cookie_is_not_retained_or_forwarded_between_service_ports(monkeypatch, watchdog):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"set-cookie": "fixture=not-a-secret; Path=/"})

    clients, _ = install_client(monkeypatch, handler)
    for port in (8740, 8750, 8644, 8740):
        assert watchdog._http("fixture", f"http://127.0.0.1:{port}/health/ready").healthy
        assert not list(clients[0].cookies.items())
    assert all("cookie" not in request.headers for request in requests)


@pytest.mark.parametrize("status", [204, 301, 401, 503])
def test_non_200_does_not_follow_redirect_or_claim_ready(monkeypatch, watchdog, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "http://sentinel.invalid/forbidden"})

    install_client(monkeypatch, handler)
    result = watchdog._http("fixture", "http://127.0.0.1:8740/health/ready")
    assert not result.healthy
    assert result.status == "DEGRADED"
    assert len(requests) == 1


def test_unused_http_does_not_create_client(monkeypatch, watchdog):
    factory = Mock(side_effect=AssertionError("must stay lazy"))
    monkeypatch.setattr("engineering_control.watchdog.httpx.Client", factory)
    watchdog.close()
    factory.assert_not_called()


def test_database_pool_disposed_even_if_http_close_raises(monkeypatch, watchdog):
    clients, _ = install_client(monkeypatch, lambda _request: httpx.Response(200))
    assert watchdog._http("fixture", "http://127.0.0.1:8740/health/ready").healthy
    clients[0].close()
    monkeypatch.setattr(clients[0], "close", Mock(side_effect=OSError("synthetic close failure")))
    dispose = Mock(wraps=watchdog.engine.dispose)
    monkeypatch.setattr(watchdog.engine, "dispose", dispose)
    with pytest.raises(OSError, match="synthetic close failure"):
        watchdog.close()
    dispose.assert_called_once_with()


@pytest.mark.parametrize("cycle_fails", [False, True])
def test_serve_closes_resources_on_loop_exit(monkeypatch, cycle_fails):
    instance = Mock()
    instance.run_once.return_value = {"status": "PASS"}
    if cycle_fails:
        instance.run_once.side_effect = RuntimeError("synthetic cycle failure")
    monkeypatch.setattr("engineering_control.watchdog.get_settings", lambda: Settings())
    monkeypatch.setattr("engineering_control.watchdog.configure_logging", lambda _level: None)
    monkeypatch.setattr("engineering_control.watchdog._systemd_notify", lambda _value: None)
    monkeypatch.setattr(
        "engineering_control.watchdog.OperationalWatchdog", lambda _settings: instance
    )
    monkeypatch.setattr(
        "engineering_control.watchdog.time.sleep", Mock(side_effect=KeyboardInterrupt)
    )
    with pytest.raises(KeyboardInterrupt):
        serve()
    instance.run_once.assert_called_once_with()
    instance.close.assert_called_once_with()
