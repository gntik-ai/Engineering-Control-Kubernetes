from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from engineering_control.validation.c01 import REQUIRED_PROVIDER_RUNS


def load_builder() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "build-c01-acceptance.py"
    spec = importlib.util.spec_from_file_location("c01_acceptance_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_provider_matrix_requires_release_pack_and_no_paid_fallback() -> None:
    module = load_builder()
    release = "a" * 40
    pack = "b" * 40
    runs = {
        key: {
            "provider": provider,
            "role": role,
            "status": "SUCCESS",
            "agent_pack_revision": pack,
            "instruction_bundle_sha256": "c" * 64,
        }
        for key, (provider, role) in REQUIRED_PROVIDER_RUNS.items()
    }
    value = {
        "status": "PASS",
        "release_sha": release,
        "redaction_status": "REDACTED",
        "authentication_mode": "native-subscription-cli",
        "paid_api_fallback": False,
        "directions": {"codex_to_claude": "PASS", "claude_to_codex": "PASS"},
        "provider_runs": runs,
    }

    module.validate_provider(value, release, pack)
    runs["codex_maker"]["status"] = "success"
    with pytest.raises(ValueError, match="codex_maker"):
        module.validate_provider(value, release, pack)
    runs["codex_maker"]["status"] = "SUCCESS"
    value["paid_api_fallback"] = True
    with pytest.raises(ValueError, match="native replacement"):
        module.validate_provider(value, release, pack)


def test_lifecycle_requires_real_artifact_digest_and_restart(tmp_path: Path) -> None:
    module = load_builder()
    artifact = tmp_path / "lifecycle.json"
    artifact.write_text("evidence\n", encoding="utf-8")
    value = {
        "status": "PASS",
        "release_sha": "a" * 40,
        "change_set_state": "AWAITING_STAGING_APPROVAL",
        "workflow_worker_restarted": True,
        "authenticated_api_path": True,
        "maker_provider": "codex-synthetic",
        "checker_provider": "claude-synthetic",
        "redaction_status": "SCANNED_CLEAN",
        "evidence_path": str(artifact),
        "evidence_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }

    module.validate_lifecycle(value, "a" * 40, tmp_path)
    value["workflow_worker_restarted"] = False
    with pytest.raises(ValueError, match="restart/resume"):
        module.validate_lifecycle(value, "a" * 40, tmp_path)


def test_telegram_sequence_must_be_release_bound() -> None:
    module = load_builder()
    release = "a" * 40
    value = {
        "status": "PASS",
        "release_sha": release,
        "commands": ["usage", "validate", "status"],
        "events": [{"release_sha": release} for _ in range(3)],
    }
    module.validate_telegram(value, release)

    value["events"][2]["release_sha"] = "b" * 40
    with pytest.raises(ValueError, match="release-bound"):
        module.validate_telegram(value, release)


def test_usage_keeps_unexposed_capacity_explicitly_unknown() -> None:
    module = load_builder()
    value = {
        provider: {
            "status": "AVAILABLE",
            "authenticated": True,
            "cli_present": True,
            "rate_limited": False,
            "remaining_capacity": None,
            "total_capacity": None,
            "reset_at": None,
        }
        for provider in ("codex", "claude")
    }

    module.validate_usage(value)
    del value["claude"]["remaining_capacity"]
    with pytest.raises(ValueError, match="omits remaining_capacity"):
        module.validate_usage(value)
