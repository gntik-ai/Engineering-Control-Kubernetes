"""The cluster the platform runs in, as far as the platform acts on it.

The platform talks to the Kubernetes API of its own cluster with the identity of the pod it
runs in, and only in the namespace of the sandboxes. This module holds what is used so far:
storing and deleting a Secret. The identity is a token the kubelet mounts and rotates, read
again for every request; the server is checked against the cluster's own certificate
authority.

Nothing here reads a Secret back: what is stored for a subscription account is written once
and afterwards only mounted into the sandbox that uses it.
"""

from __future__ import annotations

import base64
import re
import ssl
from pathlib import Path
from typing import Any

import httpx

from engineering_control.config import Settings
from engineering_control.secret_files import read_mounted_secret

REQUEST_SECONDS = 10.0
# A DNS label, as Kubernetes wants for a namespace and for the names used here.
NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
MAX_TOKEN_BYTES = 8192
MAX_SECRET_BYTES = 256 * 1024


class ClusterError(RuntimeError):
    """Something the administrator can read; never a token or a response body."""


class Cluster:
    def __init__(
        self,
        *,
        server: str,
        namespace: str,
        token_file: Path,
        ca_file: Path | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not NAME.fullmatch(namespace):
            raise ValueError("the sandbox namespace is not a valid name")
        self.namespace = namespace
        self._token_file = token_file
        verify: ssl.SSLContext | bool = True
        if transport is None:
            if ca_file is None:
                raise ValueError("the cluster's certificate authority file is not set")
            verify = ssl.create_default_context(cafile=str(ca_file))
        self._client = httpx.AsyncClient(
            base_url=server.rstrip("/"),
            timeout=REQUEST_SECONDS,
            follow_redirects=False,
            trust_env=False,
            verify=verify,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Cluster | None:
        """None when no sandbox namespace is set: the platform then acts on no cluster."""
        if settings.sandbox_namespace is None:
            return None
        return cls(
            server=settings.cluster_api_server,
            namespace=settings.sandbox_namespace,
            token_file=settings.cluster_token_file,
            ca_file=settings.cluster_ca_file,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        try:
            token = read_mounted_secret(
                self._token_file, label="cluster token", minimum=20, maximum=MAX_TOKEN_BYTES
            )
        except (OSError, ValueError) as error:
            raise ClusterError("The platform's cluster identity could not be read") from error
        return {
            "Authorization": f"Bearer {token.decode('ascii', errors='strict').strip()}",
            "Accept": "application/json",
        }

    async def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> int:
        try:
            response = await self._client.request(method, path, json=body, headers=self._headers())
        except httpx.HTTPError as error:
            raise ClusterError("The cluster did not answer") from error
        return response.status_code

    def _secrets(self, name: str | None = None) -> str:
        path = f"/api/v1/namespaces/{self.namespace}/secrets"
        return path if name is None else f"{path}/{name}"

    async def put_secret(self, name: str, data: dict[str, bytes], labels: dict[str, str]) -> None:
        """Store the Secret, replacing one of the same name."""
        if not NAME.fullmatch(name):
            raise ValueError("the secret's name is not a valid name")
        if sum(len(value) for value in data.values()) > MAX_SECRET_BYTES:
            raise ValueError("the secret is too large")
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": self.namespace, "labels": labels},
            "type": "Opaque",
            "data": {key: base64.b64encode(value).decode("ascii") for key, value in data.items()},
        }
        status = await self._send("PUT", self._secrets(name), body)
        if status == 404:
            status = await self._send("POST", self._secrets(), body)
            if status == 409:  # created by someone else in between
                status = await self._send("PUT", self._secrets(name), body)
        if status not in (200, 201):
            raise ClusterError(_refusal("store the sign-in", status))

    async def delete_secret(self, name: str) -> None:
        """Delete the Secret; one that is not there is already deleted."""
        if not NAME.fullmatch(name):
            raise ValueError("the secret's name is not a valid name")
        status = await self._send("DELETE", self._secrets(name))
        if status not in (200, 202, 404):
            raise ClusterError(_refusal("delete the sign-in", status))


def _refusal(action: str, status: int) -> str:
    if status in (401, 403):
        return f"The cluster did not allow the platform to {action} (status {status})"
    return f"The cluster could not {action} (status {status})"
