from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "prepare-hermes-c01-cutover.py"
SPEC = importlib.util.spec_from_file_location("prepare_hermes_c01_cutover", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _fixture() -> dict[str, object]:
    return {
        "model": {"provider": "omniroute", "default": "legacy", "api_mode": "legacy"},
        "providers": {
            "omniroute": {
                "api": "http://127.0.0.1:20128/v1",
                "key_env": "OMNIROUTE_API_KEY",
            }
        },
        "custom_providers": [
            {"name": "OmniRoute", "base_url": "http://localhost:20128/v1"},
            {"name": "retained", "base_url": "http://127.0.0.1:11434/v1"},
        ],
        "model_aliases": {
            "coding": {"provider": "omniroute", "model": "legacy"},
            "cheap": {"provider": "omniroute", "model": "legacy"},
            "smart": {"provider": "omniroute", "model": "legacy"},
        },
        "delegation": {"base_url": "http://127.0.0.1:20131/v1", "model": "legacy"},
    }


def test_transform_removes_omniroute_and_preserves_unrelated_provider() -> None:
    candidate, changes = MODULE.transform(_fixture())

    assert candidate["model"]["provider"] == "openai-codex"
    assert "providers" not in candidate
    assert candidate["custom_providers"] == [
        {"name": "retained", "base_url": "http://127.0.0.1:11434/v1"}
    ]
    assert candidate["delegation"]["model"] == "gpt-5.6-luna"
    assert changes
    assert not any(MODULE.OMNI_DEPENDENCY.search(value) for value in MODULE._strings(candidate))


def test_apply_requires_exact_confirmation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exact confirmation"):
        MODULE.apply(
            tmp_path / "config.yaml",
            tmp_path,
            tmp_path / "providers.json",
            tmp_path / "lifecycle.json",
            "wrong",
        )


def test_pre_cutover_evidence_binds_release_pack_and_restart_resume(tmp_path: Path) -> None:
    release = "a" * 40
    agent_pack = "b" * 40
    artifact = tmp_path / "lifecycle-artifact.json"
    artifact.write_text('{"redaction":"clean"}\n', encoding="utf-8")
    artifact.chmod(0o600)
    provider = tmp_path / "providers.json"
    provider_value = {
        "status": "PASS",
        "release_sha": release,
        "redaction_status": "REDACTED",
        "authentication_mode": "native-subscription-cli",
        "paid_api_fallback": False,
        "directions": {"codex_to_claude": "PASS", "claude_to_codex": "PASS"},
        "provider_runs": {
            key: {
                "provider": provider_name,
                "role": role,
                "status": "success",
                "agent_pack_revision": agent_pack,
                "instruction_bundle_sha256": "c" * 64,
            }
            for key, (provider_name, role) in MODULE.REQUIRED_PROVIDER_RUNS.items()
        },
    }
    provider.write_text(json.dumps(provider_value), encoding="utf-8")
    provider.chmod(0o600)
    lifecycle = tmp_path / "lifecycle.json"
    lifecycle_value = {
        "status": "PASS",
        "release_sha": release,
        "change_set_state": "AWAITING_STAGING_APPROVAL",
        "workflow_worker_restarted": True,
        "authenticated_api_path": True,
        "maker_provider": "codex-synthetic",
        "checker_provider": "claude-synthetic",
        "redaction_status": "SCANNED_CLEAN",
        "evidence_path": str(artifact),
        "evidence_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }
    lifecycle.write_text(json.dumps(lifecycle_value), encoding="utf-8")
    lifecycle.chmod(0o600)

    MODULE._validate_pre_cutover_evidence(
        provider,
        lifecycle,
        release,
        agent_pack,
        provider_root=tmp_path,
        lifecycle_root=tmp_path,
    )
    provider_value["directions"] = {"codex_to_claude": "PASS"}
    provider.write_text(json.dumps(provider_value), encoding="utf-8")
    with pytest.raises(ValueError, match="native replacement matrix"):
        MODULE._validate_pre_cutover_evidence(
            provider,
            lifecycle,
            release,
            agent_pack,
            provider_root=tmp_path,
            lifecycle_root=tmp_path,
        )
