"""The sandboxes' way out: tunnels to named hosts on port 443, and nothing else."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from engineering_control.config import Settings
from engineering_control.sandbox import pods
from engineering_control.sandbox.egress import (
    EgressProxy,
    allowed,
    parse_connect,
    public_addresses,
)
from engineering_control.sandbox.launcher import Launcher

HOSTS = ("api.anthropic.com", "*.githubusercontent.com")


def test_a_host_is_allowed_by_its_exact_name_or_under_a_named_domain() -> None:
    assert allowed("api.anthropic.com", HOSTS)
    assert allowed("objects.githubusercontent.com", HOSTS)
    assert allowed("a.b.githubusercontent.com", HOSTS)
    # Not the domain itself, not a name that merely ends the same way, not a subdomain of
    # a host that was named exactly.
    assert not allowed("githubusercontent.com", HOSTS)
    assert not allowed("evilgithubusercontent.com", HOSTS)
    assert not allowed("x.api.anthropic.com", HOSTS)
    assert not allowed("anthropic.com", HOSTS)
    assert not allowed("example.com", HOSTS)


def test_only_a_tunnel_request_for_a_host_name_is_understood() -> None:
    assert parse_connect(b"CONNECT API.Anthropic.com:443 HTTP/1.1\r\nHost: x\r\n\r\n") == (
        "api.anthropic.com",
        443,
    )
    assert parse_connect(b"CONNECT github.com.:22 HTTP/1.1\r\n\r\n") == ("github.com", 22)
    for head in (
        b"GET http://api.anthropic.com/ HTTP/1.1\r\n\r\n",
        b"CONNECT 10.43.0.1:443 HTTP/1.1\r\n\r\n",
        b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n",
        b"CONNECT localhost:443 HTTP/1.1\r\n\r\n",
        b"CONNECT api.anthropic.com HTTP/1.1\r\n\r\n",
        b"CONNECT api.anthropic.com:443\r\n\r\n",
        b"CONNECT api.anthropic.com:443 HTTP/1.1 extra\r\n\r\n",
        b"CONNECT \xff.com:443 HTTP/1.1\r\n\r\n",
    ):
        assert parse_connect(head) is None, head


def test_a_name_that_points_into_a_private_network_is_not_followed() -> None:
    def entry(address: str) -> tuple[object, ...]:
        return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))

    found = [
        entry(address)
        for address in (
            "10.43.0.1",
            "160.79.104.10",
            "127.0.0.1",
            "100.64.0.7",
            "169.254.169.254",
            "192.168.1.1",
            "160.79.104.10",
            "fd00::1",
            "2607:6bc0::10",
        )
    ]
    assert public_addresses(found) == ["160.79.104.10", "2607:6bc0::10"]


class _Proxy(EgressProxy):
    """Resolves every name to the given addresses instead of asking a name server."""

    def __init__(self, addresses: list[str], *, upstream_port: int = 443) -> None:
        super().__init__(HOSTS, upstream_port=upstream_port)
        self.addresses = addresses
        self.asked: list[str] = []

    async def _resolve(self, host: str) -> list[str]:
        self.asked.append(host)
        return public_addresses(
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in self.addresses]
        )


async def _ask(proxy: EgressProxy, request: bytes) -> bytes:
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        await writer.wait_closed()
    return answer


@pytest.mark.parametrize(
    ("request_", "status", "resolved"),
    [
        (b"GET http://api.anthropic.com/ HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n", 405, False),
        (b"CONNECT api.anthropic.com:80 HTTP/1.1\r\n\r\n", 403, False),
        (b"CONNECT api.anthropic.com:22 HTTP/1.1\r\n\r\n", 403, False),
        (b"CONNECT example.com:443 HTTP/1.1\r\n\r\n", 403, False),
        (
            b"CONNECT engineering-control-postgres-rw.engineering-control.svc:443 HTTP/1.1\r\n\r\n",
            403,
            False,
        ),
        (b"CONNECT 10.43.0.1:443 HTTP/1.1\r\n\r\n", 405, False),
        # On the list, but the name points into the cluster: looked up, then refused.
        (b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n", 502, True),
    ],
)
def test_the_proxy_refuses_everything_but_a_tunnel_to_a_named_public_host(
    request_: bytes, status: int, resolved: bool
) -> None:
    proxy = _Proxy(["10.42.3.7", "127.0.0.1"])
    answer = asyncio.run(_ask(proxy, request_))
    assert answer.startswith(f"HTTP/1.1 {status} ".encode()), answer
    assert answer.endswith(b"\r\n\r\n")
    assert bool(proxy.asked) is resolved


def test_a_tunnel_passes_bytes_both_ways_and_ends_with_either_side() -> None:
    async def scenario() -> tuple[bytes, bytes]:
        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.readexactly(5)
            writer.write(b"echo:" + data)
            await writer.drain()
            writer.close()

        target = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = target.sockets[0].getsockname()[1]

        class Local(_Proxy):
            async def _resolve(self, host: str) -> list[str]:
                self.asked.append(host)
                return ["127.0.0.1"]

        proxy = Local([], upstream_port=port)
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        async with target, server:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", server.sockets[0].getsockname()[1]
            )
            writer.write(
                b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\n\r\n"
            )
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            writer.write(b"hello")
            await writer.drain()
            body = await asyncio.wait_for(reader.read(), 5)
            writer.close()
            await writer.wait_closed()
        assert proxy.asked == ["api.anthropic.com"]
        return head, body

    head, body = asyncio.run(scenario())
    assert head == b"HTTP/1.1 200 Connection established\r\n\r\n"
    assert body == b"echo:hello"


def test_the_list_of_hosts_holds_names_and_narrow_domains_only() -> None:
    settings = Settings(
        sandbox_egress_hosts=(" API.Anthropic.com ", "*.example.org", "api.anthropic.com")
    )
    assert settings.sandbox_egress_hosts == ("api.anthropic.com", "*.example.org")
    for wrong in ("*.com", "*", "10.0.0.1", "http://github.com", "github.com:443", "a_b.com"):
        with pytest.raises(ValueError):
            Settings(sandbox_egress_hosts=(wrong,))
    # Every client of a subscription is on the default list; no API-key endpoint is added
    # by a wildcard.
    hosts = Settings().sandbox_egress_hosts
    assert {"api.anthropic.com", "chatgpt.com", "auth.openai.com", "github.com"} <= set(hosts)
    assert not any(host.startswith("*.") for host in hosts)
    with pytest.raises(ValueError):
        Settings(sandbox_egress_host="0.0.0.0")  # noqa: S104 - refused outside a cluster
    Settings(network_boundary="cluster", sandbox_egress_host="0.0.0.0")  # noqa: S104
    for wrong in ("https://proxy:8888", "http://proxy", "http://proxy:8888/", "proxy:8888"):
        with pytest.raises(ValueError):
            Settings(sandbox_egress_proxy=wrong)


def _sandboxes(proxy: str | None) -> pods.Sandboxes:
    return pods.Sandboxes(
        namespace="engineering-control-sandboxes",
        image="ghcr.io/gntik-ai/engineering-control-kubernetes-sandbox:abc",
        pull_secret="engineering-control-registry",  # noqa: S106 - a name, not a secret
        proxy=proxy,
    )


def _pod(proxy: str | None, egress: pods.Egress) -> dict[str, Any]:
    return pods.step_pod(
        _sandboxes(proxy),
        run_id=UUID("01234567-89ab-cdef-0123-456789abcdef"),
        step="prepare",
        position=1,
        attempt=1,
        job={"commands": []},
        egress=egress,
    )


def _environment(pod: dict[str, Any]) -> dict[str, str]:
    container = pod["spec"]["containers"][0]
    return {entry["name"]: entry["value"] for entry in container["env"]}


def test_a_step_that_may_reach_out_is_pointed_at_the_proxy_and_no_other_step_is() -> None:
    out = _environment(_pod("http://10.43.12.34:8888", "internet"))
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        assert out[name] == "http://10.43.12.34:8888"
    assert set(_environment(_pod("http://10.43.12.34:8888", "none"))) == {"SANDBOX_JOB"}
    assert set(_environment(_pod(None, "internet"))) == {"SANDBOX_JOB"}


def test_the_launcher_gives_sandboxes_the_proxy_by_address() -> None:
    async def scenario() -> tuple[str | None, str | None, str | None]:
        def launcher(proxy: str | None) -> Launcher:
            return Launcher(None, None, _sandboxes(proxy), None)  # type: ignore[arg-type]

        named = (await launcher("http://localhost:8888")._sandboxes()).proxy
        addressed = (await launcher("http://10.43.12.34:8888")._sandboxes()).proxy
        absent = (await launcher(None)._sandboxes()).proxy
        return named, addressed, absent

    assert asyncio.run(scenario()) == (
        "http://127.0.0.1:8888",
        "http://10.43.12.34:8888",
        None,
    )


# --- the step's own wait for its way out -------------------------------------------------

STEP = Path(__file__).resolve().parents[1] / "images" / "sandbox" / "sandbox-step"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _step(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    job = {"commands": [{"name": "work", "argv": ["echo", "the command ran"], "timeout": 20}]}
    return subprocess.run(  # noqa: S603 - the repository's own step runner
        [sys.executable, str(STEP)],
        env={"PATH": "/usr/bin:/bin", "SANDBOX_JOB": json.dumps(job), **environment},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_step_waits_until_its_way_out_opens_before_its_first_command() -> None:
    """The cluster's network rules learn of a new sandbox a moment after it has started.
    A command that connects at once (a clone) would be refused in that moment."""
    port = _free_port()

    def open_later() -> None:
        time.sleep(1.5)
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen(4)
            listener.settimeout(20)
            with contextlib.suppress(OSError):
                listener.accept()[0].close()

    opener = threading.Thread(target=open_later)
    opener.start()
    ran = _step({"HTTPS_PROXY": f"http://127.0.0.1:{port}"})
    opener.join()
    assert ran.returncode == 0, ran.stderr
    waited, command = ran.stdout.index("for the way out"), ran.stdout.index("the command ran")
    assert waited < command, "the command ran only once the way out was open"
    assert "sandbox-step: waited 1." in ran.stdout or "sandbox-step: waited 2." in ran.stdout


def test_a_step_does_not_wait_for_ever_and_one_without_a_way_out_does_not_wait() -> None:
    closed = _free_port()
    started = time.monotonic()
    ran = _step({"https_proxy": f"http://127.0.0.1:{closed}", "SANDBOX_WAY_OUT_SECONDS": "1"})
    assert ran.returncode == 0 and 1.0 <= time.monotonic() - started < 15
    assert f"the way out at 127.0.0.1:{closed} did not open in 1 s" in ran.stdout
    assert "the command ran" in ran.stdout, "the commands run anyway and say what fails"
    # No proxy named, or a value that names none: nothing to wait for, nothing said.
    for environment in ({}, {"HTTPS_PROXY": "nonsense"}):
        started = time.monotonic()
        ran = _step(environment)
        assert ran.returncode == 0 and "way out" not in ran.stdout
        assert time.monotonic() - started < 5
