"""Finding verifiers (revalidation) run with the toolchain the project's makers get.

falcone-serverless #62: the verifier sandbox had the worker's Go 1.25 (go.mod needs 1.26),
so neither control could run and the delivery paused CURRENT_REVALIDATION_FLAKY. The fresh
snapshot (scan worker, where the installs are) resolves the selection exactly like
delivery.prepare_toolchain, and the verifier request carries it to the provider sandbox.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from test_language_toolchain import _language_installs
from test_node_toolchain import RecordingAdapter, _manifests
from test_provider_activity import FakeInstructionPack, request_for

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.process import SandboxToolchain
from engineering_control.config import Settings
from engineering_control.engineering.toolchain import (
    TOOLCHAIN_GO_UNAVAILABLE,
    load_toolchain_policy,
    payload_node_toolchain,
    resolve_node_toolchain,
)
from engineering_control.models.enums import Provider
from engineering_control.worktrees.manager import WorktreeAllocation

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "config" / "projects"
SHA = "a" * 40


class _SnapshotManager:
    def __init__(self, root: Path) -> None:
        self.root = root

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


def _snapshot(settings: Settings, tmp_path: Path, project: str) -> dict[str, Any]:
    handler = ScanActivityHandler(settings, _SnapshotManager(tmp_path / "wt"))  # type: ignore[arg-type]
    return handler._fresh_finding_snapshot(
        {
            "finding_id": str(uuid4()),
            "project": project,
            "affected_source_sha": SHA,
            "affected_deployment_sha": SHA,
            "flow": "repository-static",
        }
    )


def _maker_selection(settings: Settings, project: str) -> dict[str, str | None] | None:
    """What delivery.prepare_toolchain puts in the maker's payload for this project."""
    policy = load_toolchain_policy(settings.project_config_root, project)
    return resolve_node_toolchain(settings, policy).selection()


def _prepare(
    tmp_path: Path, project: str, snapshot: dict[str, Any]
) -> tuple[dict[str, Any], MetadataActivityHandler]:
    handler = MetadataActivityHandler(
        Settings(
            database_url=SecretStr(f"sqlite:///{tmp_path / 'verifier.sqlite'}"),
            project_config_root=PROJECTS,
        )
    )
    routing = SimpleNamespace(primary=Provider.CLAUDE, reason="test", status="READY")
    broker = SimpleNamespace(route=lambda **_: routing)
    handler._quota_broker = lambda: broker  # type: ignore[assignment,method-assign,return-value]
    finding = {
        "finding_id": str(uuid4()),
        "campaign_id": str(uuid4()),
        "project": project,
        "discoverer_provider": "codex",
        "risk_class": "R1",
        "flow": "current-state-revalidation",
        "fingerprint": "f" * 64,
        "expected_behavior": "controller builds",
        "actual_behavior": "controller fails",
        "minimal_reproduction": ["go test ./..."],
        "affected_component": "controller",
    }
    prepared = handler._prepare_verifier(
        {
            "finding": finding,
            "snapshot": snapshot,
            "ordinal": 1,
            "orchestration": {"workflow_id": f"finding-verify-{uuid4()}"},
        }
    )
    return prepared, handler


def test_toolchain_project_snapshot_carries_the_makers_selection(tmp_path: Path) -> None:
    settings = Settings(project_config_root=PROJECTS, **_language_installs(tmp_path))
    snapshot = _snapshot(settings, tmp_path, "falcone-serverless")
    selection = _maker_selection(settings, "falcone-serverless")
    assert selection is not None and selection["go"] == "1.26" and selection["rust"] == "1.99.1"
    assert snapshot["toolchain_preparation"] == {"node_toolchain": selection}
    # The selection is outside the hashed evidence: the anchor is the default project's.
    default = _snapshot(Settings(project_config_root=PROJECTS), tmp_path / "d", "falcone")
    assert "toolchain_preparation" not in default
    assert set(default) == set(snapshot) - {"toolchain_preparation"}


