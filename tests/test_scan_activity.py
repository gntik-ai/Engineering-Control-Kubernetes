from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ApplicationError

import engineering_control.activities.scan as scan_module
from engineering_control.activities.retention import cleanup_authorization_sha256
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.config import Settings
from engineering_control.worktrees.manager import WorktreeAllocation


class FakeWorktreeManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.snapshots: list[str] = []
        self.release_reads: list[tuple[str, Path]] = []

    def snapshot(self, repository: str, *, base_ref: str, expected_sha: str) -> dict[str, str]:
        self.snapshots.append(repository)
        return {
            "repository": repository,
            "mirror": str(self.root / f"{repository}.git"),
            "base_ref": base_ref,
            "sha": expected_sha,
        }

    def allocate(self, **values: Any) -> WorktreeAllocation:
        target = self.root / values["repository"] / str(values["change_set_id"])
        target.mkdir(parents=True, exist_ok=True)
        return WorktreeAllocation(
            repository=values["repository"],
            path=target,
            branch=f"agent/{values['project']}/{values['issue']}/{values['change_set_id']}",
            base_sha=values["expected_base_sha"],
            change_set_id=values["change_set_id"],
        )

    def allocate_audit_snapshot(self, **values: Any) -> WorktreeAllocation:
        target = self.root / values["repository"] / str(values["campaign_id"])
        target.mkdir(parents=True, exist_ok=True)
        return WorktreeAllocation(
            repository=values["repository"],
            path=target,
            branch="",
            base_sha=values["expected_base_sha"],
            change_set_id=values["campaign_id"],
        )

    def inspect_snapshot(self, path: Path, *, expected_sha: str) -> dict[str, str]:
        return {"path": str(path), "sha": expected_sha, "status": "clean"}

    def grant_release_read(self, repository: str, target: Path) -> None:
        self.release_reads.append((repository, target))

    def grant_delivery_read(self, repository: str, target: Path) -> None:
        return None


class FakeObserverRunner:
    def run(self, executable: str, arguments: Any, **kwargs: Any) -> CommandResult:
        assert executable == "kubectl"
        if "auth" in arguments:
            if "can-i" in arguments:
                index = arguments.index("can-i")
                verb, resource = arguments[index + 1 : index + 3]
                allowed = verb in {"get", "list", "watch"} and resource not in {
                    "secrets",
                    "pods/exec",
                    "pods/attach",
                    "pods/portforward",
                }
                return CommandResult(0, "yes\n" if allowed else "no\n", "")
            return CommandResult(0, "no\n", "")
        resource = arguments[arguments.index("get") + 1]
        revision = "d" * 40
        digest = f"sha256:{'e' * 64}"
        if resource == "deployments":
            value = {
                "items": [
                    {
                        "metadata": {
                            "name": "api",
                            "annotations": {"engineering.gntik.ai/deployment-sha": revision},
                        },
                        "spec": {
                            "replicas": 1,
                            "template": {
                                "spec": {
                                    "containers": [
                                        {"name": "api", "image": f"example.invalid/api@{digest}"}
                                    ]
                                }
                            },
                        },
                        "status": {"availableReplicas": 1},
                    }
                ]
            }
        else:
            value = {
                "items": [
                    {
                        "metadata": {
                            "name": "api-pod",
                            "annotations": {"engineering.gntik.ai/deployment-sha": revision},
                        },
                        "spec": {"containers": [{"name": "api", "image": "example.invalid/api"}]},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [{"name": "api", "imageID": digest}],
                        },
                    }
                ]
            }
        return CommandResult(0, json.dumps(value), "")


class IndeterminateObserverRunner(FakeObserverRunner):
    def run(self, executable: str, arguments: Any, **kwargs: Any) -> CommandResult:
        if "auth" in arguments:
            return CommandResult(1, "", "authorization endpoint unavailable")
        return super().run(executable, arguments, **kwargs)


