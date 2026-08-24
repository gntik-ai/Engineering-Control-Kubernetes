from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "verify-hermes-policy.py"
SPEC = importlib.util.spec_from_file_location("verify_hermes_policy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    home = tmp_path / "hermes"
    for relative in (
        "plugins/engineering-control/plugin.yaml",
        "plugins/engineering-control/__init__.py",
        "hooks/engineering-control/HOOK.yaml",
        "hooks/engineering-control/handler.py",
    ):
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n", encoding="utf-8")
    config: dict[str, object] = {
        "plugins": {"enabled": [MODULE.PLUGIN_NAME]},
        "platform_toolsets": {"telegram": []},
        "platforms": {
            "telegram": {
                "allow_admin_from": ["engineering-control-no-admin"],
                "group_allow_admin_from": ["engineering-control-no-admin"],
                "user_allowed_commands": sorted(MODULE.ENGINEERING_COMMANDS),
                "group_user_allowed_commands": sorted(MODULE.ENGINEERING_COMMANDS),
            }
        },
    }
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path, home, config


def test_policy_accepts_narrow_control_only_telegram_surface(tmp_path: Path) -> None:
    config, home, _ = _fixture(tmp_path)

    MODULE.verify(config, home, require_no_omniroute=True)


def test_policy_rejects_telegram_terminal_toolset(tmp_path: Path) -> None:
    config_path, home, config = _fixture(tmp_path)
    config["platform_toolsets"] = {"telegram": ["terminal"]}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(ValueError, match="toolsets"):
        MODULE.verify(config_path, home, require_no_omniroute=False)


def test_cutover_policy_rejects_omniroute_dependency(tmp_path: Path) -> None:
    config_path, home, config = _fixture(tmp_path)
    config["model"] = {"provider": "custom:omniroute"}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(ValueError, match="OmniRoute"):
        MODULE.verify(config_path, home, require_no_omniroute=True)


def test_cutover_policy_rejects_omniroute_project_profile(tmp_path: Path) -> None:
    config_path, home, _ = _fixture(tmp_path)
    profiles = home / "project-profiles"
    profiles.mkdir()
    profile = profiles / "falcone.yaml"
    profile.write_text(
        yaml.safe_dump({"model_policy": {"provider": "custom:omniroute"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="project-profiles/falcone.yaml"):
        MODULE.verify(config_path, home, require_no_omniroute=True)


def test_cutover_policy_rejects_omniroute_runtime_skill(tmp_path: Path) -> None:
    config_path, home, _ = _fixture(tmp_path)
    skill = home / "skills/quality-cross/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("route through Hermes/OmniRoute\n", encoding="utf-8")

    with pytest.raises(ValueError, match="skills/quality-cross/SKILL.md"):
        MODULE.verify(config_path, home, require_no_omniroute=True)


def test_cutover_policy_ignores_historical_hidden_skill_backup(tmp_path: Path) -> None:
    config_path, home, _ = _fixture(tmp_path)
    backup = home / "skills/.curator_backups/old/SKILL.md"
    backup.parent.mkdir(parents=True)
    backup.write_text("historical OmniRoute guidance\n", encoding="utf-8")

    MODULE.verify(config_path, home, require_no_omniroute=True)


def test_policy_rejects_disabled_engineering_plugin(tmp_path: Path) -> None:
    config_path, home, config = _fixture(tmp_path)
    config["plugins"] = {
        "enabled": [MODULE.PLUGIN_NAME],
        "disabled": [MODULE.PLUGIN_NAME],
    }
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(ValueError, match="must not be disabled"):
        MODULE.verify(config_path, home, require_no_omniroute=False)


def test_lockdown_is_backup_first_and_idempotent(tmp_path: Path) -> None:
    config_path, home, config = _fixture(tmp_path)
    config["platform_toolsets"] = {"telegram": ["terminal"]}
    config["plugins"] = None
    telegram = config["platforms"]["telegram"]  # type: ignore[index]
    telegram.pop("allow_admin_from")  # type: ignore[union-attr]
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    original = config_path.read_bytes()

    backup = MODULE.apply_telegram_lockdown(config_path, tmp_path / "backups")

    assert backup is not None
    assert (backup / "config.pre-telegram-lockdown.yaml").read_bytes() == original
    assert MODULE.apply_telegram_lockdown(config_path, tmp_path / "backups") is None
    MODULE.verify(config_path, home, require_no_omniroute=False)


def test_lockdown_refuses_to_duplicate_direct_secrets(tmp_path: Path) -> None:
    config_path, _, config = _fixture(tmp_path)
    config["platform_toolsets"] = {"telegram": ["terminal"]}
    config["api_key"] = "synthetic-direct-secret"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(ValueError, match="automatic backup refused"):
        MODULE.apply_telegram_lockdown(config_path, tmp_path / "backups")
