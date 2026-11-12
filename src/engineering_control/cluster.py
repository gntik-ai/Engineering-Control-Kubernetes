"""The cluster the platform runs in, as far as the platform acts on it.

The platform talks to the Kubernetes API of its own cluster with the identity of the pod it
runs in, and only in the namespace of the sandboxes. This module holds what is used so far:
storing and deleting a Secret. The identity is a token the kubelet mounts and rotates, read
again for every request; the server is checked against the cluster's own certificate
authority.

Nothing here reads a Secret back: what is stored for a subscription account is written once
and afterwards only mounted into the sandbox that uses it.

The launcher uses the rest: it starts one pod per step of a run, reads how the pod is
doing and what it printed, and deletes it; it creates and deletes the volume a run works
in. Each process has only the verbs its role in the sandbox namespace grants.
"""

from __future__ import annotations

import base64
import re
import ssl
from pathlib import Path
from typing import Any

import httpx
import structlog

from engineering_control.config import Settings
from engineering_control.secret_files import read_mounted_secret

LOG = structlog.get_logger(__name__)
REQUEST_SECONDS = 10.0
# A DNS label, as Kubernetes wants for a namespace and for the names used here.
NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
MAX_TOKEN_BYTES = 8192
MAX_SECRET_BYTES = 256 * 1024
MAX_LOG_BYTES = 48 * 1024 * 1024


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
                self._token_file,
                label="cluster token",
                minimum=20,
                maximum=MAX_TOKEN_BYTES,
                # The kubelet gives a projected token to the pod's own user and group.
                own_group_readable=True,
            )
        except (OSError, ValueError) as error:
            # The reason names a rule about the file (owner, mode, size), never its content.
            LOG.warning("cluster_identity_unreadable", reason=str(error)[:200])
            raise ClusterError("The platform's cluster identity could not be read") from error
        return {
            "Authorization": f"Bearer {token.decode('ascii', errors='strict').strip()}",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method, path, json=body, params=params, headers=self._headers()
            )
        except httpx.HTTPError as error:
            raise ClusterError("The cluster did not answer") from error

    async def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> int:
        return (await self._request(method, path, body)).status_code

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

    # --- pods and volumes of the sandboxes -----------------------------------------------

    def _path(self, kind: str, name: str | None = None) -> str:
        path = f"/api/v1/namespaces/{self.namespace}/{kind}"
        return path if name is None else f"{path}/{name}"

    async def _create(self, kind: str, what: str, manifest: dict[str, Any]) -> None:
        response = await self._request("POST", self._path(kind), manifest)
        # 409: it is already there, which is what a repeated attempt wants.
        if response.status_code not in (200, 201, 202, 409):
            raise ClusterError(_refusal(f"create {what}", response.status_code, response))

    async def _read(self, kind: str, what: str, name: str) -> dict[str, Any] | None:
        _valid(name)
        response = await self._request("GET", self._path(kind, name))
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ClusterError(_refusal(f"read {what}", response.status_code, response))
        document = response.json()
        return document if isinstance(document, dict) else None

    async def _delete(self, kind: str, what: str, name: str, grace_seconds: int) -> None:
        _valid(name)
        response = await self._request(
            "DELETE", self._path(kind, name), params={"gracePeriodSeconds": str(grace_seconds)}
        )
        if response.status_code not in (200, 202, 404):
            raise ClusterError(_refusal(f"delete {what}", response.status_code, response))

    async def create_pod(self, manifest: dict[str, Any]) -> None:
        await self._create("pods", "the sandbox", manifest)

    async def pod(self, name: str) -> dict[str, Any] | None:
        """The pod as the cluster describes it, or None when there is no such pod."""
        return await self._read("pods", "the sandbox", name)

    async def delete_pod(self, name: str, *, grace_seconds: int = 5) -> None:
        await self._delete("pods", "the sandbox", name, grace_seconds)

    async def pod_log(self, name: str, *, tail_lines: int, limit_bytes: int = MAX_LOG_BYTES) -> str:
        """The end of what the pod's container printed: its last ``tail_lines`` lines, at
        most ``limit_bytes`` of them.

        A pod that has not started yet has no log: that is an empty text, not an error.
        """
        _valid(name)
        response = await self._request(
            "GET",
            f"{self._path('pods', name)}/log",
            params={"tailLines": str(tail_lines), "limitBytes": str(limit_bytes)},
        )
        if response.status_code in (400, 404):
            return ""
        if response.status_code != 200:
            raise ClusterError(_refusal("read the sandbox's output", response.status_code))
        return response.text

    async def create_claim(self, manifest: dict[str, Any]) -> None:
        await self._create("persistentvolumeclaims", "the volume", manifest)

    async def delete_claim(self, name: str) -> None:
        await self._delete("persistentvolumeclaims", "the volume", name, 0)


def _valid(name: str) -> None:
    if not NAME.fullmatch(name):
        raise ValueError("not a valid name")


def _refusal(action: str, status: int, response: httpx.Response | None = None) -> str:
    """What failed. For pods and volumes the cluster's own reason is added: it names a
    field or a rule, never a value the platform put in."""
    reason = ""
    if response is not None:
        try:
            message = response.json().get("message")
        except (ValueError, AttributeError):
            message = None
        if isinstance(message, str) and message:
            reason = f": {' '.join(message.split())[:300]}"
    if status in (401, 403):
        return f"The cluster did not allow the platform to {action} (status {status}){reason}"
    return f"The cluster could not {action} (status {status}){reason}"
