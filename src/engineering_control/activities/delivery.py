from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from temporalio import activity
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import CommandFailure, SafeCommandRunner
from engineering_control.adapters.github.client import (
    GhCliAuthError,
    GhCliBroker,
    GhCliPolicyError,
    same_repository_pull_request,
)
from engineering_control.adapters.publication_authority import (
    PublicationAuthorityClient,
    PublicationAuthorityError,
)
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
from engineering_control.projects import (
    PROJECT_ALTERNATION,
    PROJECT_REPOSITORIES,
    PROJECT_REPOSITORY_SLUGS,
    REPOSITORY_SET,
    REPOSITORY_SLUG_ALTERNATION,
    SELF_CONTAINED,
    SEPARATE_DEPLOYMENT_REPOSITORY_SLUGS,
    SPLIT_PROJECTS,
    alternation,
)
from engineering_control.security.redaction import SecretRedactor

_T = TypeVar("_T")
# NEW-147: GitHub updates a PR's head asynchronously after a push (falcone #972: #1068 still
# showed the previous head 3-4 s later). Binding waits this long, with backoff, for the
# bound PR to reach the pushed commit, and never creates a second PR meanwhile.
PR_BIND_CONVERGENCE_SECONDS = 120.0
_PR_BIND_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 15.0)
# NEW-147 review 2: publication listings are complete, not gh's default page of 10.
# ``gh pr list --head`` matches the branch name in forks too and filters by repository only
# afterwards, so a short page of fork PRs could hide this repository's own (merged) PR.
# gh pages up to this limit; a listing that reaches it may be truncated and is refused.
_PR_BIND_LIST_LIMIT = 1000
_PUBLISH_HEARTBEAT_SECONDS = 30.0


class PullRequestHeadNotConverged(ValueError):
    """NEW-147: the publication's PR did not show the pushed commit within the bound."""


PublicationContext = tuple[PublicationAuthorityClient, dict[str, Any], dict[str, Any]]


class RepositoryPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(pattern=rf"^gntik-ai/({REPOSITORY_SLUG_ALTERNATION})$")
    repository_slug: str = Field(pattern=rf"^({REPOSITORY_SLUG_ALTERNATION})$")
    working_directory: Path
    branch: str = Field(pattern=rf"^agent/({PROJECT_ALTERNATION})/[A-Za-z0-9._-]+/[a-f0-9-]{{36}}$")
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

    repository_slug: str = Field(
        pattern=rf"^({alternation(SEPARATE_DEPLOYMENT_REPOSITORY_SLUGS)})$"
    )
    branch: str = Field(
        pattern=rf"^agent/({alternation(SPLIT_PROJECTS)})/[A-Za-z0-9._-]+/[a-f0-9-]{{36}}$"
    )
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
        # NEW-157: the registry's split pairs, their sources alone, or a self-contained repo.
        split = {
            PROJECT_REPOSITORY_SLUGS[project][0]: PROJECT_REPOSITORY_SLUGS[project][1]
            for project in SPLIT_PROJECTS
        }
        full_pairs = tuple({source, deployment} for source, deployment in split.items())
        source_only = slugs in tuple({source} for source in split)
        self_contained = slugs in tuple(
            {PROJECT_REPOSITORY_SLUGS[project][0]} for project in SELF_CONTAINED
        )
        if slugs not in full_pairs and not source_only and not self_contained:
            raise ValueError("publication plan does not match a configured ChangeSet")
        if source_only:
            expected = split[next(iter(slugs))]
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
        publication_authority: PublicationAuthorityClient | None = None,
    ) -> None:
        self.settings = settings
        self.publication_authority = publication_authority
        self.runner = runner or SafeCommandRunner(
            {"git": settings.git_executable, "gh": settings.github_executable}
        )
        self.redactor = SecretRedactor()
        # NEW-147: injectable for the PR-binding convergence wait (tests use a fake clock).
        self._sleep: Callable[[float], None] = time.sleep
        self._monotonic: Callable[[], float] = time.monotonic
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
            # NEW-147 review 3: promotion binding waits for the PR head too; heartbeat it.
            return await self._with_heartbeat(asyncio.to_thread(self._publish_promotion, payload))
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
            if not isinstance(payload.get("publication_intent"), dict):
                raise PublicationAuthorityError("publication requires its durable intent")
            authority = self.publication_authority or PublicationAuthorityClient(
                self.settings, self.broker
            )
            results = await self._with_heartbeat(
                asyncio.to_thread(
                    self._publish_all, plan, (authority, payload, plan.model_dump(mode="json"))
                )
            )
        except PullRequestHeadNotConverged:
            # NEW-147: retryable; nothing was created while waiting for GitHub.
            return {**payload, "status": "BLOCKED", "blocker": "PR_HEAD_NOT_CONVERGED"}
        except PublicationAuthorityError as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "PUBLICATION_AUTHORITY_DENIED_OR_UNVERIFIED",
                "publication_effect_unknown": error.unknown,
                # Refers to the current effect, not earlier completed effects
                # within a paired publication. Unknown may still describe lost
                # receipt custody even when this callback never began.
                "publication_effect_not_attempted": error.not_attempted,
            }
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

    @staticmethod
    async def _with_heartbeat(work: Awaitable[_T]) -> _T:
        """NEW-147: keep a publication (pushes plus the PR-binding wait) alive against the
        workflow's 2-minute heartbeat timeout; a timed-out publish leaves UNKNOWN effects."""
        task = asyncio.ensure_future(work)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=_PUBLISH_HEARTBEAT_SECONDS)
                if done:
                    return task.result()
                try:
                    activity.heartbeat({"phase": "publication"})
                except RuntimeError:
                    pass  # not running inside an activity (unit tests)
        except asyncio.CancelledError:
            task.cancel()
            raise

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
            # NEW-61: the ChangeSet id selects the worker metadata repository; it is not a
            # RepositoryPublication field (extra="forbid" rejected the full dump).
            publication = RepositoryPublication.model_validate(
                promotion.model_dump(exclude={"change_set_id"})
            )
            result = self._publish(publication, promotion.change_set_id)
        except PullRequestHeadNotConverged:
            # NEW-147 review 3: retryable; nothing was created while waiting for GitHub.
            return {**payload, "status": "BLOCKED", "blocker": "PR_HEAD_NOT_CONVERGED"}
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

    def _publish_all(
        self, plan: PublicationPlan, publication_context: PublicationContext | None = None
    ) -> list[dict[str, Any]]:
        self.broker.preflight(tuple(item.repository for item in plan.publications))
        return [
            self._publish(item, plan.change_set_id, publication_context=publication_context)
            for item in plan.publications
        ]

    def _publication_effect(
        self,
        plan: RepositoryPublication,
        publication_context: PublicationContext | None,
        action: str,
        operation: Callable[[], _T],
    ) -> _T | None:
        """Run one GitHub write effect, re-checking the write policy immediately before it.

        With a publication context the effect runs under durable publication-authority
        custody; without one (promotion's distinct approval path) it runs directly.
        """

        def guarded() -> _T:
            denial = self.write_policy.evaluate(lambda: (plan.repository,))
            if denial is not None:
                raise PublicationAuthorityError(f"publication write policy changed before {action}")
            return operation()

        if publication_context is None:
            return guarded()
        authority, payload, full_plan = publication_context
        return authority.execute(
            payload=payload,
            plan=full_plan,
            repository=plan.repository,
            action=action,
            operation=guarded,
        )

    def _publish(
        self,
        plan: RepositoryPublication,
        change_set_id: UUID,
        *,
        publication_context: PublicationContext | None = None,
    ) -> dict[str, Any]:
        if plan.branch.rsplit("/", 1)[-1] != str(change_set_id):
            raise ValueError("publication branch does not bind its ChangeSet")
        # NEW-103: a CI repair of the deployment PR may re-publish a ChangeSet whose source
        # PR is already merged; that branch is delivered and must never move.
        listed_merged = self.broker.list_pull_requests(
            plan.repository, head=plan.branch, state="merged", limit=_PR_BIND_LIST_LIMIT
        )
        if len(listed_merged) >= _PR_BIND_LIST_LIMIT:
            # A truncated listing could hide this repository's merged PR: never push then.
            raise ValueError("too many merged GitHub PRs share this publication head to verify")
        merged = [
            item
            for item in listed_merged
            if item.get("baseRefName") == plan.base_branch
            # NEW-147 review: a fork's PR from a same-named branch is not this branch's.
            and self._own_pull_request(item, plan)
        ]
        if any(item.get("headRefOid") != plan.head_sha for item in merged):
            raise ValueError("a merged pull request's branch cannot be republished")
        if merged:
            # already merged at exactly this head
            return self._bind_pull_request(plan, publication_context)
        if self.settings.publication_fetch_from_worker:
            # NEW-61: the worktree lives on the worker host; bring only its commits here.
            self._push_from_worker(plan, change_set_id, publication_context)
        else:
            self._push_from_local_worktree(plan, publication_context)
        return self._bind_pull_request(plan, publication_context)

    def _push_from_worker(
        self,
        plan: RepositoryPublication,
        change_set_id: UUID,
        publication_context: PublicationContext | None = None,
    ) -> None:
        """NEW-61: fetch the exact validated branch from the worker, then push it.

        The worker keeps each ChangeSet's commits in a bare Git metadata repository whose
        path is derived only from settings, the repository slug and the ChangeSet id (as
        GitWorktreeManager.metadata_path does), never from the payload. The branch is
        fetched over SSH into a controller-local scratch repository and pushed only when it
        resolves to exactly the checker-approved head.
        """
        self._expected_worktree(plan)  # the payload must still name the exact worktree
        metadata = self._worker_metadata_path(plan.repository_slug, change_set_id)
        scratch = self._publication_scratch(plan, change_set_id)
        try:
            repository = scratch / "repository.git"
            self.runner.run("git", ["init", "--quiet", "--bare", str(repository)], cwd=scratch)
            environment = self._scratch_environment(plan.repository_slug)
            ref = f"refs/heads/{plan.branch}"
            self.runner.run(
                "git",
                [
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    # The metadata repository is owned by the worker's scan identity.
                    f"--upload-pack=git -c safe.directory={metadata} upload-pack",
                    f"ssh://{self.settings.publication_transport_host}{metadata}",
                    f"+{ref}:{ref}",
                ],
                cwd=repository,
                timeout_seconds=900,
                environment={
                    **environment,
                    "GIT_SSH_COMMAND": (
                        "ssh -o BatchMode=yes -o ConnectTimeout=20 -o UpdateHostKeys=no"
                    ),
                },
            )
            fetched = self.runner.run(
                "git",
                ["rev-parse", "--verify", f"{ref}^{{commit}}"],
                cwd=repository,
                environment=environment,
            ).stdout.strip()
            if fetched != plan.head_sha:
                # NEW-61: fail closed; never publish anything but the validated commit.
                raise ValueError("fetched ChangeSet branch does not match the validated commit")
            self._publication_effect(
                plan,
                publication_context,
                "git_push",
                lambda: self.runner.run(
                    "git",
                    [
                        "push",
                        "--porcelain",
                        f"https://github.com/{plan.repository}.git",
                        f"{plan.head_sha}:{ref}",
                    ],
                    cwd=repository,
                    timeout_seconds=900,
                    environment=environment,
                ),
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _worker_metadata_path(self, repository_slug: str, change_set_id: UUID) -> Path:
        """NEW-61: mirror GitWorktreeManager.metadata_path on the worker host."""
        metadata = self.settings.worktree_metadata_root / repository_slug / f"{change_set_id}.git"
        # The path is embedded in the remote upload-pack command line: keep it inert.
        if (
            not metadata.is_absolute()
            or ".." in metadata.parts
            or not re.fullmatch(r"/[A-Za-z0-9._/-]+", str(metadata))
        ):
            raise ValueError("worker ChangeSet Git metadata path is not bounded")
        return metadata

    def _publication_scratch(self, plan: RepositoryPublication, change_set_id: UUID) -> Path:
        root = (self.settings.run_root / "delivery" / "publications").resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        scratch = Path(
            tempfile.mkdtemp(prefix=f"{plan.repository_slug}-{change_set_id}-", dir=root)
        ).resolve()
        if scratch.parent != root:
            raise ValueError("publication scratch repository escaped its root")
        os.chmod(scratch, 0o700)
        return scratch

    def _scratch_environment(self, repository_slug: str) -> dict[str, str]:
        environment = {
            "HOME": str(self.broker.authenticated_home),
            "GIT_TERMINAL_PROMPT": "0",
        }
        # NEW-61: borrow objects from the controller's canonical mirror so only the
        # ChangeSet's new commits travel over SSH (and the base need not be re-sent).
        mirror_root = self.settings.mirror_root.resolve()
        mirror = (self.settings.mirror_root / f"{repository_slug}.git").resolve()
        if mirror.parent == mirror_root and (mirror / "objects").is_dir():
            environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(mirror / "objects")
        return environment

    def _push_from_local_worktree(
        self,
        plan: RepositoryPublication,
        publication_context: PublicationContext | None = None,
    ) -> None:
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
        self._publication_effect(
            plan,
            publication_context,
            "git_push",
            lambda: self.runner.run(
                "git",
                [
                    *git_arguments,
                    "push",
                    "--porcelain",
                    f"https://github.com/{plan.repository}.git",
                    f"{plan.head_sha}:refs/heads/{plan.branch}",
                ],
                cwd=worktree,
                timeout_seconds=900,
                environment=environment,
            ),
        )

    def _bind_pull_request(
        self,
        plan: RepositoryPublication,
        publication_context: PublicationContext | None = None,
    ) -> dict[str, Any]:
        """Bind the publication to its one PR for this head branch and base.

        NEW-147: an existing PR is found by head branch and base whatever its head (GitHub
        updates a PR's head asynchronously after a push), then awaited until it shows the
        pushed commit. A PR is created only when none can bind (none at all, or only closed
        ones), and the created PR is awaited the same way. Reading never makes an effect."""
        existing = self._bindable_pull_request(plan)
        number: int | None = None
        if existing is None:
            body_path = self._body_file(plan)
            try:
                created = self._publication_effect(
                    plan,
                    publication_context,
                    "pr_create",
                    lambda: self.broker.create_pull_request(
                        repository=plan.repository,
                        head=plan.branch,
                        base=plan.base_branch,
                        title=plan.title,
                        body_file=body_path,
                        change_binding=f"changeset:{plan.branch.rsplit('/', 1)[-1]}",
                        draft=True,
                    ),
                )
                if created is not None:
                    match = re.fullmatch(
                        rf"https://github\.com/{re.escape(plan.repository)}/pull/([1-9][0-9]*)",
                        created,
                    )
                    if match is None:
                        raise ValueError("GitHub PR creation returned an invalid URL")
                    number = int(match.group(1))
            finally:
                body_path.unlink(missing_ok=True)
        else:
            number = int(existing["number"])
        bound = self._await_bound_pull_request(plan, number)
        return {
            "repository": plan.repository,
            "repository_slug": plan.repository_slug,
            "branch": plan.branch,
            "head_sha": plan.head_sha,
            "base_branch": plan.base_branch,
            "pr_number": bound["number"],
            "pr_url": bound["url"],
            "pr_state": bound["state"],
            "draft": bound["isDraft"],
        }

    @staticmethod
    def _own_pull_request(item: Any, plan: RepositoryPublication) -> bool:
        """NEW-147 review: ``gh pr list --head`` matches the branch name in forks too. Only
        a PR whose head repository is exactly the publication's own (not cross-repository)
        is this branch's; a PR whose head repository is unknown is refused."""
        if not isinstance(item, dict):
            raise ValueError("a GitHub PR for this publication is malformed")
        try:
            return same_repository_pull_request(item, plan.repository)
        except GhCliPolicyError as error:
            raise ValueError(
                "a GitHub PR for this publication has no known head repository"
            ) from error

    def _publication_pull_requests(self, plan: RepositoryPublication) -> list[dict[str, Any]]:
        """Every PR (any state) from this repository's head branch into this base; never a
        cut list. Fork PRs from a same-named branch are foreign: never bound, and they do not
        block creating this repository's own PR (GitHub allows both, and NEW-145b's complete
        listing of ``owner:branch`` never includes them either)."""
        value = self.broker.list_pull_requests(
            plan.repository, head=plan.branch, state="all", limit=_PR_BIND_LIST_LIMIT
        )
        if len(value) >= _PR_BIND_LIST_LIMIT:
            raise ValueError("too many GitHub PRs share this publication head to bind safely")
        return [
            item
            for item in value
            if self._own_pull_request(item, plan) and item.get("baseRefName") == plan.base_branch
        ]

    def _bindable_pull_request(self, plan: RepositoryPublication) -> dict[str, Any] | None:
        """NEW-147: the one PR this publication binds to, or None when a PR may be created.

        Exactly one OPEN PR binds, whatever its head. A MERGED PR binds only alone and at
        exactly the validated head (a merged branch never moves). A CLOSED PR is never bound
        or reopened, and none may silently stand beside a new one: with no open PR, a closed
        one refuses (an operator decision). Any other combination is ambiguous and refused."""
        pulls = self._publication_pull_requests(plan)
        open_pulls = [item for item in pulls if item.get("state") == "OPEN"]
        merged = [item for item in pulls if item.get("state") == "MERGED"]
        closed = [item for item in pulls if item.get("state") == "CLOSED"]
        if len(open_pulls) + len(merged) + len(closed) != len(pulls):
            raise ValueError("a GitHub PR for this publication has an unknown state")
        if len(open_pulls) > 1 or (open_pulls and merged):
            raise ValueError("multiple GitHub PRs bind to the same publication")
        if open_pulls:
            return open_pulls[0]
        if merged:
            if len(merged) != 1 or merged[0].get("headRefOid") != plan.head_sha:
                raise ValueError("a merged GitHub PR for this publication is at another commit")
            return merged[0]
        if closed:
            raise ValueError(
                "a closed GitHub PR exists for this publication head; reopen or remove it "
                "deliberately before publishing again"
            )
        return None

    def _await_bound_pull_request(
        self, plan: RepositoryPublication, number: int | None
    ) -> dict[str, Any]:
        """NEW-147: wait (bounded, with backoff) until the bound PR shows the pushed commit.

        ``number`` is the PR found or created (None when a completed creation's response was
        not available); the PR that binds must stay that one."""
        deadline = self._monotonic() + PR_BIND_CONVERGENCE_SECONDS
        attempt = 0
        while True:
            current = self._bindable_pull_request(plan)
            if current is not None and number is not None and int(current["number"]) != number:
                raise ValueError("the publication's GitHub PR changed while binding")
            if current is not None and current.get("headRefOid") == plan.head_sha:
                return current
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise PullRequestHeadNotConverged(
                    "the publication's GitHub PR did not reach the pushed commit in time"
                    if current is not None
                    else "the created GitHub PR did not appear in time"
                )
            delay = _PR_BIND_BACKOFF_SECONDS[min(attempt, len(_PR_BIND_BACKOFF_SECONDS) - 1)]
            self._sleep(min(delay, remaining))
            attempt += 1

    def _bounded_worktree(self, plan: RepositoryPublication) -> Path:
        resolved = self._expected_worktree(plan)
        if not resolved.is_dir():
            raise ValueError("publication escaped its exact ChangeSet worktree")
        return resolved

    def _expected_worktree(self, plan: RepositoryPublication) -> Path:
        # NEW-61: on the controller the worktree is remote, so this checks the path only.
        root = self.settings.worktree_root.resolve()
        resolved = plan.working_directory.resolve()
        expected = root / plan.repository_slug / str(plan.branch.rsplit("/", 1)[-1])
        if resolved != expected:
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
        pairs = dict(PROJECT_REPOSITORIES)
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
        if repository not in REPOSITORY_SET:
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
        allowed = REPOSITORY_SET
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