class FakeCleanupManager:
    def __init__(self, branch: str, *, dirty: bool = False) -> None:
        self.branch = branch
        self.dirty = dirty
        self.cleaned: list[str] = []

    def _run(self, arguments: list[str], path: Path) -> str:
        if arguments[:2] == ["git", "branch"]:
            return self.branch
        if arguments[:2] == ["git", "rev-parse"]:
            return "b" * 40
        raise AssertionError(arguments)

    def is_dirty(self, path: Path) -> bool:
        return self.dirty

    def cleanup(
        self, allocation: Any, *, evidence_captured: bool, archive_dirty: bool = False
    ) -> None:
        assert evidence_captured is False
        self.cleaned.append(str(allocation.path))


@pytest.mark.asyncio
async def test_project_snapshot_maps_both_transaction_repositories(tmp_path: Path) -> None:
    manager = FakeWorktreeManager(tmp_path)
    handler = ScanActivityHandler(
        Settings(mirror_root=tmp_path, worktree_root=tmp_path / "worktrees"),
        manager,  # type: ignore[arg-type]
    )

    result = await handler.handle(
        "project.snapshot",
        {
            "project": "musematic",
            "campaign_id": str(uuid4()),
            "source_sha": "a" * 40,
            "deployment_sha": "b" * 40,
        },
    )

    assert result["status"] == "SNAPSHOT_VERIFIED"
    assert manager.snapshots == ["musematic", "musematic-deploy"]
    assert result["deployment_snapshot"]["sha"] == "b" * 40
    assert manager.release_reads == [
        ("musematic-deploy", tmp_path / "musematic-deploy" / str(result["campaign_id"]))
    ]


@pytest.mark.asyncio
async def test_worktree_allocation_requires_independent_providers(tmp_path: Path) -> None:
    manager = FakeWorktreeManager(tmp_path)
    handler = ScanActivityHandler(Settings(), manager)  # type: ignore[arg-type]
    change_set_id: UUID = uuid4()
    request = {
        "change_set_id": str(change_set_id),
        "project": "falcone",
        "issue": "123",
        "source_repository": "falcone",
        "source_base_sha": "a" * 40,
        "deployment_repository": "falcone-charts",
        "deployment_base_sha": "b" * 40,
        "maker_provider": "codex",
        "checker_provider": "codex",
    }

    with pytest.raises(ApplicationError, match="must differ"):
        await handler.handle("delivery.allocate_worktrees", {"allocation_request": request})


def test_release_impact_classifier_escalates_migrations_and_security_boundaries() -> None:
    impact = ScanActivityHandler._classify_release_impact(
        repository="falcone-charts",
        changed_paths=[
            "charts/in-falcone/templates/clusterrole.yaml",
            "migrations/0042.sql",
            "src/auth/policy.py",
        ],
        diff=(
            "+kind: ClusterRole\n+ALTER TABLE tenant_credentials ADD COLUMN encrypted_value text;\n"
        ),
    )

    assert impact["risk_floor"] == "R3"
    assert impact["migration"] is True
    assert impact["categories"] == [
        "auth_or_tenant_boundary",
        "cluster_scoped",
        "migration",
    ]
    assert len(impact["sha256"]) == 64


