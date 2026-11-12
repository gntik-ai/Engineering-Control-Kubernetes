"""Which address a sign-in attempt is counted against, with and without a proxy."""

from __future__ import annotations

import ipaddress

import pytest
from starlette.requests import Request

from engineering_control.config import Settings
from engineering_control.dashboard.app import _client_address

POD_NETWORK = (ipaddress.ip_network("10.42.0.0/16"),)


def _request(peer: str, forwarded: str | None = None) -> Request:
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
    return Request({"type": "http", "client": (peer, 40000), "headers": headers})


@pytest.mark.parametrize(
    "peer,forwarded,trusted,expected",
    [
        # A direct connection is never allowed to name another address.
        ("203.0.113.9", "198.51.100.7", (), "203.0.113.9"),
        ("203.0.113.9", "198.51.100.7", POD_NETWORK, "203.0.113.9"),
        # The ingress controller, inside the trusted network, is believed.
        ("10.42.3.14", "198.51.100.7", POD_NETWORK, "198.51.100.7"),
        ("10.42.3.14", "198.51.100.7", (), "10.42.3.14"),
        # Only the entry the nearest proxy wrote counts; the rest is what the client sent.
        ("10.42.3.14", "192.0.2.1, 198.51.100.7", POD_NETWORK, "198.51.100.7"),
        ("127.0.0.1", "192.0.2.1, 198.51.100.7", (), "198.51.100.7"),
        ("::1", "2001:db8::5", (), "2001:db8::5"),
        # Nothing usable was forwarded: fall back to the connection.
        ("10.42.3.14", None, POD_NETWORK, "10.42.3.14"),
        ("10.42.3.14", "not-an-address", POD_NETWORK, "10.42.3.14"),
        ("10.42.3.14", "198.51.100.7, ", POD_NETWORK, "10.42.3.14"),
    ],
)
def test_forwarded_address_is_believed_only_from_a_proxy(
    peer: str,
    forwarded: str | None,
    trusted: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    expected: str,
) -> None:
    assert _client_address(_request(peer, forwarded), trusted) == expected


def test_trusted_proxy_networks_are_bounded_and_only_for_a_cluster() -> None:
    assert Settings().dashboard_trusted_proxy_networks == ()
    with pytest.raises(ValueError, match="only meaningful inside a cluster"):
        Settings(dashboard_trusted_proxy_networks=("10.42.0.0/16",))
    settings = Settings(
        network_boundary="cluster", dashboard_trusted_proxy_networks=("10.42.0.0/16", "fd00::/48")
    )
    assert settings.dashboard_trusted_proxy_networks == ("10.42.0.0/16", "fd00::/48")
    for value in (("0.0.0.0/0",), ("::/0",), ("ingress-nginx",), ("10.42.0.1/16",)):
        with pytest.raises(ValueError):
            Settings(network_boundary="cluster", dashboard_trusted_proxy_networks=value)
    with pytest.raises(ValueError):
        Settings(
            network_boundary="cluster",
            dashboard_trusted_proxy_networks=tuple(f"10.{n}.0.0/16" for n in range(9)),
        )


def test_the_setting_is_read_from_the_environment_as_the_manifest_writes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENGINEERING_NETWORK_BOUNDARY", "cluster")
    monkeypatch.setenv("ENGINEERING_DASHBOARD_TRUSTED_PROXY_NETWORKS", '["10.42.0.0/16"]')
    assert Settings().dashboard_trusted_proxy_networks == ("10.42.0.0/16",)
