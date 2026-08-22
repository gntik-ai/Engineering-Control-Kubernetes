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
from engineering_control.config import Settings
from engineering_control.evidence.transfer import EvidencePublisher
from engineering_control.models.enums import RedactionStatus
from engineering_control.models.reconciliation import (
    CandidateArtifactManifest,
    CIReconciliationPlan,
    PromotionPublication,
)
from engineering_control.security.redaction import SecretRedactor


class RepositoryPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(
        pattern=r"^gntik-ai/(falcone|falcone-charts|musematic|musematic-deploy)$"
    )
    repository_slug: str = Field(pattern=r"^(falcone|falcone-charts|musematic|musematic-deploy)$")
    working_directory: Path
    branch: str = Field(pattern=r"^agent/(falcone|musematic)/[A-Za-z0-9._-]+/[a-f0-9-]{36}$")
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
        if slugs not in full_pairs and not source_only:
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

    def __init__(self, settings: Settings, runner: SafeCommandRunner | None = None) -> None:
        self.settings = settings
        self.runner = runner or SafeCommandRunner(
            {"git": settings.git_executable, "gh": settings.github_executable}
        )
        self.redactor = SecretRedactor()

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        if step == "reconcile.inspect_ci":
            return self._inspect_ci(payload)
        if step == "reconcile.publish_promotion":
            return self._publish_promotion(payload)
        if step != "delivery.publish":
            raise ApplicationError("unsupported delivery activity", non_retryable=True)
        try:
            plan = PublicationPlan.model_validate(payload.get("publication_plan"))
            token = self._token()
            if token is None:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "MANUAL_GITHUB_DELIVERY_CREDENTIAL_REQUIRED",
                }
            askpass = self._askpass()
            results = [self._publish(item, token, askpass) for item in plan.publications]
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
            token = self._token()
            if token is None:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "MANUAL_GITHUB_DELIVERY_CREDENTIAL_REQUIRED",
                }
            root = (self.settings.run_root / "delivery").resolve()
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            environment = {"GH_TOKEN": token}
            pull_request = self._pull_request(plan, root, environment)
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
            manifest, candidate_evidence = self._candidate_manifest(
                plan, root, environment, registry_auth
            )
            combined = {**ci_evidence, "candidate": candidate_evidence}
            return self._with_ci_evidence(
                payload,
                plan,
                combined,
                status="CANDIDATE_VERIFIED",
                candidate_status="VERIFIED",
                candidate_manifest=manifest.model_dump(mode="json"),
            )
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
        cwd: Path,
        environment: dict[str, str],
    ) -> dict[str, Any]:
        result = self.runner.run(
            "gh",
            [
                "pr",
                "view",
                str(plan.source_pr_number),
                "--repo",
                plan.repository,
                "--json",
                (
                    "number,url,state,isDraft,headRefName,headRefOid,baseRefName,"
                    "mergeStateStatus,reviewDecision,statusCheckRollup"
                ),
            ],
            cwd=cwd,
            environment=environment,
        )
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("GitHub PR inspection returned an invalid object")
        return value

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
        environment: dict[str, str],
        registry_auth: Path,
    ) -> tuple[CandidateArtifactManifest, dict[str, Any]]:
        policy = plan.policy.candidate_build
        run_id = plan.candidate_build_run_id
        if run_id is None:
            raise ValueError("candidate build run is missing")
        viewed = self.runner.run(
            "gh",
            [
                "run",
                "view",
                str(run_id),
                "--repo",
                plan.repository,
                "--json",
                "databaseId,workflowName,headSha,status,conclusion,event,url",
            ],
            cwd=cwd,
            environment=environment,
        )
        run = json.loads(viewed.stdout)
        if not isinstance(run, dict):
            raise ValueError("candidate build run returned an invalid object")
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
            self.runner.run(
                "gh",
                [
                    "run",
                    "download",
                    str(run_id),
                    "--repo",
                    plan.repository,
                    "--name",
                    policy.artifact_name,
                    "--dir",
                    str(download_root),
                ],
                cwd=cwd,
                timeout_seconds=900,
                environment=environment,
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
        attestation_environment = {
            **environment,
            "DOCKER_CONFIG": str(registry_auth.parent),
        }
        for image in manifest.images:
            arguments = [
                "attestation",
                "verify",
                f"oci://{image.repository}@{image.digest}",
                "--repo",
                plan.repository,
                "--signer-workflow",
                policy.signer_workflow,
                "--source-digest",
                plan.source_head_sha,
                "--format",
                "json",
            ]
            if policy.deny_self_hosted_runners:
                arguments.append("--deny-self-hosted-runners")
            verified = self.runner.run(
                "gh", arguments, cwd=cwd, timeout_seconds=900, environment=attestation_environment
            )
            result = json.loads(verified.stdout)
            if not isinstance(result, list) or not result:
                raise ValueError("candidate image has no verified provenance attestation")
            attestation_hashes[image.component] = hashlib.sha256(
                verified.stdout.encode()
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
            token = self._token()
            if token is None:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "MANUAL_GITHUB_DELIVERY_CREDENTIAL_REQUIRED",
                }
            publication = RepositoryPublication.model_validate(promotion.model_dump())
            result = self._publish(publication, token, self._askpass())
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

    def _publish(self, plan: RepositoryPublication, token: str, askpass: Path) -> dict[str, Any]:
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
            "ENGINEERING_GITHUB_TOKEN": token,
            "GH_TOKEN": token,
            "GIT_ASKPASS": str(askpass),
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
        existing = self._existing_pull_request(plan, environment)
        if existing is None:
            body_path = self._body_file(plan)
            try:
                created = self.runner.run(
                    "gh",
                    [
                        "pr",
                        "create",
                        "--repo",
                        plan.repository,
                        "--head",
                        plan.branch,
                        "--base",
                        plan.base_branch,
                        "--title",
                        plan.title,
                        "--body-file",
                        str(body_path),
                        "--draft",
                    ],
                    cwd=worktree,
                    environment=environment,
                ).stdout.strip()
                if not re.fullmatch(
                    rf"https://github\.com/{re.escape(plan.repository)}/pull/[1-9][0-9]*",
                    created,
                ):
                    raise ValueError("GitHub PR creation returned an invalid URL")
            finally:
                body_path.unlink(missing_ok=True)
            existing = self._existing_pull_request(plan, environment)
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
        environment: dict[str, str],
    ) -> dict[str, Any] | None:
        result = self.runner.run(
            "gh",
            [
                "pr",
                "list",
                "--repo",
                plan.repository,
                "--head",
                plan.branch,
                "--state",
                "all",
                "--json",
                "number,url,state,isDraft,headRefOid,baseRefName",
                "--limit",
                "10",
            ],
            cwd=plan.working_directory,
            environment=environment,
        )
        value = json.loads(result.stdout)
        if not isinstance(value, list):
            raise ValueError("GitHub PR lookup returned a non-list response")
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

    def _token(self) -> str | None:
        path = self.settings.github_token_file
        if path is None or not path.exists():
            return None
        root = self.settings.delivery_credential_root.resolve()
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(root):
            raise ValueError("GitHub delivery token escaped its credential root")
        mode = resolved.stat().st_mode
        permissions = stat.S_IMODE(mode)
        if not stat.S_ISREG(mode) or mode & stat.S_IRWXO:
            raise ValueError("GitHub delivery token permissions are too broad")
        if self.settings.environment == "production":
            metadata = resolved.stat()
            if metadata.st_uid != 0 or metadata.st_gid != os.getegid() or permissions != 0o640:
                raise ValueError("production GitHub delivery token ownership is invalid")
        elif permissions not in {0o600, 0o640}:
            raise ValueError("GitHub delivery token permissions are invalid")
        token = resolved.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ValueError("GitHub delivery token is invalid")
        return token

    def _askpass(self) -> Path:
        configured = self.settings.git_askpass_executable
        if not configured.is_absolute():
            raise ValueError("Git askpass executable must use an absolute path")
        resolved = configured.resolve()
        if not resolved.is_file():
            raise ValueError("Git askpass executable is missing")
        mode = resolved.stat().st_mode
        if not stat.S_ISREG(mode) or mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("Git askpass executable permissions are unsafe")
        if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            raise ValueError("Git askpass helper is not executable")
        return resolved

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
