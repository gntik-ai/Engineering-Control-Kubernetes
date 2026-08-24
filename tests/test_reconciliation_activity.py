from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from engineering_control.activities.delivery import DeliveryActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.config import Settings

DIGESTS = {
    "control-plane": "a" * 64,
    "control-plane-executor": "b" * 64,
    "web-console": "c" * 64,
    "function-runtime": "d" * 64,
    "workflow-worker": "e" * 64,
    "mcp-runtime": "f" * 64,
}
REPOSITORIES = {
    "control-plane": "ghcr.io/gntik-ai/in-falcone-control-plane",
    "control-plane-executor": "ghcr.io/gntik-ai/in-falcone-control-plane-executor",
    "web-console": "ghcr.io/gntik-ai/in-falcone-web-console",
    "function-runtime": "ghcr.io/gntik-ai/in-falcone-fn-runtime",
    "workflow-worker": "ghcr.io/gntik-ai/in-falcone-workflow-worker",
    "mcp-runtime": "ghcr.io/gntik-ai/in-falcone-mcp-runtime",
}


def reconciliation_payload(change_set_id: Any, run_id: Any, *, enabled: bool) -> dict[str, Any]:
    branch = f"agent/falcone/123/{change_set_id}"
    return {
        "dry_run": True,
        "reconciliation_plan": {
            "change_set_id": str(change_set_id),
            "run_id": str(run_id),
            "workflow_id": f"reconcile-{change_set_id}",
            "project": "falcone",
            "repository": "gntik-ai/falcone",
            "source_pr_number": 7,
            "source_pr_url": "https://github.com/gntik-ai/falcone/pull/7",
            "source_branch": branch,
            "source_head_sha": "1" * 40,
            "source_base_branch": "main",
            "candidate_build_run_id": 99,
            "policy": {
                "source_ci_workflow": "ci",
                "candidate_build": {
                    "enabled": enabled,
                    "workflow_name": "release-images",
                    "artifact_name": "engineering-control-artifacts",
                    "manifest_file": "engineering-artifacts.json",
                    "signer_workflow": ("gntik-ai/falcone/.github/workflows/release-images.yml"),
                    "deny_self_hosted_runners": True,
                    "blocker": None if enabled else "CANDIDATE_BUILD_CONTRACT_MISSING",
                },
                "immutable_promotion": {
                    "enabled": True,
                    "deployment_file": "charts/in-falcone/values/staging.yaml",
                    "chart_directory": "charts/in-falcone",
                    "values_files": ["charts/in-falcone/values/staging.yaml"],
                    "component_lease": "falcone:charts:staging",
                    "image_repositories": REPOSITORIES,
                    "digest_paths": {
                        component: f"images.{component}.digest" for component in REPOSITORIES
                    },
                    "blocker": None,
                },
            },
            "independent_review_sha256": "2" * 64,
            "independent_review_provider": "claude",
            "maker_provider": "codex",
        },
    }


class FakeReconciliationRunner:
    def __init__(self, manifest: dict[str, Any]) -> None:
        self.manifest = manifest
        self.attestations = 0

    def run(
        self,
        executable: str,
        arguments: list[str],
        *,
        cwd: Path,
        **kwargs: Any,
    ) -> CommandResult:
        assert executable == "gh"
        if arguments[:2] == ["pr", "view"]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "number": 7,
                        "url": "https://github.com/gntik-ai/falcone/pull/7",
                        "state": "OPEN",
                        "isDraft": True,
                        "headRefName": self.manifest["branch"],
                        "headRefOid": "1" * 40,
                        "baseRefName": "main",
                        "mergeStateStatus": "BLOCKED",
                        "reviewDecision": "APPROVED",
                        "statusCheckRollup": [
                            {
                                "name": "unit",
                                "workflowName": "ci",
                                "status": "COMPLETED",
                                "conclusion": "SUCCESS",
                            }
                        ],
                    }
                ),
                "",
            )
        if arguments[:2] == ["run", "view"]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "databaseId": 99,
                        "workflowName": "release-images",
                        "headSha": "1" * 40,
                        "status": "completed",
                        "conclusion": "success",
                        "event": "workflow_dispatch",
                        "url": "https://github.com/gntik-ai/falcone/actions/runs/99",
                    }
                ),
                "",
            )
        if arguments[:2] == ["run", "download"]:
            destination = Path(arguments[arguments.index("--dir") + 1])
            value = {key: item for key, item in self.manifest.items() if key != "branch"}
            (destination / "engineering-artifacts.json").write_text(
                json.dumps(value), encoding="utf-8"
            )
            return CommandResult(0, "", "")
        if arguments[:2] == ["attestation", "verify"]:
            assert "--source-digest" in arguments
            assert kwargs["environment"]["DOCKER_CONFIG"]
            self.attestations += 1
            return CommandResult(0, '[{"verificationResult":{"statement":{}}}]', "")
        raise AssertionError((cwd, arguments))


