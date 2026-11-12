"""One way for every process to reach Temporal, with or without TLS."""

from __future__ import annotations

from pathlib import Path

from temporalio.client import Client
from temporalio.service import TLSConfig

from engineering_control.config import Settings

MAX_CA_BYTES = 65_536
CERTIFICATE_MARKER = b"-----BEGIN CERTIFICATE-----"


def read_certificate_authority(path: Path) -> bytes:
    """The certificates the Temporal server's own certificate must chain to."""
    data = path.read_bytes()
    if not data or len(data) > MAX_CA_BYTES or CERTIFICATE_MARKER not in data:
        raise ValueError("the Temporal certificate authority file holds no certificate")
    return data


def tls_config(settings: Settings) -> TLSConfig | bool:
    """Plain text when no authority is configured (a loopback server); TLS otherwise."""
    if settings.temporal_tls_ca_file is None:
        return False
    return TLSConfig(
        server_root_ca_cert=read_certificate_authority(settings.temporal_tls_ca_file),
        domain=settings.temporal_tls_server_name,
    )


async def connect(settings: Settings) -> Client:
    return await Client.connect(
        settings.temporal_target,
        namespace=settings.temporal_namespace,
        tls=tls_config(settings),
    )