async def test_verifier_request_gets_the_makers_sandbox_toolchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installs = _language_installs(tmp_path)
    settings = Settings(project_config_root=PROJECTS, **installs)
    snapshot = _snapshot(settings, tmp_path, "falcone-serverless")
    prepared, _ = _prepare(tmp_path, "falcone-serverless", snapshot)
    assert prepared["status"] == "READY"
    selection = _maker_selection(settings, "falcone-serverless")
    assert prepared["toolchain_preparation"] == {"node_toolchain": selection}
    maker_payload = {
        "toolchain_preparation": {"status": "TOOLCHAIN_READY", "node_toolchain": selection}
    }
    expected = payload_node_toolchain(settings, maker_payload).sandbox()
    assert payload_node_toolchain(settings, prepared).sandbox() == expected
    prompt = prepared["verifier_request"]["prompt"]
    assert "the project's pinned toolchain" in prompt
    assert "`cargo test --workspace --locked`" in prompt
    assert prepared["verifier_request"]["writable"] is False

    # The provider worker passes exactly that toolchain to the verifier's sandbox.
    monkeypatch.setattr(
        "engineering_control.activities.provider.activity.heartbeat", lambda _: None
    )
    worktree = tmp_path / "worktrees" / "falcone" / "snapshot"
    worktree.mkdir(parents=True)
    adapter = RecordingAdapter()
    provider = ProviderActivityHandler(
        Settings(worktree_root=tmp_path / "worktrees", run_root=tmp_path / "runs", **installs),
        "codex",
        adapter,
        FakeInstructionPack(),
    )
    request = request_for(worktree, writable=False).model_dump(mode="json")
    result = await provider.handle(
        "finding.reproduce",
        {"verifier_request": request, "toolchain_preparation": prepared["toolchain_preparation"]},
    )
    assert result["status"] == "SUCCESS"
    [sandbox] = adapter.toolchains
    assert isinstance(sandbox, SandboxToolchain) and sandbox == expected


def test_default_project_verifier_request_is_unchanged(tmp_path: Path) -> None:
    settings = Settings(project_config_root=PROJECTS, **_language_installs(tmp_path))
    assert _maker_selection(settings, "falcone") is None
    snapshot = _snapshot(settings, tmp_path, "falcone")
    prepared, _ = _prepare(tmp_path, "falcone", snapshot)
    assert prepared["status"] == "READY"
    assert "toolchain_preparation" not in prepared
    assert payload_node_toolchain(settings, prepared).sandbox() is None
    assert "pinned toolchain" not in prepared["verifier_request"]["prompt"]


def test_unavailable_selection_blocks_the_verifier(tmp_path: Path) -> None:
    settings = Settings(project_config_root=PROJECTS, **_language_installs(tmp_path, go=False))
    snapshot = _snapshot(settings, tmp_path, "falcone-serverless")
    assert snapshot["toolchain_preparation"]["status"] == "BLOCKED"
    assert snapshot["toolchain_preparation"]["blocker"] == TOOLCHAIN_GO_UNAVAILABLE
    prepared, _ = _prepare(tmp_path, "falcone-serverless", snapshot)
    assert prepared["status"] == "BLOCKED"
    assert prepared["blocker"] == TOOLCHAIN_GO_UNAVAILABLE
    assert "verifier_request" not in prepared


def test_invalid_manifest_blocks_the_verifier_snapshot(tmp_path: Path) -> None:
    root = _manifests(tmp_path, "falcone-serverless", {"go": "go1.26"})
    snapshot = _snapshot(Settings(project_config_root=root), tmp_path, "falcone-serverless")
    assert snapshot["toolchain_preparation"]["blocker"] == "TOOLCHAIN_CONTRACT_INVALID"
    UUID(snapshot["finding_id"])


def test_missing_manifest_blocks_the_verifier_like_the_maker(tmp_path: Path) -> None:
    # Codex review 1: never the system toolchain for a configured project's verifier.
    root = _manifests(tmp_path, "falcone", {})
    (root / "falcone-serverless.yaml").unlink()
    settings = Settings(project_config_root=root, **_language_installs(tmp_path))
    snapshot = _snapshot(settings, tmp_path, "falcone-serverless")
    assert snapshot["toolchain_preparation"]["status"] == "BLOCKED"
    assert snapshot["toolchain_preparation"]["blocker"] == "TOOLCHAIN_CONTRACT_INVALID"
    prepared, _ = _prepare(tmp_path, "falcone-serverless", snapshot)
    assert (prepared["status"], prepared["blocker"]) == ("BLOCKED", "TOOLCHAIN_CONTRACT_INVALID")


def test_malformed_yaml_blocks_the_verifier_snapshot(tmp_path: Path) -> None:
    # Codex review 2: a YAML parse error is a contract blocker, never an activity failure.
    root = _manifests(tmp_path, "falcone", {})
    (root / "falcone-serverless.yaml").write_text(
        "project: falcone-serverless\ntoolchain: [\n", encoding="utf-8"
    )
    settings = Settings(project_config_root=root, **_language_installs(tmp_path))
    snapshot = _snapshot(settings, tmp_path, "falcone-serverless")
    assert snapshot["toolchain_preparation"]["status"] == "BLOCKED"
    assert snapshot["toolchain_preparation"]["blocker"] == "TOOLCHAIN_CONTRACT_INVALID"
