from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import WorkerDeploymentConfig

WORKER_DEPLOYMENT_NAME = "engineering-control"

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


def worker_deployment_kwargs(build_id: str | None) -> dict[str, Any]:
    """Worker() kwargs enabling Temporal Worker Deployments versioning (F-11 (a)).

    Returns {} -- no versioning, byte-identical to today's behavior -- when
    build_id is unset. All workers share one deployment identity
    (WORKER_DEPLOYMENT_NAME) because they are always built and deployed
    together from the same git commit/release. AUTO_UPGRADE (rather than
    PINNED) matches this codebase's existing workflow.patched()-based
    replay-safety model: in-flight executions are expected to pick up new
    worker code, not stay pinned to the build they started on indefinitely.

    A new build_id's worker does not receive any new workflow executions
    until its version is explicitly promoted, e.g. via
    `temporal worker deployment set-current-version` -- see
    scripts/activate-worker-deployment.sh and docs/RUNBOOK.md.
    """
    if not build_id:
        return {}
    return {
        "deployment_config": WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(
                deployment_name=WORKER_DEPLOYMENT_NAME, build_id=build_id
            ),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.AUTO_UPGRADE,
        ),
    }
