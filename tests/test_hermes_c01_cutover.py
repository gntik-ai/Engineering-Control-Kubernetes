from __future__ import annotations

import importlib.util
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
        MODULE.apply(tmp_path / "config.yaml", tmp_path, tmp_path / "gates.json", "wrong")
