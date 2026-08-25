#!/usr/bin/env python3
from __future__ import annotations

import argparse
import grp
import os
import stat
import sys
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from engineering_control.activities.core import ControlActivities  # noqa: E402
from engineering_control.activities.delivery import DeliveryActivityHandler  # noqa: E402
from engineering_control.activities.github import GitHubActivityHandler  # noqa: E402
from engineering_control.policy.github_write import (  # noqa: E402
    GITHUB_ACTIVITY_STEPS,
    GITHUB_WRITE_POLICY_PATH,
)
from engineering_control.workflows.definitions import ControlledWorkflow  # noqa: E402
from engineering_control.workflows.worker_registry import (  # noqa: E402
    GITHUB_TASK_QUEUE,
    ROLE_WORKER_REGISTRY,
)

GENERIC_KINDS = frozenset({"codex", "claude", "browser", "scan", "delivery"})
GITHUB_STEPS = GitHubActivityHandler.supported_steps | DeliveryActivityHandler.supported_steps


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _unit_value(unit: str, key: str) -> str:
    prefix = f"{key}="
    values = [line[len(prefix) :] for line in unit.splitlines() if line.startswith(prefix)]
    _require(len(values) == 1, f"{key} must occur exactly once")
    return values[0]


def _unit_environment(unit: str, name: str) -> str:
    prefix = f"Environment={name}="
    values = [line[len(prefix) :] for line in unit.splitlines() if line.startswith(prefix)]
    _require(len(values) == 1, f"{name} environment must occur exactly once")
    return values[0]


def verify_source_contract(repository_root: Path) -> None:
    systemd = repository_root / "systemd"
    scripts = repository_root / "scripts"
    controller_bootstrap = (scripts / "bootstrap-controller.sh").read_text(encoding="utf-8")
    worker_bootstrap = (scripts / "bootstrap-worker.sh").read_text(encoding="utf-8")
    generic_unit = (systemd / "engineering-worker@.service").read_text(encoding="utf-8")
    release_unit = (systemd / "engineering-release-worker.service").read_text(encoding="utf-8")
    github_unit = (systemd / "engineering-github-worker.service").read_text(encoding="utf-8")

    _require(
        _unit_environment(generic_unit, "ENGINEERING_WORKER_KIND") == "%i",
        "generic worker kind drifted",
    )
    _require(_unit_value(generic_unit, "User") == "%i-runner", "generic worker user drifted")
    _require(
        _unit_value(generic_unit, "ExecStart").endswith("/engineering-role-worker"),
        "generic worker entrypoint drifted",
    )
    _require(
        _unit_environment(release_unit, "ENGINEERING_WORKER_KIND") == "release",
        "release worker kind drifted",
    )
    _require(_unit_value(release_unit, "User") == "release-runner", "release worker user drifted")
    _require(
        _unit_environment(github_unit, "ENGINEERING_WORKER_KIND") == "github",
        "GitHub worker kind drifted",
    )
    _require(_unit_value(github_unit, "User") == "hermes", "GitHub worker must run as hermes")
    _require(_unit_value(github_unit, "Group") == "hermes", "GitHub worker group drifted")
    _require(
        _unit_value(github_unit, "SupplementaryGroups") == "engineering-control",
        "GitHub worker policy-reader group drifted",
    )
    _require(
        _unit_value(github_unit, "WorkingDirectory") == "/opt/engineering-control/source",
        "GitHub worker release path drifted",
    )
    _require(
        _unit_value(github_unit, "ExecStart").endswith("/engineering-role-worker"),
        "GitHub worker entrypoint drifted",
    )
    _require("Environment=HOME=/home/hermes" in github_unit, "GitHub worker HOME is missing")
    _require(
        f"Environment=ENGINEERING_GITHUB_ISSUE_WRITE_VALIDATION_FILE={GITHUB_WRITE_POLICY_PATH}"
        in github_unit,
        "GitHub worker policy path drifted",
    )
    _require(
        str(GITHUB_WRITE_POLICY_PATH) in _unit_value(github_unit, "ReadOnlyPaths"),
        "GitHub worker policy is not read-only",
    )
    _require(
        "Environment=PATH=/usr/local/bin:/usr/bin:/bin" in github_unit,
        "GitHub worker PATH is not deterministic",
    )
    for forbidden in ("GITHUB_TOKEN", "GH_TOKEN", "auth token", "worker-common.env"):
        _require(forbidden not in github_unit, f"GitHub unit contains forbidden value: {forbidden}")

    for kind, spec in ROLE_WORKER_REGISTRY.items():
        steps = ControlActivities.steps_for_worker(kind)
        _require(steps, f"{kind} has no registered activities")
        _require(spec.task_queue.startswith("engineering-"), f"{kind} task queue is invalid")
        if kind in GENERIC_KINDS:
            _require(
                spec.service in worker_bootstrap, f"{kind} service is absent from worker bootstrap"
            )
            _require(spec.host == "worker", f"{kind} generic worker host drifted")
        elif kind == "release":
            _require(
                spec.service in worker_bootstrap, "release service is absent from worker bootstrap"
            )
            _require(spec.host == "worker", "release worker host drifted")
        elif kind == "github":
            _require(
                spec.service in controller_bootstrap,
                "GitHub service is absent from controller bootstrap",
            )
            _require(
                spec.service not in worker_bootstrap,
                "GitHub service must not be installed on worker",
            )
            _require(spec.host == "controller", "GitHub worker host drifted")
            _require(spec.unix_user == "hermes", "GitHub worker identity drifted")

    github_steps = ControlActivities.steps_for_worker("github")
    _require(github_steps == GITHUB_STEPS, "GitHub activity dispatcher and typed handlers differ")
    _require(
        github_steps == GITHUB_ACTIVITY_STEPS,
        "GitHub activity classification and dispatcher differ",
    )
    for step in github_steps:
        queue = ControlledWorkflow._activity_queue(step, {})
        _require(queue == GITHUB_TASK_QUEUE, f"{step} is routed to {queue}")

    _require("systemctl daemon-reload" in controller_bootstrap, "controller daemon-reload missing")
    _require(
        "systemctl enable engineering-api.service" in controller_bootstrap
        and "engineering-github-worker.service" in controller_bootstrap,
        "controller GitHub enable path missing",
    )
    _require(
        'if [[ ! -f "${CONFIG_ROOT}/env/github-worker.env" ]]' in controller_bootstrap,
        "GitHub environment creation is not idempotent",
    )
    _require(
        "github-issue-write-validation.yaml" in controller_bootstrap,
        "controller write-policy preservation is missing",
    )


