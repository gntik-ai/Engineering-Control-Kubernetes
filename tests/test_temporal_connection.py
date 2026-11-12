"""Every process reaches Temporal the same way: plain text on loopback, TLS in a cluster."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from temporalio.client import Client
from temporalio.service import TLSConfig

from engineering_control import temporal_connection
from engineering_control.config import Settings

CERTIFICATE = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def test_without_an_authority_the_connection_is_plain_text() -> None:
    assert temporal_connection.tls_config(Settings()) is False


def test_with_an_authority_the_server_is_verified_against_it(tmp_path: Path) -> None:
    authority = tmp_path / "ca.crt"
    authority.write_bytes(CERTIFICATE)
    settings = Settings(
        temporal_tls_ca_file=authority, temporal_tls_server_name="engineering-control-temporal"
    )
    config = temporal_connection.tls_config(settings)
    assert isinstance(config, TLSConfig)
    assert config.server_root_ca_cert == CERTIFICATE
    assert config.domain == "engineering-control-temporal"
    # No client certificate: the server is authenticated, the client is not (yet).
    assert config.client_cert is None and config.client_private_key is None


@pytest.mark.parametrize("content", [b"", b"not a certificate", b"x" * 70_000])
def test_a_file_without_a_certificate_is_refused(tmp_path: Path, content: bytes) -> None:
    authority = tmp_path / "ca.crt"
    authority.write_bytes(content)
    with pytest.raises(ValueError, match="holds no certificate"):
        temporal_connection.tls_config(Settings(temporal_tls_ca_file=authority))


def test_a_missing_authority_file_stops_the_process(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        temporal_connection.tls_config(Settings(temporal_tls_ca_file=tmp_path / "absent.crt"))


def test_settings_refuse_a_name_without_an_authority_and_a_relative_path() -> None:
    with pytest.raises(ValueError, match="needs the certificate authority file"):
        Settings(temporal_tls_server_name="engineering-control-temporal")
    with pytest.raises(ValueError, match="absolute path"):
        Settings(temporal_tls_ca_file=Path("ca.crt"))
    for name in ("Temporal", "temporal:7233", "-temporal", "temporal/"):
        with pytest.raises(ValueError):
            Settings(temporal_tls_ca_file=Path("/ca.crt"), temporal_tls_server_name=name)


@pytest.mark.asyncio
async def test_connect_passes_target_namespace_and_tls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def connect(target: str, **options: Any) -> str:
        calls.append((target, options))
        return "client"

    monkeypatch.setattr(Client, "connect", connect)
    authority = tmp_path / "ca.crt"
    authority.write_bytes(CERTIFICATE)
    settings = Settings(
        temporal_target="engineering-control-temporal:7233",
        temporal_tls_ca_file=authority,
        temporal_tls_server_name="engineering-control-temporal",
    )
    assert await temporal_connection.connect(settings) == "client"
    assert await temporal_connection.connect(Settings()) == "client"
    (target, secured), (loopback, plain) = calls
    assert target == "engineering-control-temporal:7233"
    assert secured["namespace"] == "engineering-control"
    assert isinstance(secured["tls"], TLSConfig)
    assert loopback == "127.0.0.1:7233" and plain["tls"] is False