@pytest.mark.asyncio
async def test_runtime_observation_reduces_read_only_cluster_evidence(tmp_path: Path) -> None:
    credential_root = tmp_path / "observer"
    credential_root.mkdir()
    kubeconfig = credential_root / "falcone.kubeconfig"
    kubeconfig.write_text("scoped observer test config", encoding="utf-8")
    os.chmod(kubeconfig, 0o600)
    run_root = tmp_path / "runs"
    (run_root / "scan").mkdir(parents=True)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    source_worktree = tmp_path / "worktrees" / "falcone" / "change-set"
    deployment_worktree = tmp_path / "worktrees" / "falcone-charts" / "change-set"
    source_worktree.mkdir(parents=True)
    deployment_worktree.mkdir(parents=True)
    manager = FakeWorktreeManager(tmp_path / "worktrees")
    handler = ScanActivityHandler(
        Settings(
            observer_credential_root=credential_root,
            run_root=run_root,
            evidence_root=evidence_root,
            environment="development",
        ),
        manager,  # type: ignore[arg-type]
        observer_runner=FakeObserverRunner(),  # type: ignore[arg-type]
    )
    change_set_id = uuid4()
    deployment_id = uuid4()
    digest = f"sha256:{'e' * 64}"

    result = await handler.handle(
        "runtime.deterministic_checks",
        {
            "project": "falcone",
            "change_set_id": str(change_set_id),
            "deployment_id": str(deployment_id),
            "runtime_observation_plan": {
                "project": "falcone",
                "change_set_id": str(change_set_id),
                "deployment_id": str(deployment_id),
                "kubeconfig_path": str(kubeconfig),
                "kube_context": "falcone-observer",
                "namespace": "falcone-staging",
                "resources": ["deployments", "pods"],
                "source_worktree": str(source_worktree),
                "deployment_worktree": str(deployment_worktree),
                "expected_source_sha": "b" * 40,
                "expected_deployment_sha": "d" * 40,
                "expected_image_digests": {"api": digest},
                "revision_annotation": "engineering.gntik.ai/deployment-sha",
                "argo_application": None,
            },
        },
    )

    assert result["status"] == "RUNTIME_OBSERVATION_PASS"
    observation = result["runtime_observation"]
    assert observation["revision_matches"] is True
    assert observation["digests_match"] is True
    assert observation["resources"]["pods"]["names"] == ["api-pod"]


@pytest.mark.asyncio
async def test_runtime_observation_rejects_indeterminate_authorization(tmp_path: Path) -> None:
    credential_root = tmp_path / "observer"
    credential_root.mkdir()
    kubeconfig = credential_root / "falcone.kubeconfig"
    kubeconfig.write_text("scoped observer test config", encoding="utf-8")
    os.chmod(kubeconfig, 0o600)
    run_root = tmp_path / "runs"
    (run_root / "scan").mkdir(parents=True)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    source_worktree = tmp_path / "worktrees" / "falcone" / "change-set"
    deployment_worktree = tmp_path / "worktrees" / "falcone-charts" / "change-set"
    source_worktree.mkdir(parents=True)
    deployment_worktree.mkdir(parents=True)
    handler = ScanActivityHandler(
        Settings(
            observer_credential_root=credential_root,
            run_root=run_root,
            evidence_root=evidence_root,
            environment="development",
        ),
        FakeWorktreeManager(tmp_path / "worktrees"),  # type: ignore[arg-type]
        observer_runner=IndeterminateObserverRunner(),  # type: ignore[arg-type]
    )
    digest = f"sha256:{'e' * 64}"

    with pytest.raises(ApplicationError, match="least-privilege"):
        await handler.handle(
            "runtime.deterministic_checks",
            {
                "runtime_observation_plan": {
                    "project": "falcone",
                    "change_set_id": str(uuid4()),
                    "deployment_id": str(uuid4()),
                    "kubeconfig_path": str(kubeconfig),
                    "kube_context": "falcone-observer",
                    "namespace": "falcone-staging",
                    "resources": ["deployments", "pods"],
                    "source_worktree": str(source_worktree),
                    "deployment_worktree": str(deployment_worktree),
                    "expected_source_sha": "b" * 40,
                    "expected_deployment_sha": "d" * 40,
                    "expected_image_digests": {"api": digest},
                    "revision_annotation": "engineering.gntik.ai/deployment-sha",
                    "argo_application": None,
                }
            },
        )


