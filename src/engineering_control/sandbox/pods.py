"""What a sandbox is, as the cluster is asked for it.

One step of a run is one pod of the sandbox image. It is unprivileged in every respect the
cluster can enforce, has no identity towards the cluster, runs on the node reserved for
sandboxes, and sees only what is mounted into it: the run's workspace, and for an agent
step the sign-in of the one account leased to it. What it may reach on the network is a
label the network policies of the namespace act on.

Everything here is a plain function of its arguments, so that the shape of a sandbox is
decided, and tested, in one place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

# The account and paths of the sandbox image (images/sandbox/Dockerfile).
SANDBOX_UID: Final = 10002
HOME: Final = "/home/agent"
WORKSPACE: Final = "/workspace"
ACCOUNT: Final = "/run/account"
ACCOUNT_HOME: Final = "/account-home"
RUNNER: Final = "sandbox-step"
NODE_LABEL: Final = "engineering-control/sandbox"
PART_OF: Final = {"app.kubernetes.io/part-of": "engineering-control"}

Egress = Literal["internet", "none"]


@dataclass(frozen=True)
class Sandboxes:
    """Where sandboxes run and what they run."""

    namespace: str
    image: str
    pull_secret: str
    service_account: str = "sandbox"
    # The proxy a step that may reach out goes through, as http://address:port. A sandbox
    # has no name server, so this is an address, not a name.
    proxy: str | None = None


@dataclass(frozen=True)
class AccountMount:
    """The account leased to a step: its sign-in, and for a client that renews its sign-in
    in place, the volume that keeps it."""

    account_id: UUID
    keeps_home: bool

    @property
    def secret(self) -> str:
        return f"account-{self.account_id}"

    @property
    def home_claim(self) -> str:
        return f"account-{self.account_id}-home"


def pod_name(run_id: UUID, position: int, attempt: int) -> str:
    return f"run-{run_id.hex[:16]}-{position}-{attempt}"


def workspace_claim(run_id: UUID) -> str:
    return f"run-{run_id.hex[:16]}"


def claim(name: str, *, namespace: str, gibibytes: int, labels: dict[str, str]) -> dict[str, Any]:
    """A volume of the node's own disk. No storage class is named: the cluster's default
    decides, and the volume is made where its first pod runs, which is the sandbox node."""
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace, "labels": {**PART_OF, **labels}},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": f"{gibibytes}Gi"}},
        },
    }


def _way_out(sandboxes: Sandboxes, egress: Egress) -> list[dict[str, str]]:
    """A step that may reach out is told where the proxy is; that is its only way out."""
    if egress != "internet" or sandboxes.proxy is None:
        return []
    return [
        {"name": name, "value": sandboxes.proxy}
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    ]


def step_pod(
    sandboxes: Sandboxes,
    *,
    run_id: UUID,
    step: str,
    position: int,
    attempt: int,
    job: dict[str, Any],
    egress: Egress,
    account: AccountMount | None = None,
    workspace: str | None = None,
    cpu: str = "2",
    memory: str = "4Gi",
    deadline_seconds: int = 1800,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The pod of one step. ``job`` is what the runner inside executes. It holds no
    credential of an account or of GitHub's that outlives the step: at most a token the
    platform issued for this step alone and takes back when the step ends."""
    labels = {
        **(labels or {}),
        **PART_OF,
        "app.kubernetes.io/name": "sandbox",
        "engineering-control/run": str(run_id),
        "engineering-control/step": step,
        "engineering-control/egress": egress,
    }
    mounts: list[dict[str, Any]] = [
        {"name": "home", "mountPath": HOME},
        {"name": "tmp", "mountPath": "/tmp"},  # noqa: S108 - the pod's own empty volume
        {"name": "workspace", "mountPath": WORKSPACE},
    ]
    volumes: list[dict[str, Any]] = [
        {"name": "home", "emptyDir": {"sizeLimit": "2Gi"}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "4Gi"}},
        {"name": "workspace", "persistentVolumeClaim": {"claimName": workspace}}
        if workspace
        else {"name": "workspace", "emptyDir": {"sizeLimit": "1Gi"}},
    ]
    if account is not None:
        labels["engineering-control/account"] = str(account.account_id)
        mounts.append({"name": "account", "mountPath": ACCOUNT, "readOnly": True})
        volumes.append(
            {"name": "account", "secret": {"secretName": account.secret, "defaultMode": 0o440}}
        )
        if account.keeps_home:
            mounts.append({"name": "account-home", "mountPath": ACCOUNT_HOME})
            volumes.append(
                {
                    "name": "account-home",
                    "persistentVolumeClaim": {"claimName": account.home_claim},
                }
            )
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name(run_id, position, attempt),
            "namespace": sandboxes.namespace,
            "labels": labels,
        },
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": deadline_seconds,
            "terminationGracePeriodSeconds": 10,
            # No identity towards the cluster, and no list of the cluster's services.
            "serviceAccountName": sandboxes.service_account,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            # Only on the node reserved for sandboxes, which nothing else may use.
            "nodeSelector": {NODE_LABEL: "true"},
            "tolerations": [
                {"key": NODE_LABEL, "operator": "Equal", "value": "true", "effect": "NoSchedule"}
            ],
            "imagePullSecrets": [{"name": sandboxes.pull_secret}],
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": SANDBOX_UID,
                "runAsGroup": SANDBOX_UID,
                "fsGroup": SANDBOX_UID,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "step",
                    "image": sandboxes.image,
                    "args": [RUNNER],
                    "workingDir": WORKSPACE,
                    "env": [
                        {
                            "name": "SANDBOX_JOB",
                            "value": json.dumps(job, separators=(",", ":"), sort_keys=True),
                        },
                        *_way_out(sandboxes, egress),
                    ],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "250m", "memory": "512Mi"},
                        "limits": {"cpu": cpu, "memory": memory},
                    },
                    "volumeMounts": mounts,
                }
            ],
            "volumes": volumes,
        },
    }
