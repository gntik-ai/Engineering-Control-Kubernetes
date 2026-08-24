from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "verify-no-omniroute.py"
SPEC = importlib.util.spec_from_file_location("verify_no_omniroute", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _write(root: Path, relative: str, value: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")
    target.chmod(0o600)
    return target


def test_clean_fixture_passes(tmp_path: Path) -> None:
    _write(tmp_path, "home/hermes/.hermes/config.yaml", "model: native-codex\n")

    result = MODULE.Validator("controller", tmp_path).validate()

    assert result["status"] == "PASS"
    assert result["findings"] == []


def test_active_config_fails_without_persisting_value(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "home/hermes/.hermes/.env",
        "OMNIROUTE_API_KEY=synthetic-value-that-must-not-appear\n",
    )

    result = MODULE.Validator("controller", tmp_path).validate()

    assert result["status"] == "FAIL"
    rendered = str(result)
    assert "synthetic-value-that-must-not-appear" not in rendered
    assert result["findings"][0]["target"].endswith(".env")


def test_reports_backups_and_evidence_are_excluded(tmp_path: Path) -> None:
    _write(tmp_path, "home/hermes/engineering/reports/old/report.json", "port 20128\n")
    _write(tmp_path, "home/hermes/engineering-backups/old/state.txt", "omniroute\n")
    _write(tmp_path, "srv/engineering/evidence/old.json", "omni.musematic.ai\n")

    result = MODULE.Validator("controller", tmp_path).validate()

    assert result["status"] == "PASS"


def test_active_residual_path_fails(tmp_path: Path) -> None:
    (tmp_path / "home/hermes/.omniroute").mkdir(parents=True)

    result = MODULE.Validator("controller", tmp_path).validate()

    assert result["status"] == "FAIL"
    assert any(value["target"] == "/home/hermes/.omniroute" for value in result["findings"])


def test_worker_legacy_hermes_assets_are_not_active(tmp_path: Path) -> None:
    _write(tmp_path, "home/hermes/.hermes/skills/old/SKILL.md", "use OmniRoute\n")

    result = MODULE.Validator("worker", tmp_path).validate()

    assert result["status"] == "PASS"


def test_worker_hermes_installation_activates_policy_scan(tmp_path: Path) -> None:
    binary = _write(tmp_path, "home/hermes/.local/bin/hermes", "#!/bin/sh\n")
    binary.chmod(0o700)
    _write(tmp_path, "home/hermes/.hermes/config.yaml", "provider: custom:omniroute\n")

    result = MODULE.Validator("worker", tmp_path).validate()

    assert result["status"] == "FAIL"
    assert any(value["target"].endswith("config.yaml") for value in result["findings"])