def verify_write_policy(path: Path) -> None:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), "GitHub write policy must be an object")
    _require(value.get("schema_version") == 1, "GitHub write policy schema drifted")
    _require(value.get("enabled") is False, "GitHub write policy must remain disabled")
    _require(value.get("safe_repositories") == [], "GitHub safe repository list must be empty")
    if path == GITHUB_WRITE_POLICY_PATH:
        _require(not path.is_symlink(), "GitHub write policy must not be a symlink")
        metadata = path.stat()
        parent_metadata = path.parent.stat()
        expected_group = grp.getgrnam("engineering-control").gr_gid
        _require(metadata.st_uid == 0, "GitHub write policy owner must be root")
        _require(metadata.st_gid == expected_group, "GitHub write policy group drifted")
        _require(stat.S_IMODE(metadata.st_mode) == 0o640, "GitHub write policy mode drifted")
        _require(parent_metadata.st_uid == 0, "GitHub write policy parent owner must be root")
        _require(
            parent_metadata.st_gid == expected_group, "GitHub write policy parent group drifted"
        )
        _require(
            stat.S_IMODE(parent_metadata.st_mode) == 0o750,
            "GitHub write policy parent mode drifted",
        )
        _require(os.access(path, os.R_OK), "GitHub write policy is unreadable")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", choices=("controller", "worker"), required=True)
    parser.add_argument("--source-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--write-policy", type=Path)
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    verify_source_contract(source_root)
    write_policy = args.write_policy or (
        source_root / "config/policy/github-issue-write-validation.yaml"
    )
    verify_write_policy(write_policy.resolve())
    expected = sorted(spec.kind for spec in ROLE_WORKER_REGISTRY.values() if spec.host == args.host)
    print(f"PASS worker registry host={args.host} kinds={','.join(expected)}")


if __name__ == "__main__":
    main()
