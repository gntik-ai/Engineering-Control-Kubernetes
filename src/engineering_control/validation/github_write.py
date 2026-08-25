from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from engineering_control.workflows.worker_registry import GITHUB_TASK_QUEUE

DENIAL_ACCEPTANCE_STATUS = "PASS_DENIAL_ENFORCED"


def classify_write_denial(result: dict[str, Any], *, activity_starts: int) -> str:
    required = {
        "status": "BLOCKED_BY_POLICY",
        "terminal": True,
        "retryable": False,
        "external_command_started": False,
        "external_mutation_attempted": False,
        "mutating_command_constructed": False,
        "external_command_count": 0,
        "external_mutation_attempt_count": 0,
        "mutating_command_construction_count": 0,
    }
    if any(result.get(key) != value for key, value in required.items()):
        raise RuntimeError("GitHub write activity did not enforce the terminal denial contract")
    policy = result.get("policy")
    if not isinstance(policy, dict) or policy != {
        "enabled": False,
        "repository_allowed": False,
    }:
        raise RuntimeError("GitHub write activity did not report the disabled policy state")
    if result.get("reason_code") != "POLICY_DISABLED":
        raise RuntimeError("GitHub write activity did not report the canonical disabled reason")
    if activity_starts != 1:
        raise RuntimeError("GitHub write denial was unconsumed or retried")
    return DENIAL_ACCEPTANCE_STATUS


@workflow.defn(name="GitHubWriteDenialAcceptanceWorkflow")
class GitHubWriteDenialAcceptanceWorkflow:
    """Ephemeral local workflow that reaches the real typed GitHub activity queue."""

    @workflow.run
    async def run(self) -> dict[str, Any]:
        result = await workflow.execute_activity(
            "execute_control_step",
            {
                "step": "finding.publish",
                "payload": {
                    "acceptance_only": True,
                    "status": "PUBLICATION_READY",
                    "finding_publication": {"repository": "gntik-ai/llmwiki"},
                },
            },
            task_queue=GITHUB_TASK_QUEUE,
            schedule_to_start_timeout=timedelta(seconds=30),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        if not isinstance(result, dict):
            raise TypeError("GitHub write-denial activity returned a non-object result")
        return result
