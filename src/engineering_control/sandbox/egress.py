"""The way out for sandboxes: a proxy that connects only to named hosts.

A sandbox that has to reach its provider, GitHub or a package registry may open
connections to this proxy and to nothing else: no other pod, no node, no name server. It
asks the proxy for a tunnel to a host (``CONNECT host:443``); the proxy opens one when the
host is on the list and its address is a public one, and refuses otherwise. What passes
through the tunnel is TLS between the sandbox and that host; the proxy neither reads nor
changes it.

Refusals are logged with the host that was asked for, which is how a host a project turns
out to need is found and added to the list.

One kind of host is not on the list: where a project's own application runs, which a step
that looks at the application in a browser has to reach. The launcher gives such a step a
grant for the hosts the project names for its application, signed with a key only the
launcher and this proxy hold and good for the step's time; the browser presents it with
its tunnel requests. The proxy knows nothing of projects: it checks the signature, the
hosts and the time.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import ipaddress
import re
import signal
import socket
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import structlog

from engineering_control.config import Settings, get_settings
from engineering_control.logging import configure_logging

LOG = structlog.get_logger(__name__)
HEAD_BYTES: Final = 8192
HEAD_SECONDS: Final = 10.0
CONNECT_SECONDS: Final = 15.0
# A tunnel that carries nothing for this long is closed.
IDLE_SECONDS: Final = 1800.0
MAX_TUNNELS: Final = 512
HOST: Final = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
PATTERN: Final = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def allowed(host: str, hosts: Sequence[str]) -> bool:
    """Whether ``host`` is on the list: named exactly, or under a ``*.`` entry."""
    for entry in hosts:
        if entry.startswith("*."):
            if host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


def grant(key: bytes, hosts: str | Sequence[str], *, seconds: int, now: float | None = None) -> str:
    """A grant to open tunnels to ``hosts`` for ``seconds``, signed with ``key``. A host is
    named exactly or, as on the proxy's own list, as every host under a domain (``*.``)."""
    named = [hosts] if isinstance(hosts, str) else list(hosts)
    expires = int((time.time() if now is None else now) + seconds)
    said = f"{expires}.{','.join(host.lower() for host in named)}"
    signature = hmac.new(key, said.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{said}.{signature}"


def granted(key: bytes, token: str, host: str, *, now: float | None = None) -> bool:
    """Whether ``token`` is a grant for ``host`` that this key signed and that still holds."""
    expires, _, rest = token.partition(".")
    named, _, signature = rest.rpartition(".")
    if not expires.isdigit() or not named or not signature:
        return False
    wanted = hmac.new(key, f"{expires}.{named}".encode("ascii", "replace"), hashlib.sha256)
    if not hmac.compare_digest(wanted.hexdigest(), signature):
        return False
    return allowed(host, named.split(",")) and int(expires) > (time.time() if now is None else now)


def presented(head: bytes) -> str | None:
    """The grant a tunnel request carries (``Proxy-Authorization: Basic``, as the password),
    or None."""
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() != b"proxy-authorization":
            continue
        scheme, _, encoded = value.strip().partition(b" ")
        if scheme.lower() != b"basic":
            return None
        try:
            decoded = base64.b64decode(encoded.strip(), validate=True).decode("ascii")
        except (binascii.Error, UnicodeDecodeError):
            return None
        return decoded.partition(":")[2] or None
    return None


def read_grant_key(path: Path | None) -> bytes | None:
    """The key grants are signed with, or None when the installation has none."""
    if path is None:
        return None
    try:
        key = path.read_bytes().strip()
    except OSError:
        return None
    return key if len(key) >= 32 else None


def parse_connect(head: bytes) -> tuple[str, int] | None:
    """The host and port of a CONNECT request, or None for anything else."""
    try:
        line = head.split(b"\r\n", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    parts = line.split(" ")
    if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
        return None
    host, _, port = parts[1].rpartition(":")
    host = host.lower().rstrip(".")
    if not port.isdigit() or not HOST.fullmatch(host):
        return None
    return host, int(port)


def public_addresses(found: Sequence[tuple[object, ...]]) -> list[str]:
    """The addresses a name resolved to that are public ones. A listed name that points
    into the cluster, at a node or at any private network is not followed."""
    addresses: list[str] = []
    for entry in found:
        address = str(entry[4][0])  # type: ignore[index]
        if ipaddress.ip_address(address).is_global and address not in addresses:
            addresses.append(address)
    return addresses


class EgressProxy:
    def __init__(
        self, hosts: Sequence[str], *, upstream_port: int = 443, grant_key: bytes | None = None
    ) -> None:
        self.hosts = tuple(hosts)
        # What grants for hosts that are not on the list are checked against.
        self.grant_key = grant_key
        # Always 443 in service; a test points it at a listener of its own.
        self.upstream_port = upstream_port
        self._tunnels = 0

    async def _resolve(self, host: str) -> list[str]:
        loop = asyncio.get_running_loop()
        found = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return public_addresses(found)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _refuse(
        self, writer: asyncio.StreamWriter, status: str, host: str, why: str, *, ask: bool = False
    ) -> None:
        LOG.warning("egress_refused", host=host, reason=why)
        # A browser presents its grant only once it is asked for one.
        asked = 'Proxy-Authenticate: Basic realm="sandbox"\r\n' if ask else ""
        writer.write(
            f"HTTP/1.1 {status}\r\n{asked}Connection: close\r\nContent-Length: 0\r\n\r\n".encode()
        )
        await writer.drain()

    def _may(self, host: str, head: bytes) -> str | None:
        """None when a tunnel to ``host`` may be opened, else why not."""
        if allowed(host, self.hosts):
            return None
        if self.grant_key is None:
            return "not on the list"
        token = presented(head)
        if token is None:
            return "not on the list, and no grant"
        return None if granted(self.grant_key, token, host) else "not on the list, grant not good"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_SECONDS)
        target = parse_connect(head)
        if target is None:
            await self._refuse(writer, "405 Method Not Allowed", "-", "not a tunnel request")
            return
        host, port = target
        if port != 443:
            await self._refuse(writer, "403 Forbidden", host, f"port {port}")
            return
        why = self._may(host, head)
        if why is not None:
            if why.endswith("no grant"):
                await self._refuse(writer, "407 Proxy Authentication Required", host, why, ask=True)
            else:
                await self._refuse(writer, "403 Forbidden", host, why)
            return
        if self._tunnels >= MAX_TUNNELS:
            await self._refuse(writer, "503 Service Unavailable", host, "too many tunnels")
            return
        try:
            addresses = await asyncio.wait_for(self._resolve(host), CONNECT_SECONDS)
        except (OSError, TimeoutError):
            addresses = []
        if not addresses:
            await self._refuse(writer, "502 Bad Gateway", host, "no public address")
            return
        try:
            upstream_reader, upstream = await asyncio.wait_for(
                asyncio.open_connection(addresses[0], self.upstream_port), CONNECT_SECONDS
            )
        except (OSError, TimeoutError):
            await self._refuse(writer, "502 Bad Gateway", host, "could not connect")
            return
        self._tunnels += 1
        LOG.info("egress_tunnel", host=host)
        try:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(_copy(reader, upstream), _copy(upstream_reader, writer))
        finally:
            self._tunnels -= 1
            upstream.close()
            with contextlib.suppress(OSError):
                await upstream.wait_closed()


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Pass bytes one way until that side ends or goes quiet, then end the other."""
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), IDLE_SECONDS)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (OSError, TimeoutError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            if writer.can_write_eof():
                writer.write_eof()


async def serve(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    configure_logging(resolved.log_level)
    proxy = EgressProxy(
        resolved.sandbox_egress_hosts,
        grant_key=read_grant_key(resolved.sandbox_egress_grant_key_file),
    )
    server = await asyncio.start_server(
        proxy.handle,
        resolved.sandbox_egress_host,
        resolved.sandbox_egress_port,
        limit=HEAD_BYTES,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    LOG.info(
        "egress_started",
        port=resolved.sandbox_egress_port,
        hosts=len(proxy.hosts),
        grants=proxy.grant_key is not None,
    )
    async with server:
        await stop.wait()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
