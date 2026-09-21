from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import CommandFailure, SafeCommandRunner
from engineering_control.adapters.github.client import GhCliAuthError, GhCliBroker, GhCliPolicyError
from engineering_control.config import Settings
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.evidence.transfer import EvidencePublisher
from engineering_control.models.enums import RedactionStatus
from engineering_control.models.final_closure import (
    FinalArtifactPlan,
    FinalClosurePlan,
    FinalDeploymentMergePlan,
    FinalIssueMutationPlan,
    ProtectedMergeResult,
)
from engineering_control.models.reconciliation import (
    CandidateArtifactManifest,
    CIReconciliationPlan,
    PromotionPublication,
)
from engineering_control.policy.github_write import (
    WRITE_CAPABLE_GITHUB_STEPS,
    GitHubWritePolicyEvaluator,
)
from engineering_control.security.redaction import SecretRedactor


class RepositoryPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(
        pattern=r"^gntik-ai/(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)$"
    )
    repository_slug: str = Field(
        pattern=r"^(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)$"
    )
    working_directory: Path
    branch: str = Field(
        pattern=r"^agent/(falcone|musematic|llmwiki)/[A-Za-z0-9._-]+/[a-f0-9-]{36}$"
    )
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    base_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9._/-]{1,120}$")
    title: str = Field(min_length=1, max_length=240)
    body: str = Field(min_length=1, max_length=32_000)

    @model_validator(mode="after")
    def matching_repository_slug(self) -> RepositoryPublication:
        if self.repository.rsplit("/", 1)[-1] != self.repository_slug:
            raise ValueError("publication repository slug does not match its GitHub repository")
        return self


class DeferredDeployment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_slug: str = Field(pattern=r"^(falcone-charts|musematic-deploy)$")
    branch: str = Field(pattern=r"^agent/(falcone|musematic)/[A-Za-z0-9._-]+/[a-f0-9-]{36}$")
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    base_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    reason: str = Field(pattern=r"^AWAITING_IMMUTABLE_ARTIFACT$")


class PublicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    change_set_id: UUID
    publications: tuple[RepositoryPublication, ...] = Field(min_length=1, max_length=2)
    deferred_deployment: DeferredDeployment | None = None

    @model_validator(mode="after")
    def source_and_deployment_pair(self) -> PublicationPlan:
        slugs = {item.repository_slug for item in self.publications}
        if len(slugs) != len(self.publications):
            raise ValueError("publication repositories must be unique")
        full_pairs = (
            {"falcone", "falcone-charts"},
            {"musematic", "musematic-deploy"},
        )
        source_only = slugs in ({"falcone"}, {"musematic"})
        llmwiki_self_contained = slugs == {"llmwiki"}
        if slugs not in full_pairs and not source_only and not llmwiki_self_contained:
            raise ValueError("publication plan does not match a configured ChangeSet")
        if source_only:
            expected = "falcone-charts" if slugs == {"falcone"} else "musematic-deploy"
            if (
                self.deferred_deployment is None
                or self.deferred_deployment.repository_slug != expected
            ):
                raise ValueError("source-only publication must bind its deferred deployment")
        elif self.deferred_deployment is not None:
            raise ValueError("published deployment cannot also be deferred")
        if len({item.branch for item in self.publications}) != 1:
            raise ValueError("source and deployment publication must share one ChangeSet branch")
        branch = self.publications[0].branch
        if self.deferred_deployment is not None and self.deferred_deployment.branch != branch:
            raise ValueError("deferred deployment must share the ChangeSet branch")
        return self


