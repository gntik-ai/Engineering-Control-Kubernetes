from __future__ import annotations

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
from engineering_control.evidence.transfer import EvidencePublisher
from engineering_control.models.enums import RedactionStatus
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
    """Publish exact validated commits and draft PRs; never merge or rewrite history."""

    supported_steps = frozenset(
        {
            "finding.dedupe",
            "enhancement.github_dedupe",
            "enhancement.publish",
            "finding.publish",
            "reconcile.inspect_ci",
            "reconcile.publish_promotion",
            "delivery.publish",
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
            return self._dedupe_finding(payload)
        if step == "enhancement.github_dedupe":
            return self._dedupe_enhancement(payload)
        if step == "enhancement.publish":
            return self._publish_enhancement(payload)
        if step == "finding.publish":
            return self._publish_finding(payload)
        if step == "reconcile.inspect_ci":
            return self._inspect_ci(payload)
        if step == "reconcile.publish_promotion":
            return self._publish_promotion(payload)
        if step != "delivery.publish":
            raise ApplicationError("unsupported delivery activity", non_retryable=True)
        try:
            plan = PublicationPlan.model_validate(payload.get("publication_plan"))
            self.broker.preflight(tuple(item.repository for item in plan.publications))
            results = [self._publish(item) for item in plan.publications]
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