@pytest.mark.asyncio
async def test_cleanup_protects_dirty_and_executes_only_controller_authorization(
    tmp_path: Path,
) -> None:
    worktree_root = tmp_path / "worktrees"
    change_set_id = uuid4()
    lease_id = uuid4()
    path = worktree_root / "falcone" / str(change_set_id)
    path.mkdir(parents=True)
    branch = f"agent/falcone/123/{change_set_id}"
    candidate = {
        "lease_id": str(lease_id),
        "change_set_id": str(change_set_id),
        "repository": "falcone",
        "worktree_path": str(path),
        "branch": branch,
        "base_sha": "a" * 40,
        "reason": "RELEASED_RETENTION_EXPIRED",
    }
    dirty_handler = ScanActivityHandler(
        Settings(worktree_root=worktree_root),
        FakeCleanupManager(branch, dirty=True),  # type: ignore[arg-type]
    )
    protected = await dirty_handler.handle(
        "cleanup.protect_active", {"cleanup_candidates": [candidate]}
    )
    assert protected["eligible_candidates"] == []
    assert protected["protected_candidates"][0]["protection"] == "DIRTY_WORKTREE"

    manager = FakeCleanupManager(branch)
    handler = ScanActivityHandler(
        Settings(worktree_root=worktree_root, cleanup_execution_enabled=True),
        manager,  # type: ignore[arg-type]
    )
    inspected = await handler.handle("cleanup.protect_active", {"cleanup_candidates": [candidate]})
    assert inspected["eligible_candidates"][0]["inspection"] == "CLEAN"
    workflow_id = "cleanup-test"
    authorization_sha = cleanup_authorization_sha256(
        workflow_id=workflow_id,
        worktrees=[candidate],
        files=[],
        component_leases=[],
        preview_namespaces=[],
    )
    disabled_handler = ScanActivityHandler(
        Settings(worktree_root=worktree_root),
        manager,  # type: ignore[arg-type]
    )
    with pytest.raises(ApplicationError, match="disabled by policy"):
        await disabled_handler.handle(
            "cleanup.execute",
            {
                "authorized_candidates": [candidate],
                "authorized_file_candidates": [],
                "authorized_component_lease_candidates": [],
                "authorized_preview_namespace_candidates": [],
                "cleanup_authorization_sha256": authorization_sha,
                "orchestration": {"workflow_id": workflow_id},
            },
        )
    result = await handler.handle(
        "cleanup.execute",
        {
            "authorized_candidates": [candidate],
            "authorized_file_candidates": [],
            "authorized_component_lease_candidates": [],
            "authorized_preview_namespace_candidates": [],
            "cleanup_authorization_sha256": authorization_sha,
            "orchestration": {"workflow_id": workflow_id},
        },
    )
    assert result["removed_lease_ids"] == [str(lease_id)]
    assert manager.cleaned == [str(path)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step", "expected_method"),
    [
        ("project.snapshot_pair", "_snapshot_pair"),
        ("runtime.deterministic_checks", "_runtime_deterministic_checks"),
        ("identity.observer_attest", "_observer_attestation"),
        ("reconcile.apply_promotion", "_apply_promotion"),
        ("delivery.deterministic_prechecks", "_delivery_prechecks"),
        ("delivery.validate_changeset", "_validate_changeset"),
        ("delivery.prepare_toolchain", "_prepare_toolchain"),
    ],
)
async def test_scan_dispatch_runs_blocking_kubectl_and_scanner_steps_off_the_event_loop(
    step: str, expected_method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-08: these steps all end up calling kubectl (via KubernetesObserver), git (via
    the promotion runner), or gitleaks/trivy/semgrep/syft (via DeterministicScanner) --
    all blocking subprocess calls through SafeCommandRunner. Confirm handle() schedules
    the actual work via asyncio.to_thread rather than running it inline on the event loop.
    """
    handler = ScanActivityHandler(Settings())  # type: ignore[arg-type]
    calls: list[str] = []

    async def fake_to_thread(func: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(getattr(func, "__name__", str(func)))
        return {"status": "OK"}

    monkeypatch.setattr(scan_module.asyncio, "to_thread", fake_to_thread)

    result = await handler.handle(step, {"status": "PENDING"})

    assert calls == [expected_method]
    assert result == {"status": "OK"}


class BaselineScanner:
    """Scanner double: each repository's findings for the current phase (pre/post)."""

    def __init__(self) -> None:
        self.findings: dict[str, dict[str, Any]] = {}

    def _for(self, worktree: Path) -> dict[str, Any]:
        return self.findings.get(worktree.name, {})

    def secret_scan(self, worktree: Path, output: Path) -> dict[str, Any]:
        output.mkdir(parents=True, exist_ok=True)
        (output / "gitleaks.json").write_text("[]\n", encoding="utf-8")
        found = [
            {"RuleID": rule, "File": f"{worktree}/{path}", "StartLine": line}
            for rule, path, line in self._for(worktree).get("secrets", [])
        ]
        return {"status": "FINDINGS" if found else "PASS", "findings": found}

    def vulnerability_scan(self, worktree: Path, output: Path) -> dict[str, Any]:
        vulnerabilities = [
            {
                "VulnerabilityID": cve,
                "PkgName": package,
                "InstalledVersion": "1.0",
                "Severity": "HIGH",
            }
            for cve, package in self._for(worktree).get("vulnerabilities", [])
        ]
        return {"Results": [{"Target": "requirements.txt", "Vulnerabilities": vulnerabilities}]}

    def semgrep(self, worktree: Path, output: Path) -> dict[str, Any]:
        return {
            "results": [
                {"check_id": rule, "path": f"{worktree}/{path}", "start": {"line": line}}
                for rule, path, line in self._for(worktree).get("semgrep", [])
            ]
        }

    def sbom(self, worktree: Path, output: Path) -> dict[str, Any]:
        return {}


class ChangeManager:
    def change_evidence(self, path: Path, *, base_sha: str) -> tuple[dict[str, Any], str]:
        return (
            {"changed_file_count": 1, "changed_paths": ["app/service.py"]},
            "--- a/app/service.py\n+++ b/app/service.py\n+fixed = True\n",
        )

    def grant_delivery_read(self, repository: str, path: Path) -> None:
        pass

    def grant_release_read(self, repository: str, path: Path) -> None:
        pass


class ReceiptPublisher:
    def publish(self, path: Path, **_: Any) -> dict[str, Any]:
        return {"id": str(uuid4()), "filename": path.name}


def _baseline_handler(
    tmp_path: Path,
) -> tuple[ScanActivityHandler, BaselineScanner, dict[str, Any]]:
    worktrees = tmp_path / "worktrees"
    for name in ("source", "deployment"):
        (worktrees / name).mkdir(parents=True)
    scanner = BaselineScanner()
    handler = ScanActivityHandler(
        Settings(evidence_root=tmp_path / "evidence", worktree_root=worktrees),
        ChangeManager(),  # type: ignore[arg-type]
        scanner=scanner,  # type: ignore[arg-type]
        publisher=ReceiptPublisher(),  # type: ignore[arg-type]
    )
    maker = {"structured_result": {"tests": [{"status": "PASS"}]}}
    payload = {
        "project": "musematic",
        "change_set_id": str(uuid4()),
        "source_worktree": str(worktrees / "source"),
        "deployment_worktree": str(worktrees / "deployment"),
        "source_repository": "musematic",
        "deployment_repository": "musematic-deploy",
        "source_base_sha": "a" * 40,
        "deployment_base_sha": "b" * 40,
        "agent_result": {"structured_result": {"decision": "PASS"}},
        "source_maker_result": maker,
        "deployment_maker_result": maker,
    }
    return handler, scanner, payload


EXISTING = {
    "secrets": [("generic-api-key", "tests/fixtures/sample.env", 3)],
    "semgrep": [("python.lang.security.audit.eval", "app/service.py", 10)],
    "vulnerabilities": [("CVE-2024-0001", "requests")],
}


def test_prechecks_record_existing_findings_as_baseline_instead_of_blocking(
    tmp_path: Path,
) -> None:
    # NEW-28: musematic's unmodified repo (156 semgrep findings, 363 HIGH/CRITICAL vulns,
    # gitleaks findings) blocked every delivery at prechecks, before any change existed.
    handler, scanner, payload = _baseline_handler(tmp_path)
    scanner.findings = {"source": EXISTING, "deployment": EXISTING}

    prechecks = handler._delivery_prechecks(payload)

    assert prechecks["status"] == "PRECHECKS_PASS"
    assert prechecks["blocker"] is None
    assert prechecks["prechecks"]["source"]["semgrep_findings"] == 1


def test_postchecks_pass_when_the_change_only_shifts_existing_findings(tmp_path: Path) -> None:
    handler, scanner, payload = _baseline_handler(tmp_path)
    scanner.findings = {"source": EXISTING, "deployment": EXISTING}
    handler._delivery_prechecks(payload)
    shifted = {
        "secrets": [("generic-api-key", "tests/fixtures/sample.env", 9)],
        "semgrep": [("python.lang.security.audit.eval", "app/service.py", 14)],
        "vulnerabilities": EXISTING["vulnerabilities"],
    }
    scanner.findings = {"source": shifted, "deployment": EXISTING}

    validated = handler._validate_changeset(payload)

    assert validated["status"] == "LOCAL_VALIDATION_PASS"
    source = validated["change_evidence"]["source"]
    assert source["introduced_findings"] == {"secrets": 0, "semgrep": 0, "vulnerabilities": 0}
    assert source["diff_withheld"] is False


@pytest.mark.parametrize(
    ("category", "added", "withheld"),
    [
        ("semgrep", ("python.lang.security.audit.eval", "app/service.py", 30), False),
        ("secrets", ("aws-access-token", "app/service.py", 2), True),
        ("vulnerabilities", ("CVE-2025-9999", "urllib3"), False),
    ],
)
def test_postchecks_block_findings_the_change_introduces(
    tmp_path: Path, category: str, added: tuple[Any, ...], withheld: bool
) -> None:
    handler, scanner, payload = _baseline_handler(tmp_path)
    scanner.findings = {"source": EXISTING, "deployment": EXISTING}
    handler._delivery_prechecks(payload)
    changed = {key: list(value) for key, value in EXISTING.items()}
    changed[category].append(added)
    scanner.findings = {"source": changed, "deployment": EXISTING}

    validated = handler._validate_changeset(payload)

    # NEW-76: introduced findings go back to the maker as a correction request, named.
    assert validated["status"] == "REQUEST_CHANGES"
    assert validated["blocker"] == "CHANGESET_VALIDATION_FINDINGS"
    source = validated["change_evidence"]["source"]
    assert source["introduced_findings"][category] == 1
    assert source["diff_withheld"] is withheld
    [finding] = validated["validation_findings"]
    assert finding.startswith("source: ") and str(added[0]) in finding


def test_postchecks_fail_closed_to_absolute_gate_without_a_baseline(tmp_path: Path) -> None:
    handler, scanner, payload = _baseline_handler(tmp_path)
    scanner.findings = {"source": EXISTING, "deployment": EXISTING}

    validated = handler._validate_changeset(payload)

    assert validated["status"] == "BLOCKED"
    assert validated["change_evidence"]["source"]["diff_withheld"] is True


class ResumingManager:
    """NEW-52 allocation double: the source worktree resumes, the deployment does not."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.resume_flags: list[bool] = []

    def allocate(self, **kwargs: Any) -> WorktreeAllocation:
        self.resume_flags.append(kwargs["resume_prior_commits"])
        repository = kwargs["repository"]
        return WorktreeAllocation(
            repository=repository,
            path=self.root / repository,
            branch=f"agent/{repository}",
            base_sha=kwargs["expected_base_sha"],
            change_set_id=kwargs["change_set_id"],
            resumed_from_sha="d" * 40 if repository == "musematic" else None,
        )


@pytest.mark.parametrize("enabled", [True, False])
def test_allocation_threads_the_resume_setting_and_resumed_head(
    tmp_path: Path, enabled: bool
) -> None:
    manager = ResumingManager(tmp_path)
    handler = ScanActivityHandler(
        Settings(delivery_resume_prior_commits=enabled),
        manager,  # type: ignore[arg-type]
    )
    request = {
        "change_set_id": str(uuid4()),
        "project": "musematic",
        "issue": "993",
        "source_repository": "musematic",
        "source_base_sha": "a" * 40,
        "deployment_repository": "musematic-deploy",
        "deployment_base_sha": "b" * 40,
        "maker_provider": "codex",
        "checker_provider": "claude",
    }

    allocated = handler._allocate({"allocation_request": request})

    assert manager.resume_flags == [enabled, enabled]
    assert allocated["status"] == "WORKTREES_ALLOCATED"
    assert allocated["source_resumed_from_sha"] == "d" * 40
    assert allocated["deployment_resumed_from_sha"] is None


class BaseMirrorManager(ChangeManager):
    def __init__(self, mirror: Path) -> None:
        self.mirror = mirror

    def verify_mirror(self, repository: str) -> Path:
        assert repository == "musematic"
        return self.mirror


class RecordingBaselineScanner(BaselineScanner):
    def __init__(self) -> None:
        super().__init__()
        self.scanned: list[tuple[Path, list[str]]] = []

    def secret_scan(self, worktree: Path, output: Path) -> dict[str, Any]:
        files = sorted(item.name for item in worktree.iterdir())
        self.scanned.append((worktree, files))
        return super().secret_scan(worktree, output)


def test_resumed_worktree_prechecks_baseline_is_taken_at_the_base(tmp_path: Path) -> None:
    # NEW-52: a resumed worktree already holds the previous attempt's commits. Its own
    # findings must not become the NEW-28 baseline, or postchecks would miss them.
    import shutil
    import subprocess

    git = shutil.which("git")
    assert git is not None
    mirror = tmp_path / "mirrors" / "musematic"
    mirror.mkdir(parents=True)
    (mirror / "README.md").write_text("base\n", encoding="utf-8")

    def run(*arguments: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
            [git, *arguments], cwd=mirror, check=True, capture_output=True, text=True
        ).stdout.strip()

    run("init", "-q")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "base")
    base = run("rev-parse", "HEAD")

    worktrees = tmp_path / "worktrees"
    for name in ("source", "deployment"):
        (worktrees / name).mkdir(parents=True)
    (worktrees / "source" / "resumed_feature.py").write_text("eval(x)\n", encoding="utf-8")
    scanner = RecordingBaselineScanner()
    handler = ScanActivityHandler(
        Settings(
            evidence_root=tmp_path / "evidence",
            worktree_root=worktrees,
            toolchain_cache_root=tmp_path / "toolchain",
        ),
        BaseMirrorManager(mirror),  # type: ignore[arg-type]
        scanner=scanner,  # type: ignore[arg-type]
        publisher=ReceiptPublisher(),  # type: ignore[arg-type]
    )
    maker = {"structured_result": {"tests": [{"status": "PASS"}]}}
    payload = {
        "project": "musematic",
        "change_set_id": str(uuid4()),
        "source_worktree": str(worktrees / "source"),
        "deployment_worktree": str(worktrees / "deployment"),
        "source_repository": "musematic",
        "deployment_repository": "musematic-deploy",
        "source_base_sha": base,
        "deployment_base_sha": "b" * 40,
        "source_resumed_from_sha": "d" * 40,
        "deployment_resumed_from_sha": None,
        "agent_result": {"structured_result": {"decision": "PASS"}},
        "source_maker_result": maker,
        "deployment_maker_result": maker,
    }
    introduced = ("python.lang.security.audit.eval", "resumed_feature.py", 1)
    resumed = {**EXISTING, "semgrep": [*EXISTING["semgrep"], introduced]}
    # The resumed worktree already carries the previous attempt's finding; the base
    # checkout ("source-base") only has the pre-existing ones.
    scanner.findings = {"source": resumed, "source-base": EXISTING, "deployment": EXISTING}

    prechecks = handler._delivery_prechecks(payload)

    assert prechecks["status"] == "PRECHECKS_PASS"
    base_scan = next(item for item in scanner.scanned if item[0].name == "source-base")
    assert base_scan[1] == ["README.md"]  # the base tree, without the resumed commit
    assert not base_scan[0].exists()  # the scratch checkout is removed afterwards
    assert (worktrees / "deployment").resolve() in [item[0] for item in scanner.scanned]

    validated = handler._validate_changeset(payload)

    # NEW-76: the maker's own finding is handed back to it by name.
    assert validated["status"] == "REQUEST_CHANGES"
    assert validated["blocker"] == "CHANGESET_VALIDATION_FINDINGS"
    assert any("resumed_feature.py" in item for item in validated["validation_findings"])
    assert validated["change_evidence"]["source"]["introduced_findings"]["semgrep"] == 1
    assert validated["change_evidence"]["deployment"]["introduced_findings"]["semgrep"] == 0