class DeliveryActivityHandler:
    """Perform only typed, policy-bound GitHub delivery operations."""

    supported_steps = frozenset(
        {
            "finding.dedupe",
            "enhancement.github_dedupe",
            "enhancement.publish",
            "finding.publish",
            "reconcile.inspect_ci",
            "reconcile.publish_promotion",
            "delivery.publish",
            "final.merge_source",
            "final.inspect_artifact",
            "final.merge_deployment",
            "final.sync_worktrees",
            "final.comment_issue",
            "final.close_issue",
        }
    )
    write_capable_steps = WRITE_CAPABLE_GITHUB_STEPS

    def __init__(
        self,
        settings: Settings,
        runner: SafeCommandRunner | None = None,
        broker: GhCliBroker | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner or SafeCommandRunner(
            {"git": settings.git_executable, "gh": settings.github_executable}
        )
        self.redactor = SecretRedactor()
        broker_root = (settings.run_root / "delivery").resolve()
        broker_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.broker = broker or GhCliBroker(
            self.runner,
            broker_root,
            writes_enabled=settings.github_writes_enabled,
            authenticated_home=settings.github_authenticated_home,
        )
        self.write_policy = GitHubWritePolicyEvaluator(
            policy_path=settings.github_issue_write_validation_file,
            project_config_root=settings.project_config_root,
            production=settings.environment == "production",
            writes_enabled=settings.github_writes_enabled and self.broker.writes_enabled,
        )

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        if step in self.write_capable_steps:
            denial = self.write_policy.evaluate(lambda: self._write_repositories(step, payload))
            if denial is not None:
                return denial.model_dump(mode="json")
        if step == "finding.dedupe":
            return await asyncio.to_thread(self._dedupe_finding, payload)
        if step == "enhancement.github_dedupe":
            return await asyncio.to_thread(self._dedupe_enhancement, payload)
        if step == "enhancement.publish":
            return await asyncio.to_thread(self._publish_enhancement, payload)
        if step == "finding.publish":
            return await asyncio.to_thread(self._publish_finding, payload)
        if step == "reconcile.inspect_ci":
            return await asyncio.to_thread(self._inspect_ci, payload)
        if step == "reconcile.publish_promotion":
            return await asyncio.to_thread(self._publish_promotion, payload)
        if step == "final.merge_source":
            return await asyncio.to_thread(self._merge_bound_pull_request, payload, source=True)
        if step == "final.inspect_artifact":
            return await asyncio.to_thread(self._inspect_final_artifact, payload)
        if step == "final.merge_deployment":
            return await asyncio.to_thread(self._merge_bound_pull_request, payload, source=False)
        if step == "final.sync_worktrees":
            return await asyncio.to_thread(self._sync_final_worktrees, payload)
        if step == "final.comment_issue":
            return await asyncio.to_thread(self._comment_final_evidence, payload)
        if step == "final.close_issue":
            return await asyncio.to_thread(self._close_final_issue, payload)
        if step != "delivery.publish":
            raise ApplicationError("unsupported delivery activity", non_retryable=True)
        try:
            plan = PublicationPlan.model_validate(payload.get("publication_plan"))
            results = await asyncio.to_thread(self._publish_all, plan)
        except GhCliAuthError:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "BLOCKED_BY_GH_AUTH",
            }
        except (CommandFailure, OSError, ValueError) as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "GITHUB_PUBLICATION_FAILED",
                "error_type": type(error).__name__,
            }
        safe = self.redactor.redact(
            {**payload, "status": "SOURCE_PRS_CREATED", "publication_results": results}
        ).value
        if not isinstance(safe, dict):
            raise TypeError("redacted delivery publication must remain an object")
        return safe

    def _inspect_ci(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = CIReconciliationPlan.model_validate(payload.get("reconciliation_plan"))
            root = (self.settings.run_root / "delivery").resolve()
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.broker.preflight((plan.repository,))
            pull_request = self._pull_request(plan)
            ci_evidence = self._evaluate_pull_request(plan, pull_request)
            if ci_evidence["status"] != "CI_VALIDATED":
                return self._with_ci_evidence(
                    payload,
                    plan,
                    ci_evidence,
                    status=str(ci_evidence["status"]),
                    candidate_status=None,
                )
            build_policy = plan.policy.candidate_build
            if not build_policy.enabled:
                return self._with_ci_evidence(
                    payload,
                    plan,
                    ci_evidence,
                    status="CI_VALIDATED",
                    candidate_status=build_policy.blocker,
                )
            if plan.candidate_build_run_id is None:
                return self._with_ci_evidence(
                    payload,
                    plan,
                    ci_evidence,
                    status="CI_VALIDATED",
                    candidate_status="AWAITING_IMMUTABLE_ARTIFACT",
                )
            registry_auth = self._registry_auth()
            if registry_auth is None:
                return self._with_ci_evidence(
                    payload,
                    plan,
                    ci_evidence,
                    status="CI_VALIDATED",
                    candidate_status="MANUAL_GITHUB_REGISTRY_AUTH_REQUIRED",
                )
            manifest, candidate_evidence = self._candidate_manifest(plan, root, registry_auth)
            combined = {**ci_evidence, "candidate": candidate_evidence}
            return self._with_ci_evidence(
                payload,
                plan,
                combined,
                status="CANDIDATE_VERIFIED",
                candidate_status="VERIFIED",
                candidate_manifest=manifest.model_dump(mode="json"),
            )
        except GhCliAuthError:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "BLOCKED_BY_GH_AUTH",
            }
        except (
            CommandFailure,
            OSError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ) as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "CI_RECONCILIATION_FAILED",
                "error_type": type(error).__name__,
            }

    def _pull_request(
        self,
        plan: CIReconciliationPlan,
    ) -> dict[str, Any]:
        return self.broker.view_pull_request(plan.repository, plan.source_pr_number)

    @staticmethod
    def _evaluate_pull_request(
        plan: CIReconciliationPlan, pull_request: dict[str, Any]
    ) -> dict[str, Any]:
        expected = {
            "number": plan.source_pr_number,
            "url": plan.source_pr_url,
            "state": "OPEN",
            "headRefName": plan.source_branch,
            "headRefOid": plan.source_head_sha,
            "baseRefName": plan.source_base_branch,
        }
        if any(pull_request.get(key) != value for key, value in expected.items()):
            return {
                "status": "BLOCKED",
                "blocker": "SOURCE_PR_IMMUTABLE_BINDING_CHANGED",
            }
        raw_checks = pull_request.get("statusCheckRollup")
        if not isinstance(raw_checks, list) or not raw_checks:
            return {"status": "CI_PENDING", "blocker": "SOURCE_PR_CHECKS_NOT_REPORTED"}
        normalized: list[dict[str, str | None]] = []
        expected_workflow_seen = False
        pending = False
        failed = False
        for raw in raw_checks:
            if not isinstance(raw, dict):
                raise ValueError("GitHub PR check result is invalid")
            workflow_name = str(raw.get("workflowName") or raw.get("workflow") or "")
            name = str(raw.get("name") or raw.get("context") or "")
            state = str(raw.get("status") or raw.get("state") or "").upper()
            conclusion = str(raw.get("conclusion") or raw.get("state") or "").upper()
            if workflow_name == plan.policy.source_ci_workflow:
                expected_workflow_seen = True
            if (
                state in {"QUEUED", "PENDING", "IN_PROGRESS", "REQUESTED", "WAITING"}
                or not conclusion
            ):
                pending = True
            elif conclusion not in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
                failed = True
            normalized.append(
                {
                    "name": name[:200],
                    "workflow": workflow_name[:160] or None,
                    "state": state[:40] or None,
                    "conclusion": conclusion[:40] or None,
                }
            )
        checks_sha = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        base = {
            "source_pr_number": plan.source_pr_number,
            "source_pr_url": plan.source_pr_url,
            "source_head_sha": plan.source_head_sha,
            "check_count": len(normalized),
            "checks_sha256": checks_sha,
            "merge_state": str(pull_request.get("mergeStateStatus") or "UNKNOWN")[:40],
            "review_decision": str(pull_request.get("reviewDecision") or "")[:40] or None,
        }
        if not expected_workflow_seen:
            return {**base, "status": "BLOCKED", "blocker": "REQUIRED_CI_WORKFLOW_MISSING"}
        if failed:
            return {**base, "status": "BLOCKED", "blocker": "SOURCE_PR_CHECKS_FAILED"}
        if pending:
            return {**base, "status": "CI_PENDING", "blocker": "SOURCE_PR_CHECKS_PENDING"}
        return {**base, "status": "CI_VALIDATED"}

    def _candidate_manifest(
        self,
        plan: CIReconciliationPlan,
        cwd: Path,
        registry_auth: Path,
    ) -> tuple[CandidateArtifactManifest, dict[str, Any]]:
        policy = plan.policy.candidate_build
        run_id = plan.candidate_build_run_id
        if run_id is None:
            raise ValueError("candidate build run is missing")
        run = self.broker.workflow_run(plan.repository, run_id)
        expected_run = {
            "databaseId": run_id,
            "workflowName": policy.workflow_name,
            "headSha": plan.source_head_sha,
            "status": "completed",
            "conclusion": "success",
        }
        if any(
            str(run.get(key)).lower() != str(value).lower() for key, value in expected_run.items()
        ):
            raise ValueError("candidate build run is not successful at the exact source SHA")
        with tempfile.TemporaryDirectory(prefix=f"candidate-{run_id}-", dir=cwd) as raw:
            download_root = Path(raw)
            self.broker.download_run_artifact(
                plan.repository, run_id, policy.artifact_name, download_root
            )
            files = sorted(path for path in download_root.rglob("*") if path.is_file())
            manifest_path = download_root / policy.manifest_file
            if files != [manifest_path] or manifest_path.is_symlink():
                raise ValueError("candidate artifact must contain exactly the configured manifest")
            if manifest_path.stat().st_size > 1_048_576:
                raise ValueError("candidate artifact manifest exceeds its size bound")
            manifest_bytes = manifest_path.read_bytes()
        manifest = CandidateArtifactManifest.model_validate_json(manifest_bytes)
        expected_images = plan.policy.immutable_promotion.image_repositories
        actual_images = {image.component: image.repository for image in manifest.images}
        if (
            manifest.project is not plan.project
            or manifest.source_repository != plan.repository
            or manifest.source_sha != plan.source_head_sha
            or manifest.workflow_run_id != run_id
            or actual_images != expected_images
        ):
            raise ValueError("candidate manifest changed its immutable project binding")
        attestation_hashes: dict[str, str] = {}
        for image in manifest.images:
            result = self.broker.verify_attestation(
                repository=plan.repository,
                subject=f"oci://{image.repository}@{image.digest}",
                signer_workflow=policy.signer_workflow,
                source_digest=plan.source_head_sha,
                deny_self_hosted_runners=policy.deny_self_hosted_runners,
                docker_config=registry_auth.parent,
            )
            if not result:
                raise ValueError("candidate image has no verified provenance attestation")
            attestation_hashes[image.component] = hashlib.sha256(
                json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        return manifest, {
            "workflow_run_id": run_id,
            "workflow_name": policy.workflow_name,
            "source_sha": plan.source_head_sha,
            "run_url": str(run.get("url") or "")[:500],
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "attestation_sha256": attestation_hashes,
            "image_count": len(manifest.images),
        }

    def _inspect_final_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = FinalArtifactPlan.model_validate(payload.get("final_artifact_plan"))
            policy = plan.policy.candidate_build
            if not policy.enabled:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": policy.blocker or "FINAL_BUILD_NOT_CONFIGURED",
                }
            runs = self.broker.list_workflow_runs(
                plan.closure.source.repository,
                workflow_name=policy.workflow_name,
                commit_sha=plan.final_source_sha,
            )
            exact = [
                item
                for item in runs
                if item.get("headSha") == plan.final_source_sha
                and item.get("workflowName") == policy.workflow_name
            ]
            successful = [
                item
                for item in exact
                if str(item.get("status", "")).lower() == "completed"
                and str(item.get("conclusion", "")).lower() == "success"
            ]
            if not successful:
                failed = any(
                    str(item.get("status", "")).lower() == "completed"
                    and str(item.get("conclusion", "")).lower() not in {"", "success"}
                    for item in exact
                )
                return {
                    **payload,
                    "status": "BLOCKED" if failed else "AWAITING_FINAL_BUILD",
                    "blocker": (
                        "FINAL_BUILD_FAILED" if failed else "FINAL_BUILD_NOT_YET_AVAILABLE"
                    ),
                }
            selected = max(successful, key=lambda item: int(item.get("databaseId", 0)))
            run_id = int(selected["databaseId"])
            independent_review_sha256 = str(payload.get("independent_review_sha256") or "")
            if not re.fullmatch(r"[a-f0-9]{64}", independent_review_sha256):
                raise ValueError("final artifact independent review binding is missing")
            reconciliation = CIReconciliationPlan(
                change_set_id=plan.closure.change_set_id,
                run_id=plan.closure.run_id,
                workflow_id=plan.closure.workflow_id,
                project=plan.closure.project,
                repository=plan.closure.source.repository,
                source_pr_number=plan.closure.source.number,
                source_pr_url=plan.closure.source.url,
                source_branch=plan.closure.source.head_branch,
                source_head_sha=plan.final_source_sha,
                source_base_branch=plan.closure.source.base_branch,
                candidate_build_run_id=run_id,
                policy=plan.policy,
                independent_review_sha256=independent_review_sha256,
                independent_review_provider=plan.closure.checker_provider,
                maker_provider=plan.closure.maker_provider,
            )
            registry_auth = self._registry_auth()
            if registry_auth is None:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "MANUAL_GITHUB_REGISTRY_AUTH_REQUIRED",
                }
            root = (self.settings.run_root / "delivery").resolve()
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            manifest, evidence = self._candidate_manifest(reconciliation, root, registry_auth)
            return {
                **payload,
                "status": "FINAL_ARTIFACT_VERIFIED",
                "final_build_run_id": run_id,
                "final_artifact_manifest": manifest.model_dump(mode="json"),
                "final_artifact_evidence": evidence,
            }
        except GhCliAuthError:
            return {**payload, "status": "BLOCKED", "blocker": "BLOCKED_BY_GH_AUTH"}
        except (
            CommandFailure,
            OSError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ) as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "FINAL_ARTIFACT_RECONCILIATION_FAILED",
                "error_type": type(error).__name__,
            }

    def _merge_bound_pull_request(self, payload: dict[str, Any], *, source: bool) -> dict[str, Any]:
        try:
            plan = FinalClosurePlan.model_validate(payload.get("final_closure_plan"))
            if source:
                binding = plan.source
            else:
                deployment_plan = FinalDeploymentMergePlan.model_validate(
                    payload.get("final_deployment_merge_plan")
                )
                if deployment_plan.closure != plan:
                    raise ValueError("final deployment merge closure binding changed")
                if payload.get("status") != "FINAL_DEPLOYMENT_MERGE_AUTHORIZED" or payload.get(
                    "deployment_merge_authorization_sha256"
                ) != canonical_sha256(deployment_plan.model_dump(mode="json")):
                    raise ValueError("final deployment merge lacks current persisted authorization")
                binding = deployment_plan.deployment
            expected_head = binding.head_sha
            result = self.broker.merge_pull_request(
                repository=binding.repository,
                number=binding.number,
                expected_head_sha=expected_head,
                expected_base_branch=binding.base_branch,
                change_binding=f"changeset:{plan.change_set_id}",
            )
            ProtectedMergeResult.model_validate(result)
            return {
                **payload,
                "status": "SOURCE_MERGED" if source else "DEPLOYMENT_MERGED",
                "source_merge" if source else "deployment_merge": result,
            }
        except GhCliAuthError:
            return {**payload, "status": "BLOCKED", "blocker": "BLOCKED_BY_GH_AUTH"}
        except (CommandFailure, OSError, ValueError, ValidationError, GhCliPolicyError) as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": (
                    "PROTECTED_SOURCE_MERGE_FAILED"
                    if source
                    else "PROTECTED_DEPLOYMENT_MERGE_FAILED"
                ),
                "error_type": type(error).__name__,
            }

    def _sync_final_worktrees(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = FinalClosurePlan.model_validate(payload.get("final_closure_plan"))
            source = ProtectedMergeResult.model_validate(payload.get("source_merge"))
            deployment = ProtectedMergeResult.model_validate(payload.get("deployment_merge"))
            synchronized: list[dict[str, str | bool]] = []
            for binding, result, slug in (
                (plan.source, source, plan.source.repository.rsplit("/", 1)[-1]),
                (
                    plan.deployment,
                    deployment,
                    plan.deployment.repository.rsplit("/", 1)[-1],
                ),
            ):
                unresolved = self.settings.worktree_root / slug / str(plan.change_set_id)
                if unresolved.is_symlink():
                    raise ValueError("final synchronization rejects symlinked worktrees")
                worktree = unresolved.resolve()
                expected = self.settings.worktree_root.resolve() / slug / str(plan.change_set_id)
                if worktree != expected or not worktree.is_dir():
                    raise ValueError("final synchronization escaped its ChangeSet worktree")
                status = self.runner.run(
                    "git",
                    ["-c", f"safe.directory={worktree}", "status", "--porcelain=v1"],
                    cwd=worktree,
                ).stdout
                if status:
                    raise ValueError("final synchronization requires a clean worktree")
                environment = {
                    "HOME": str(self.broker.authenticated_home),
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "safe.directory",
                    "GIT_CONFIG_VALUE_0": str(worktree),
                }
                self.runner.run(
                    "git",
                    [
                        "-c",
                        f"safe.directory={worktree}",
                        "fetch",
                        "--no-tags",
                        f"https://github.com/{binding.repository}.git",
                        result.merge_sha,
                    ],
                    cwd=worktree,
                    timeout_seconds=900,
                    environment=environment,
                )
                fetched = self.runner.run(
                    "git",
                    [
                        "-c",
                        f"safe.directory={worktree}",
                        "show",
                        "-s",
                        "--format=%T",
                        result.merge_sha,
                    ],
                    cwd=worktree,
                ).stdout.strip()
                if fetched != result.merge_tree_sha:
                    raise ValueError("final merge tree changed during synchronization")
                self.runner.run(
                    "git",
                    ["-c", f"safe.directory={worktree}", "reset", "--hard", result.merge_sha],
                    cwd=worktree,
                )
                final_head = self.runner.run(
                    "git",
                    ["-c", f"safe.directory={worktree}", "rev-parse", "HEAD"],
                    cwd=worktree,
                ).stdout.strip()
                final_status = self.runner.run(
                    "git",
                    ["-c", f"safe.directory={worktree}", "status", "--porcelain=v1"],
                    cwd=worktree,
                ).stdout
                if final_head != result.merge_sha or final_status:
                    raise ValueError("final synchronization did not produce a clean exact head")
                synchronized.append(
                    {
                        "repository": binding.repository,
                        "merge_sha": result.merge_sha,
                        "merge_tree_sha": result.merge_tree_sha,
                        "clean": True,
                    }
                )
            return {
                **payload,
                "status": "FINAL_WORKTREES_SYNCHRONIZED",
                "final_worktree_sync": synchronized,
            }
        except (CommandFailure, OSError, ValueError, ValidationError) as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "FINAL_WORKTREE_SYNCHRONIZATION_FAILED",
                "error_type": type(error).__name__,
            }

    def _comment_final_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = FinalIssueMutationPlan.model_validate(payload.get("final_issue_plan"))
            marker = f"<!-- engineering-control-final-closure:{plan.closure.change_set_id} -->"
            root = (self.settings.run_root / "delivery").resolve()
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, raw = tempfile.mkstemp(prefix="final-closure-", suffix=".md", dir=root)
            path = Path(raw)
            os.chmod(path, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(str(self.redactor.redact_text(plan.comment_body).value))
                result = self.broker.comment_issue(
                    repository=plan.closure.issue_repository,
                    number=plan.closure.issue_number,
                    body_file=path,
                    marker=marker,
                    change_binding=f"changeset:{plan.closure.change_set_id}",
                )
            finally:
                path.unlink(missing_ok=True)
            return {**payload, "status": "FINAL_EVIDENCE_COMMENTED", "comment_result": result}
        except GhCliAuthError:
            return {**payload, "status": "BLOCKED", "blocker": "BLOCKED_BY_GH_AUTH"}
        except (CommandFailure, OSError, ValueError, ValidationError, GhCliPolicyError) as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "FINAL_EVIDENCE_COMMENT_FAILED",
                "error_type": type(error).__name__,
            }

    def _close_final_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = FinalIssueMutationPlan.model_validate(payload.get("final_issue_plan"))
            if payload.get("status") != "FINAL_EVIDENCE_PERSISTED":
                raise ValueError("issue closure requires persisted final evidence")
            marker = f"<!-- engineering-control-final-closure:{plan.closure.change_set_id} -->"
            result = self.broker.close_issue(
                repository=plan.closure.issue_repository,
                number=plan.closure.issue_number,
                marker=marker,
                change_binding=f"changeset:{plan.closure.change_set_id}",
            )
            return {**payload, "status": "ISSUE_CLOSED", "issue_close_result": result}
        except GhCliAuthError:
            return {**payload, "status": "BLOCKED", "blocker": "BLOCKED_BY_GH_AUTH"}
        except (CommandFailure, OSError, ValueError, ValidationError, GhCliPolicyError) as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "FINAL_ISSUE_CLOSE_FAILED",
                "error_type": type(error).__name__,
            }

    def _with_ci_evidence(
        self,
        payload: dict[str, Any],
        plan: CIReconciliationPlan,
        evidence: dict[str, Any],
        *,
        status: str,
        candidate_status: str | None,
        candidate_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = {
            **evidence,
            "status": status,
            "candidate_status": candidate_status,
            "independent_review_sha256": plan.independent_review_sha256,
        }
        receipt = self._publish_reconciliation_evidence(plan, normalized)
        result: dict[str, Any] = {
            **payload,
            "status": status,
            "candidate_status": candidate_status,
            "ci_evidence": normalized,
            "ci_evidence_artifact": receipt,
        }
        if blocker := evidence.get("blocker"):
            result["blocker"] = str(blocker)
        if candidate_manifest is not None:
            result["candidate_manifest"] = candidate_manifest
        return result

    def _publish_reconciliation_evidence(
        self, plan: CIReconciliationPlan, value: dict[str, Any]
    ) -> dict[str, Any]:
        root = (self.settings.run_root / "delivery").resolve()
        directory = root / str(plan.run_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "ci-reconciliation.json"
        safe = self.redactor.redact(value).value
        descriptor, raw = tempfile.mkstemp(prefix="ci-", suffix=".json", dir=directory)
        temporary = Path(raw)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(safe, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        publisher = EvidencePublisher(self.settings, root)
        return publisher.publish(
            path,
            project=plan.project,
            workflow_id=plan.workflow_id,
            change_set_id=plan.change_set_id,
            category="changes",
            filename=f"ci-reconciliation-{plan.run_id}.json",
            producer="delivery-runner",
            content_type="application/json",
            redaction_status=RedactionStatus.NOT_REQUIRED,
        )

    def _publish_promotion(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            promotion = PromotionPublication.model_validate(payload.get("promotion_publication"))
            publication = RepositoryPublication.model_validate(promotion.model_dump())
            result = self._publish(publication)
        except GhCliAuthError:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "BLOCKED_BY_GH_AUTH",
            }
        except (CommandFailure, OSError, ValueError, ValidationError) as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "GITHUB_PROMOTION_PUBLICATION_FAILED",
                "error_type": type(error).__name__,
            }
        safe = self.redactor.redact(
            {**payload, "status": "DEPLOYMENT_PR_CREATED", "promotion_result": result}
        ).value
        if not isinstance(safe, dict):
            raise TypeError("redacted promotion publication must remain an object")
        return safe

    def _registry_auth(self) -> Path | None:
        path = self.settings.github_registry_auth_file
        if path is None or not path.exists():
            return None
        resolved = path.resolve()
        root = self.settings.delivery_credential_root.resolve()
        metadata = resolved.stat()
        mode = metadata.st_mode
        permissions = stat.S_IMODE(mode)
        if (
            not resolved.is_file()
            or not resolved.is_relative_to(root)
            or resolved.name != "config.json"
            or mode & stat.S_IRWXO
        ):
            raise ValueError("GitHub registry auth file is outside its secure credential root")
        if self.settings.environment == "production":
            if metadata.st_uid != 0 or metadata.st_gid != os.getegid() or permissions != 0o640:
                raise ValueError("production GitHub registry auth ownership is invalid")
        elif permissions not in {0o600, 0o640}:
            raise ValueError("GitHub registry auth file permissions are invalid")
        return resolved

    def _publish_all(self, plan: PublicationPlan) -> list[dict[str, Any]]:
        self.broker.preflight(tuple(item.repository for item in plan.publications))
        return [self._publish(item) for item in plan.publications]

    def _publish(self, plan: RepositoryPublication) -> dict[str, Any]:
        worktree = self._bounded_worktree(plan)
        git_arguments = ["-c", f"safe.directory={worktree}"]
        branch = self.runner.run(
            "git", [*git_arguments, "branch", "--show-current"], cwd=worktree
        ).stdout.strip()
        head = self.runner.run(
            "git", [*git_arguments, "rev-parse", "HEAD"], cwd=worktree
        ).stdout.strip()
        status = self.runner.run(
            "git", [*git_arguments, "status", "--porcelain=v1"], cwd=worktree
        ).stdout
        if branch != plan.branch or head != plan.head_sha or status:
            raise ValueError("publication worktree identity, head, or cleanliness changed")
        environment = {
            "HOME": str(self.broker.authenticated_home),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(worktree),
        }
        self.runner.run(
            "git",
            [
                *git_arguments,
                "push",
                "--porcelain",
                f"https://github.com/{plan.repository}.git",
                f"HEAD:refs/heads/{plan.branch}",
            ],
            cwd=worktree,
            timeout_seconds=900,
            environment=environment,
        )
        existing = self._existing_pull_request(plan)
        if existing is None:
            body_path = self._body_file(plan)
            try:
                created = self.broker.create_pull_request(
                    repository=plan.repository,
                    head=plan.branch,
                    base=plan.base_branch,
                    title=plan.title,
                    body_file=body_path,
                    change_binding=f"changeset:{plan.branch.rsplit('/', 1)[-1]}",
                    draft=True,
                )
                if not re.fullmatch(
                    rf"https://github\.com/{re.escape(plan.repository)}/pull/[1-9][0-9]*",
                    created,
                ):
                    raise ValueError("GitHub PR creation returned an invalid URL")
            finally:
                body_path.unlink(missing_ok=True)
            existing = self._existing_pull_request(plan)
        if existing is None or existing.get("headRefOid") != plan.head_sha:
            raise ValueError("GitHub PR does not bind to the exact validated commit")
        return {
            "repository": plan.repository,
            "repository_slug": plan.repository_slug,
            "branch": plan.branch,
            "head_sha": plan.head_sha,
            "base_branch": plan.base_branch,
            "pr_number": existing["number"],
            "pr_url": existing["url"],
            "pr_state": existing["state"],
            "draft": existing["isDraft"],
        }

    def _existing_pull_request(
        self,
        plan: RepositoryPublication,
    ) -> dict[str, Any] | None:
        value = self.broker.list_pull_requests(plan.repository, head=plan.branch, state="all")
        matches = [
            item
            for item in value
            if isinstance(item, dict)
            and item.get("baseRefName") == plan.base_branch
            and item.get("headRefOid") == plan.head_sha
        ]
        if len(matches) > 1:
            raise ValueError("multiple GitHub PRs bind to the same publication")
        return matches[0] if matches else None

    def _bounded_worktree(self, plan: RepositoryPublication) -> Path:
        root = self.settings.worktree_root.resolve()
        resolved = plan.working_directory.resolve()
        expected = root / plan.repository_slug / str(plan.branch.rsplit("/", 1)[-1])
        if not resolved.is_dir() or resolved != expected:
            raise ValueError("publication escaped its exact ChangeSet worktree")
        return resolved

    def _dedupe_enhancement(self, payload: dict[str, Any]) -> dict[str, Any]:
        repository = self._enhancement_repository(payload)
        fingerprint = str(payload.get("enhancement_fingerprint", ""))
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", fingerprint):
            raise ApplicationError(
                "enhancement deduplication requires its deterministic fingerprint",
                type="PolicyViolation",
                non_retryable=True,
            )
        try:
            matches = self.broker.search_issues(repository, f'"{fingerprint}"')
        except GhCliAuthError:
            return {**payload, "status": "BLOCKED", "blocker": "BLOCKED_BY_GH_AUTH"}
        except (CommandFailure, GhCliPolicyError):
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "GITHUB_DUPLICATE_SEARCH_FAILED",
            }
        if not isinstance(matches, list):
            raise ApplicationError(
                "GitHub enhancement search returned invalid data",
                type="PolicyViolation",
                non_retryable=True,
            )
        duplicate = matches[0].get("url") if matches and isinstance(matches[0], dict) else None
        return {
            **payload,
            "status": "DUPLICATE" if duplicate else "CLEAR",
            "duplicate_issue_url": duplicate,
            "searched_repository": repository,
        }

    def _dedupe_finding(self, payload: dict[str, Any]) -> dict[str, Any]:
        controls = payload.get("controls")
        finding = payload.get("finding")
        if not isinstance(controls, dict) or not isinstance(finding, dict):
            raise ApplicationError(
                "finding deduplication requires finding and controls",
                type="PolicyViolation",
                non_retryable=True,
            )
        if controls.get("disposition") != "CONFIRMED":
            return {**payload, "status": "SKIPPED", "duplicate_issue_url": None}
        project = str(finding.get("project", ""))
        pairs = {
            "falcone": ("gntik-ai/falcone", "gntik-ai/falcone-charts"),
            "musematic": ("gntik-ai/musematic", "gntik-ai/musematic-deploy"),
            "llmwiki": ("gntik-ai/llmwiki", "gntik-ai/llmwiki"),
        }
        if project not in pairs:
            raise ApplicationError(
                "finding project is not allowlisted",
                type="PolicyViolation",
                non_retryable=True,
            )
        source_repository, deployment_repository = pairs[project]
        surface = str(finding.get("repository_surface", "source"))
        repositories = {
            "source": (source_repository,),
            "deployment": (deployment_repository,),
            "pair": tuple(dict.fromkeys((source_repository, deployment_repository))),
            "runtime": tuple(dict.fromkeys((source_repository, deployment_repository))),
        }.get(surface)
        if repositories is None:
            raise ApplicationError(
                "finding repository surface is invalid",
                type="PolicyViolation",
                non_retryable=True,
            )
        searched: list[str] = []
        duplicate: str | None = None
        try:
            self.broker.preflight(repositories)
            for repository in repositories:
                matches = self.broker.search_issues(
                    repository, f'"{finding.get("fingerprint", "")}"'
                )
                searched.append(repository)
                if matches and isinstance(matches[0], dict):
                    candidate = matches[0].get("url")
                    if isinstance(candidate, str) and candidate:
                        duplicate = candidate
                        break
        except GhCliAuthError:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "BLOCKED_BY_GH_AUTH",
                "duplicate_issue_url": None,
                "searched_repositories": searched,
            }
        except (CommandFailure, GhCliPolicyError):
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "GITHUB_DUPLICATE_SEARCH_FAILED",
                "duplicate_issue_url": None,
                "searched_repositories": searched,
            }
        return {
            **payload,
            "status": "DUPLICATE" if duplicate else "CLEAR",
            "duplicate_issue_url": duplicate,
            "searched_repositories": searched,
        }

    def _publish_enhancement(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("approval_validated") is not True or not payload.get("approval_binding"):
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "ENHANCEMENT_APPROVAL_REQUIRED",
            }
        repository = self._enhancement_repository(payload)
        title_value = str(payload.get("proposed_outcome", "")).strip().splitlines()[0]
        title = f"Enhancement: {title_value}"[:200]
        if len(title_value) < 1:
            raise ApplicationError(
                "enhancement publication requires a proposed outcome",
                type="PolicyViolation",
                non_retryable=True,
            )
        body = self._enhancement_body(payload, repository)
        root = (self.settings.run_root / "delivery").resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, raw_path = tempfile.mkstemp(prefix="enhancement-", suffix=".md", dir=root)
        body_path = Path(raw_path)
        os.chmod(body_path, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(str(self.redactor.redact_text(body).value))
            created = self.broker.create_issue(
                repository=repository,
                title=title,
                body_file=body_path,
                change_binding=f"enhancement:{payload.get('enhancement_id')}",
            )
        except GhCliAuthError:
            return {**payload, "status": "BLOCKED", "blocker": "BLOCKED_BY_GH_AUTH"}
        except (CommandFailure, GhCliPolicyError):
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "GITHUB_ISSUE_PUBLICATION_FAILED",
            }
        finally:
            body_path.unlink(missing_ok=True)
        if not re.fullmatch(
            rf"https://github\.com/{re.escape(repository)}/issues/[1-9][0-9]*", created
        ):
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "GITHUB_ISSUE_PUBLICATION_RESULT_INVALID",
            }
        return {**payload, "status": "PUBLISHED", "issue_url": created}

    def _publish_finding(self, payload: dict[str, Any]) -> dict[str, Any]:
        publication = payload.get("finding_publication")
        if payload.get("status") != "PUBLICATION_READY" or not isinstance(publication, dict):
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "FINDING_PUBLICATION_NOT_PREPARED",
            }
        repository = str(publication.get("repository", ""))
        if repository not in {
            "gntik-ai/falcone",
            "gntik-ai/falcone-charts",
            "gntik-ai/musematic",
            "gntik-ai/musematic-deploy",
            "gntik-ai/llmwiki",
        }:
            raise ApplicationError(
                "finding publication repository is not allowlisted",
                type="PolicyViolation",
                non_retryable=True,
            )
        title = str(publication.get("title", ""))
        body = str(publication.get("body", ""))
        fingerprint = str(publication.get("fingerprint", ""))
        if not title or len(title) > 200 or fingerprint not in body:
            raise ApplicationError(
                "finding publication body is not bound to its fingerprint",
                type="PolicyViolation",
                non_retryable=True,
            )
        root = (self.settings.run_root / "delivery").resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, raw_path = tempfile.mkstemp(prefix="finding-", suffix=".md", dir=root)
        body_path = Path(raw_path)
        os.chmod(body_path, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(str(self.redactor.redact_text(body).value))
            created = self.broker.create_issue(
                repository=repository,
                title=title,
                body_file=body_path,
                change_binding=f"finding:{payload.get('finding_id')}",
            )
        except GhCliAuthError:
            return {**payload, "status": "BLOCKED", "blocker": "BLOCKED_BY_GH_AUTH"}
        except (CommandFailure, GhCliPolicyError):
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "GITHUB_ISSUE_PUBLICATION_FAILED",
            }
        finally:
            body_path.unlink(missing_ok=True)
        if not re.fullmatch(
            rf"https://github\.com/{re.escape(repository)}/issues/[1-9][0-9]*", created
        ):
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "GITHUB_ISSUE_PUBLICATION_RESULT_INVALID",
            }
        return {**payload, "status": "PUBLISHED", "issue_url": created}

    @staticmethod
    def _enhancement_repository(payload: dict[str, Any]) -> str:
        enhancement = payload.get("enhancement")
        repository = (
            enhancement.get("affected_repository")
            if isinstance(enhancement, dict)
            else payload.get("affected_repository")
        )
        allowed = {
            "gntik-ai/falcone",
            "gntik-ai/falcone-charts",
            "gntik-ai/musematic",
            "gntik-ai/musematic-deploy",
            "gntik-ai/llmwiki",
        }
        if repository not in allowed:
            raise ApplicationError(
                "enhancement repository is not allowlisted",
                type="PolicyViolation",
                non_retryable=True,
            )
        return str(repository)

    @staticmethod
    def _enhancement_body(payload: dict[str, Any], repository: str) -> str:
        evidence = "\n".join(f"- `{item}`" for item in payload.get("evidence_handles", []))
        dependencies = "\n".join(f"- {item}" for item in payload.get("dependencies", []))
        criteria = "\n".join(f"- [ ] {item}" for item in payload.get("acceptance_criteria", []))
        return (
            "## Engineering Control enhancement\n\n"
            f"- Enhancement: `{payload.get('enhancement_id')}`\n"
            f"- Project: `{payload.get('project')}`\n"
            f"- Persona: `{payload.get('persona')}`\n"
            f"- Affected repository: `{repository}`\n"
            f"- Risk: `{payload.get('risk')}`\n"
            f"- Fingerprint: `{payload.get('enhancement_fingerprint')}`\n"
            f"- Approval binding: `{payload.get('approval_binding')}`\n\n"
            f"## Observed problem\n\n{payload.get('observed_problem')}\n\n"
            f"## Current behavior\n\n{payload.get('current_behavior')}\n\n"
            f"## Proposed outcome\n\n{payload.get('proposed_outcome')}\n\n"
            f"## User/business value\n\n{payload.get('user_value')}\n\n"
            f"## Success metric\n\n{payload.get('success_metric')}\n\n"
            f"## Evidence\n\n{evidence}\n\n"
            f"## Dependencies\n\n{dependencies or '- None identified'}\n\n"
            "## Security and data implications\n\n"
            f"{payload.get('security_data_implications')}\n\n"
            f"## Acceptance criteria\n\n{criteria}\n\n"
            f"## Rollout strategy\n\n{payload.get('rollout_strategy')}\n\n"
            f"## Kill switch\n\n{payload.get('kill_switch') or 'Not applicable'}\n"
        )

    @staticmethod
    def _write_repositories(step: str, payload: dict[str, Any]) -> tuple[str, ...]:
        if step == "delivery.publish":
            plan = payload.get("publication_plan")
            publications = plan.get("publications") if isinstance(plan, dict) else None
            if not isinstance(publications, list) or not 1 <= len(publications) <= 2:
                raise ValueError("delivery publication targets are missing or unbounded")
            return tuple(
                str(item.get("repository", "")) if isinstance(item, dict) else ""
                for item in publications
            )
        if step == "reconcile.publish_promotion":
            promotion = payload.get("promotion_publication")
            if not isinstance(promotion, dict):
                raise ValueError("promotion publication target is missing")
            return (str(promotion.get("repository", "")),)
        if step in {"final.merge_source", "final.merge_deployment"}:
            plan = payload.get("final_closure_plan")
            key = "source" if step == "final.merge_source" else "deployment"
            binding = plan.get(key) if isinstance(plan, dict) else None
            if not isinstance(binding, dict):
                raise ValueError("final merge target is missing")
            return (str(binding.get("repository", "")),)
        if step in {"final.comment_issue", "final.close_issue"}:
            plan = payload.get("final_issue_plan")
            closure = plan.get("closure") if isinstance(plan, dict) else None
            if not isinstance(closure, dict):
                raise ValueError("final issue target is missing")
            return (str(closure.get("issue_repository", "")),)
        if step == "enhancement.publish":
            enhancement = payload.get("enhancement")
            repository = (
                enhancement.get("affected_repository")
                if isinstance(enhancement, dict)
                else payload.get("affected_repository")
            )
            return (str(repository or ""),)
        if step == "finding.publish":
            publication = payload.get("finding_publication")
            if not isinstance(publication, dict):
                raise ValueError("finding publication target is missing")
            return (str(publication.get("repository", "")),)
        raise ValueError("unknown GitHub write activity")

    def _body_file(self, plan: RepositoryPublication) -> Path:
        root = (self.settings.run_root / "delivery").resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        safe = str(self.redactor.redact_text(plan.body).value)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f"{plan.repository_slug}-{plan.head_sha}-",
            suffix=".md",
            dir=root,
        )
        path = Path(raw_path)
        os.chmod(path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(safe)
        return path
