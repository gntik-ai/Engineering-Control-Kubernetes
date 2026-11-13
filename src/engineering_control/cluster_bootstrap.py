"""One-off bootstrap steps for a fresh installation in a cluster.

Run as Jobs from the control-plane image, with a service account that may read and write
Secrets in its own namespace and nothing else:

``secrets``
    Create the Secret that holds the console's session key and the key of the sandboxes'
    grants, with random values. Keys that already exist are never changed, so re-running
    is harmless and a later release can add a key without rotating the others.

``admin-password``
    Give the dashboard its first password. The password is stored in a Secret first and
    the database is initialised from it second, so a failure between the two steps can
    be retried and never leaves a password nobody can read. Once the dashboard is
    initialised this step does nothing.

Nothing is printed except the names of what was created.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

SERVICE_ACCOUNT_ROOT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
SERVICE_SECRET = "engineering-control-secrets"  # noqa: S105 - the name of a Secret object
ADMIN_SECRET = "engineering-control-admin"  # noqa: S105 - the name of a Secret object
ADMIN_PASSWORD_KEY = "initial-password"  # noqa: S105 - a key name, not a password


def _token() -> bytes:
    return secrets.token_urlsafe(48).encode()


def service_secret_generators() -> dict[str, Callable[[], bytes]]:
    """Every key of the service Secret and how a missing one is generated."""
    return {
        # What a step's grant to reach a project's application is signed with
        # (sandbox/egress.py): the launcher signs, the proxy checks.
        "egress-grant.key": _token,
        "session.key": lambda: secrets.token_bytes(64),
    }


class NamespaceSecrets:
    """The Secrets of one namespace, through the cluster API."""

    def __init__(self, client: httpx.Client, namespace: str) -> None:
        self.client = client
        self.base = f"/api/v1/namespaces/{namespace}/secrets"

    @classmethod
    def in_cluster(cls, root: Path = SERVICE_ACCOUNT_ROOT) -> NamespaceSecrets:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if ":" in host:  # an IPv6 service address
            host = f"[{host}]"
        token = (root / "token").read_text(encoding="utf-8").strip()
        namespace = (root / "namespace").read_text(encoding="utf-8").strip()
        client = httpx.Client(
            base_url=f"https://{host}:{port}",
            headers={"Authorization": f"Bearer {token}"},
            verify=str(root / "ca.crt"),
            timeout=20,
        )
        return cls(client, namespace)

    def read(self, name: str) -> dict[str, bytes] | None:
        response = self.client.get(f"{self.base}/{name}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data: Mapping[str, str] = response.json().get("data") or {}
        return {key: base64.b64decode(value) for key, value in data.items()}

    def ensure(self, name: str, generators: Mapping[str, Callable[[], bytes]]) -> list[str]:
        """Create ``name`` or add its missing keys. Returns the keys that were added."""
        existing = self.read(name)
        missing = {
            key: base64.b64encode(generate()).decode()
            for key, generate in generators.items()
            if existing is None or key not in existing
        }
        if not missing:
            return []
        if existing is None:
            body: dict[str, Any] = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": name,
                    "labels": {"app.kubernetes.io/part-of": "engineering-control"},
                },
                "type": "Opaque",
                "data": missing,
            }
            response = self.client.post(self.base, json=body)
            if response.status_code == 409:  # created by a concurrent run: add what is missing
                return self.ensure(name, generators)
        else:
            response = self.client.patch(
                f"{self.base}/{name}",
                content=json.dumps({"data": missing}),
                headers={"Content-Type": "application/merge-patch+json"},
            )
        response.raise_for_status()
        return sorted(missing)


def bootstrap_service_secrets(store: NamespaceSecrets) -> list[str]:
    return store.ensure(SERVICE_SECRET, service_secret_generators())


def bootstrap_admin_password(store: NamespaceSecrets, database_url: str) -> bool:
    """Initialise the dashboard password if it has none. Returns whether it did."""
    from engineering_control.dashboard.auth import PasswordService
    from engineering_control.dashboard.repository import DashboardRepository

    passwords = PasswordService()
    repository = DashboardRepository(database_url, password_service=passwords)
    try:
        if repository.authentication_version_sync() is not None:
            return False
        store.ensure(ADMIN_SECRET, {ADMIN_PASSWORD_KEY: lambda: passwords.generate().encode()})
        stored = store.read(ADMIN_SECRET)
        if stored is None or ADMIN_PASSWORD_KEY not in stored:
            raise RuntimeError("the administrator Secret could not be read back")
        repository.initialize_password_sync(stored[ADMIN_PASSWORD_KEY].decode())
        return True
    finally:
        repository.engine.dispose()


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engineering-cluster-bootstrap")
    parser.add_argument("step", choices=("secrets", "admin-password"))
    step = parser.parse_args(arguments).step
    store = NamespaceSecrets.in_cluster()
    try:
        if step == "secrets":
            added = bootstrap_service_secrets(store)
            print(f"{SERVICE_SECRET}: added {len(added)} key(s): {', '.join(added) or 'none'}")
        else:
            database_url = os.environ["ENGINEERING_DATABASE_URL"]
            done = bootstrap_admin_password(store, database_url)
            print(
                f"{ADMIN_SECRET}: initial password stored and applied"
                if done
                else "dashboard password already initialised; nothing changed"
            )
    finally:
        store.client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
