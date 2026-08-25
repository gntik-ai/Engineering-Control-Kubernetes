from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

WorkerHost = Literal["controller", "worker"]
WorkerKind = Literal[
    "controller",
    "codex",
    "claude",
    "browser",
    "scan",
    "delivery",
    "release",
    "github",
    "synthetic",
]
RoleWorkerKind = Literal["codex", "claude", "browser", "scan", "delivery", "release", "github"]

CONTROL_TASK_QUEUE = "engineering-control"
ACCEPTANCE_TASK_QUEUE = "engineering-acceptance"


@dataclass(frozen=True)
class RoleWorkerSpec:
    kind: RoleWorkerKind
    host: WorkerHost
    unix_user: str
    service: str
    task_queue: str
    max_concurrent_activities: int = 4


ROLE_WORKER_REGISTRY: dict[RoleWorkerKind, RoleWorkerSpec] = {
    "codex": RoleWorkerSpec(
        kind="codex",
        host="worker",
        unix_user="codex-runner",
        service="engineering-worker@codex.service",
        task_queue="engineering-codex",
        max_concurrent_activities=2,
    ),
    "claude": RoleWorkerSpec(
        kind="claude",
        host="worker",
        unix_user="claude-runner",
        service="engineering-worker@claude.service",
        task_queue="engineering-claude",
        max_concurrent_activities=2,
    ),
    "browser": RoleWorkerSpec(
        kind="browser",
        host="worker",
        unix_user="browser-runner",
        service="engineering-worker@browser.service",
        task_queue="engineering-browser",
    ),
    "scan": RoleWorkerSpec(
        kind="scan",
        host="worker",
        unix_user="scan-runner",
        service="engineering-worker@scan.service",
        task_queue="engineering-scan",
    ),
    "delivery": RoleWorkerSpec(
        kind="delivery",
        host="worker",
        unix_user="delivery-runner",
        service="engineering-worker@delivery.service",
        task_queue="engineering-delivery",
    ),
    "release": RoleWorkerSpec(
        kind="release",
        host="worker",
        unix_user="release-runner",
        service="engineering-release-worker.service",
        task_queue="engineering-release",
    ),
    "github": RoleWorkerSpec(
        kind="github",
        host="controller",
        unix_user="hermes",
        service="engineering-github-worker.service",
        task_queue="engineering-github",
    ),
}

ROLE_TASK_QUEUES = {kind: spec.task_queue for kind, spec in ROLE_WORKER_REGISTRY.items()}
GITHUB_TASK_QUEUE = ROLE_WORKER_REGISTRY["github"].task_queue
PROVIDER_WORKER_KINDS = frozenset({"codex", "claude"})


def role_worker_spec(kind: str) -> RoleWorkerSpec:
    try:
        return ROLE_WORKER_REGISTRY[kind]  # type: ignore[index]
    except KeyError as error:
        raise ValueError("ENGINEERING_WORKER_KIND must name a bounded worker role") from error


def task_queue_for(kind: RoleWorkerKind) -> str:
    return ROLE_WORKER_REGISTRY[kind].task_queue


def provider_task_queue(provider: str, *, default: Literal["codex", "claude"]) -> str:
    selected = provider or default
    if selected not in PROVIDER_WORKER_KINDS:
        raise ValueError("provider activity requires codex or claude")
    return ROLE_TASK_QUEUES[selected]  # type: ignore[index]
