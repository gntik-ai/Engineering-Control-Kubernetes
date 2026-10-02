"""StagingGitOpsDeliveryWorkflow exactly as origin/main 012f249 ran it, before NEW-113.

Copied verbatim from that commit's workflows/definitions.py (only the class name and
``sandboxed=False`` differ) so replay tests record genuinely pre-change histories, not
histories of the current class with its patch forced off. Do not edit.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from typing import Any

from temporalio import workflow

from engineering_control.workflows.definitions import ControlledWorkflow
from engineering_control.workflows.inputs import StagingGitOpsDeliveryInput


@workflow.defn(name="StagingGitOpsDeliveryWorkflow", sandboxed=False)
class ParentStagingGitOpsDeliveryWorkflow(ControlledWorkflow):
    """Post-merge staging GitOps delivery: checks, merge, build, gate, pin, sync, verify.

    Every activity is short; waiting happens here with durable timers so a 90-minute
    CI run never holds an activity slot. Fails closed with one precise blocker.
    """

    _MERGE_ROUNDS = 20
    _REMEDIATION_ROUNDS = 3

    @workflow.run
    async def run(self, request: StagingGitOpsDeliveryInput) -> dict[str, Any]:
        prepared = await self._step("gitops.prepare", asdict(request))
        if prepared.get("status") == "ALREADY_VERIFIED":
            return {"status": "STAGING_VERIFIED", "already_verified": True}
        if prepared.get("status") != "READY" or not isinstance(prepared.get("plan"), dict):
            return {
                "status": "BLOCKED",
                "blocker": str(prepared.get("blocker") or "STAGING_GITOPS_PREPARE_FAILED"),
            }
        base: dict[str, Any] = {"plan": prepared["plan"], "project": request.project}
        self._write_notice = {
            "project": request.project,
            "backlog_campaign_id": request.backlog_campaign_id,
            "issue": f"{request.issue_repository}#{request.issue_number}",
        }
        flow = prepared.get("staging_flow")
        flow = flow if isinstance(flow, dict) else {}
        evidence: dict[str, Any] = {}
        outcome = await self._deliver(base, flow, evidence)
        await self._step(
            "gitops.persist_result",
            {
                **base,
                **evidence,
                "outcome": outcome["status"],
                "blocker": outcome.get("blocker"),
            },
        )
        return {**outcome, "evidence": evidence}

    async def _poll(
        self,
        step: str,
        payload: dict[str, Any],
        *,
        pending: frozenset[str],
        attempts: int,
        interval: timedelta,
        write: bool = False,
    ) -> dict[str, Any]:
        """Call a short activity until it leaves ``pending``; TIMED_OUT when bounded out."""
        result: dict[str, Any] = {}
        for attempt in range(attempts):
            if attempt:
                await workflow.sleep(interval)
            if not await self._checkpoint(step.upper().replace(".", "_")):
                return {"status": "CANCELLED", "blocker": "CANCELLED"}
            if write:
                result = await self._github_write_step(step, payload)
            else:
                result = await self._step(step, payload)
            if result.get("status") not in pending:
                return result
        return {**result, "status": "TIMED_OUT"}

    @staticmethod
    def _blocked(result: dict[str, Any], default: str) -> dict[str, Any]:
        if result.get("status") == "CANCELLED":
            return {"status": "CANCELLED", "blocker": "CANCELLED"}
        return {"status": "BLOCKED", "blocker": str(result.get("blocker") or default)}

    async def _deliver(
        self, base: dict[str, Any], flow: dict[str, Any], evidence: dict[str, Any]
    ) -> dict[str, Any]:
        plan = base["plan"]
        minute = timedelta(minutes=1)
        half_minute = timedelta(seconds=30)

        # 1. Source PR checks (~90 minutes).
        checks = await self._poll(
            "gitops.pr_checks",
            base,
            pending=frozenset({"CHECKS_PENDING"}),
            attempts=90,
            interval=minute,
        )
        if checks.get("status") == "TIMED_OUT":
            return {"status": "BLOCKED", "blocker": "SOURCE_PR_CI_TIMEOUT"}
        # NEW-63: keep the baseline gate's verdict (job names only) for the ChangeSet record;
        # a CI-repair re-delivery hands the regressions to the maker (NEW-64).
        baseline = checks.get("ci_baseline")
        if isinstance(baseline, dict):
            evidence["ci_baseline_pr"] = baseline.get("pr")
            evidence["ci_preexisting_failures"] = baseline.get("preexisting_failures") or []
        if checks.get("status") != "CHECKS_PASSED":
            if checks.get("blocker") == "SOURCE_PR_CI_REGRESSION":
                evidence["ci_regressions"] = checks.get("failed_checks") or []
                if checks.get("reason"):
                    # NEW-99: the failures were not compared with unchanged main.
                    evidence["ci_regression_reason"] = checks.get("reason")
                if checks.get("failure_excerpts"):
                    # NEW-100: the repair maker needs the failing lines, not only job names.
                    evidence["ci_failure_excerpts"] = checks.get("failure_excerpts")
            return self._blocked(checks, "SOURCE_PR_CI_FAILED")

        # 1b. NEW-103: the deployment PR's CI must also pass before either PR merges,
        # so a deployment failure can no longer leave the source merged on its own.
        if plan.get("deployment_pr_number") and workflow.patched("gitops-deployment-ci-gate-v1"):
            deployment_checks = await self._poll(
                "gitops.deployment_pr_checks",
                base,
                pending=frozenset({"CHECKS_PENDING"}),
                attempts=90,
                interval=minute,
            )
            if deployment_checks.get("status") == "TIMED_OUT":
                return {"status": "BLOCKED", "blocker": "DEPLOYMENT_PR_CI_TIMEOUT"}
            if deployment_checks.get("status") != "CHECKS_PASSED":
                if deployment_checks.get("blocker") == "DEPLOYMENT_PR_CI_REGRESSION":
                    evidence["ci_regressions"] = deployment_checks.get("failed_checks") or []
                    evidence["ci_regression_reason"] = deployment_checks.get("reason")
                    evidence["ci_regression_target"] = "deployment"
                    if deployment_checks.get("failure_excerpts"):
                        evidence["ci_failure_excerpts"] = deployment_checks.get("failure_excerpts")
                return self._blocked(deployment_checks, "DEPLOYMENT_PR_CI_FAILED")

        # 2. Ready + merge (source first, then the deployment PR when one exists).
        targets = ["source"]
        if plan.get("deployment_pr_number"):
            targets.append("deployment")
        for target in targets:
            merged = await self._poll(
                "gitops.merge",
                {**base, "target": target},
                pending=frozenset({"MERGE_PENDING"}),
                attempts=self._MERGE_ROUNDS,
                interval=half_minute,
                write=True,
            )
            if merged.get("status") == "TIMED_OUT":
                return {"status": "BLOCKED", "blocker": f"{target.upper()}_PR_MERGE_BLOCKED"}
            if merged.get("status") != "MERGED":
                return self._blocked(merged, "PROTECTED_MERGE_FAILED")
            key = "merge_sha" if target == "source" else "deployment_merge_sha"
            evidence[key] = str(merged.get("merge_sha", ""))
        merge_sha = evidence["merge_sha"]
        merged_base = {**base, "merge_sha": merge_sha}

        # 3. Release tag (optional) and the image-producing builds.
        if flow.get("release_tag_enabled"):
            if not await self._checkpoint("GITOPS_RELEASE_TAG"):
                return {"status": "CANCELLED", "blocker": "CANCELLED"}
            tagged = await self._github_write_step("gitops.release_tag", merged_base)
            if tagged.get("status") != "TAG_READY":
                return self._blocked(tagged, "RELEASE_TAG_FAILED")
            evidence["release_tag"] = tagged.get("release_tag")
        for role, configured, timeout_blocker in (
            ("main_ci", flow.get("main_ci_workflow"), "MAIN_CI_TIMEOUT"),
            ("image", flow.get("image_workflow"), "IMAGE_BUILD_TIMEOUT"),
        ):
            if not configured:
                continue
            run = await self._poll(
                "gitops.workflow_run",
                {**merged_base, "workflow_role": role},
                pending=frozenset({"RUN_PENDING"}),
                attempts=90,
                interval=minute,
            )
            if run.get("status") == "TIMED_OUT":
                return {"status": "BLOCKED", "blocker": timeout_blocker}
            if run.get("status") != "RUN_SUCCEEDED":
                return self._blocked(
                    run, "MAIN_CI_FAILED" if role == "main_ci" else "IMAGE_BUILD_FAILED"
                )
            if role == "image" and run.get("release_sha"):
                # NEW-68: a later release containing the merge ships this change; every
                # following step pins, syncs and verifies that release's commit.
                evidence["release_tag"] = run.get("release_tag")
                evidence["release_sha"] = str(run["release_sha"])
        deployed_base = {
            **merged_base,
            "merge_sha": str(evidence.get("release_sha") or merge_sha),
            "delivered_merge_sha": merge_sha,
        }

        # 4. Environment gate with bounded remediation.
        gate = await self._environment_gate(base, evidence)
        if gate is not None:
            return gate

        # 5. Image pin written by argocd-image-updater (~30 minutes).
        pin = await self._poll(
            "gitops.image_pin",
            deployed_base,
            pending=frozenset({"PIN_PENDING"}),
            attempts=30,
            interval=minute,
        )
        if pin.get("status") == "TIMED_OUT":
            return {"status": "BLOCKED", "blocker": "IMAGE_NOT_PINNED"}
        if pin.get("status") != "PINNED":
            return self._blocked(pin, "IMAGE_NOT_PINNED")
        evidence["image_tag"] = pin.get("image_tag")
        evidence["pinned_repositories"] = list(pin.get("pinned_repositories") or [])

        # 6. Hermes-triggered Argo sync (~20 minutes).
        triggered = await self._poll(
            "gitops.trigger_sync",
            deployed_base,
            pending=frozenset({"SYNC_BUSY"}),
            attempts=10,
            interval=half_minute,
        )
        if triggered.get("status") == "TIMED_OUT":
            return {"status": "BLOCKED", "blocker": "ARGO_OPERATION_BUSY"}
        if triggered.get("status") != "SYNC_TRIGGERED":
            return self._blocked(triggered, "ARGO_SYNC_FAILED")
        evidence["chart_pin"] = triggered.get("chart_pin")
        synced = await self._poll(
            "gitops.sync_status",
            {
                **deployed_base,
                "previous_operation_started_at": triggered.get("previous_operation_started_at"),
            },
            pending=frozenset({"SYNC_PENDING"}),
            attempts=40,
            interval=half_minute,
        )
        if synced.get("status") == "TIMED_OUT":
            return {"status": "BLOCKED", "blocker": "ARGO_SYNC_TIMEOUT"}
        if synced.get("status") != "SYNCED":
            return self._blocked(synced, "ARGO_SYNC_FAILED")
        evidence["argo_revision"] = synced.get("argo_revision")

        # 7. Verify every pinned workload runs the new tag and is fully ready.
        verified = await self._poll(
            "gitops.verify_workloads",
            {**deployed_base, "pinned_repositories": evidence["pinned_repositories"]},
            pending=frozenset({"VERIFICATION_PENDING"}),
            attempts=10,
            interval=half_minute,
        )
        if verified.get("status") != "VERIFIED":
            if verified.get("status") == "CANCELLED":
                return self._blocked(verified, "CANCELLED")
            return {"status": "BLOCKED", "blocker": "STAGING_VERIFICATION_FAILED"}

        # 8. Close the delivered issue with the delivery evidence.
        if not await self._checkpoint("GITOPS_CLOSE_ISSUE"):
            return {"status": "CANCELLED", "blocker": "CANCELLED"}
        closed = await self._github_write_step(
            "gitops.close_issue",
            {
                **deployed_base,
                "release_tag": evidence.get("release_tag"),
                "pinned_repositories": evidence["pinned_repositories"],
                "argo_revision": evidence.get("argo_revision"),
            },
        )
        if closed.get("status") != "ISSUE_CLOSED":
            return self._blocked(closed, "DELIVERED_ISSUE_CLOSE_FAILED")
        return {"status": "STAGING_VERIFIED"}

    async def _environment_gate(
        self, base: dict[str, Any], evidence: dict[str, Any]
    ) -> dict[str, Any] | None:
        check = await self._step("gitops.environment_check", base)
        issue_number: int | None = None
        rounds = 0
        while check.get("status") != "HEALTHY":
            if check.get("status") != "UNHEALTHY":
                return self._blocked(check, "ENVIRONMENT_CHECK_FAILED")
            if rounds >= self._REMEDIATION_ROUNDS:
                evidence["remediation_rounds"] = rounds
                return {"status": "BLOCKED", "blocker": "ENVIRONMENT_UNHEALTHY"}
            rounds += 1
            if not await self._checkpoint(f"GITOPS_REMEDIATION_{rounds}"):
                return {"status": "CANCELLED", "blocker": "CANCELLED"}
            issue = await self._github_write_step(
                "gitops.environment_issue",
                {
                    **base,
                    "action": "report",
                    "round": rounds,
                    "diagnosis": check.get("diagnosis"),
                    "issue_number": issue_number,
                },
            )
            if issue.get("status") not in {"ISSUE_OPENED", "ISSUE_COMMENTED"}:
                return self._blocked(issue, "ENVIRONMENT_ISSUE_FAILED")
            issue_number = int(issue["issue_number"])
            evidence["environment_issue_number"] = issue_number
            await self._step("gitops.remediate", {**base, "round": rounds})
            await workflow.sleep(timedelta(minutes=2))
            check = await self._step("gitops.environment_check", base)
        evidence["remediation_rounds"] = rounds
        if issue_number is not None:
            resolved = await self._github_write_step(
                "gitops.environment_issue",
                {**base, "action": "resolve", "issue_number": issue_number},
            )
            if resolved.get("status") != "ISSUE_RESOLVED":
                return self._blocked(resolved, "ENVIRONMENT_ISSUE_FAILED")
        return None