def reconciliation_handler(
    tmp_path: Path, runner: FakeReconciliationRunner
) -> DeliveryActivityHandler:
    credential_root = tmp_path / "credentials"
    registry = credential_root / "registry"
    registry.mkdir(parents=True)
    token = credential_root / "github.token"
    token.write_text("t" * 48, encoding="utf-8")
    token.chmod(0o600)
    registry_auth = registry / "config.json"
    registry_auth.write_text('{"auths":{}}', encoding="utf-8")
    registry_auth.chmod(0o600)
    return DeliveryActivityHandler(
        Settings(
            run_root=tmp_path / "runs",
            delivery_credential_root=credential_root,
            github_pr_write_token_file=token,
            github_registry_auth_file=registry_auth,
        ),
        runner,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_reconciliation_verifies_exact_ci_manifest_and_every_attestation(
    tmp_path: Path,
) -> None:
    change_set_id = uuid4()
    run_id = uuid4()
    branch = f"agent/falcone/123/{change_set_id}"
    manifest = {
        "schema_version": 1,
        "project": "falcone",
        "source_repository": "gntik-ai/falcone",
        "source_sha": "1" * 40,
        "workflow_run_id": 99,
        "images": [
            {
                "component": component,
                "repository": REPOSITORIES[component],
                "digest": f"sha256:{digest}",
            }
            for component, digest in DIGESTS.items()
        ],
        "branch": branch,
    }
    runner = FakeReconciliationRunner(manifest)
    handler = reconciliation_handler(tmp_path, runner)

    result = await handler.handle(
        "reconcile.inspect_ci", reconciliation_payload(change_set_id, run_id, enabled=True)
    )

    assert result["status"] == "CANDIDATE_VERIFIED"
    assert result["candidate_status"] == "VERIFIED"
    assert runner.attestations == len(DIGESTS)
    assert result["ci_evidence"]["source_head_sha"] == "1" * 40
    assert result["ci_evidence"]["candidate"]["manifest_sha256"]
    assert "auth" not in json.dumps(result).lower()


@pytest.mark.asyncio
async def test_reconciliation_stops_at_ci_when_candidate_contract_is_disabled(
    tmp_path: Path,
) -> None:
    change_set_id = uuid4()
    run_id = uuid4()
    branch = f"agent/falcone/123/{change_set_id}"
    runner = FakeReconciliationRunner({"branch": branch})
    handler = reconciliation_handler(tmp_path, runner)

    result = await handler.handle(
        "reconcile.inspect_ci", reconciliation_payload(change_set_id, run_id, enabled=False)
    )

    assert result["status"] == "CI_VALIDATED"
    assert result["candidate_status"] == "CANDIDATE_BUILD_CONTRACT_MISSING"
    assert runner.attestations == 0


@pytest.mark.asyncio
async def test_reconciliation_rejects_changed_source_pr_head(tmp_path: Path) -> None:
    change_set_id = uuid4()
    run_id = uuid4()
    branch = f"agent/falcone/123/{change_set_id}"
    runner = FakeReconciliationRunner({"branch": branch})
    handler = reconciliation_handler(tmp_path, runner)
    payload = reconciliation_payload(change_set_id, run_id, enabled=False)
    payload["reconciliation_plan"]["source_head_sha"] = "9" * 40

    result = await handler.handle("reconcile.inspect_ci", payload)

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "SOURCE_PR_IMMUTABLE_BINDING_CHANGED"
    assert result["ci_evidence"]["blocker"] == "SOURCE_PR_IMMUTABLE_BINDING_CHANGED"


@pytest.mark.asyncio
async def test_reconciliation_rejects_broad_registry_auth_permissions(tmp_path: Path) -> None:
    change_set_id = uuid4()
    run_id = uuid4()
    branch = f"agent/falcone/123/{change_set_id}"
    manifest = {
        "schema_version": 1,
        "project": "falcone",
        "source_repository": "gntik-ai/falcone",
        "source_sha": "1" * 40,
        "workflow_run_id": 99,
        "images": [
            {
                "component": component,
                "repository": REPOSITORIES[component],
                "digest": f"sha256:{digest}",
            }
            for component, digest in DIGESTS.items()
        ],
        "branch": branch,
    }
    runner = FakeReconciliationRunner(manifest)
    handler = reconciliation_handler(tmp_path, runner)
    assert handler.settings.github_registry_auth_file is not None
    handler.settings.github_registry_auth_file.chmod(0o644)

    result = await handler.handle(
        "reconcile.inspect_ci", reconciliation_payload(change_set_id, run_id, enabled=True)
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "CI_RECONCILIATION_FAILED"
    assert runner.attestations == 0
