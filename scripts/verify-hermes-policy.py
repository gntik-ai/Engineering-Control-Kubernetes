#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ENGINEERING_COMMANDS = frozenset(
    {
        "audit",
        "campaigns",
        "findings",
        "run",
        "status",
        "evidence",
        "approve",
        "reject",
        "pause",
        "resume",
        "cancel",
        "usage",
        "validate",
        "rollback",
    }
)
OMNI_DEPENDENCY = re.compile(
    r"omniroute|omni\.musematic\.ai|(?:127\.0\.0\.1|localhost):201(?:28|31|32)",
    re.IGNORECASE,
)
NO_ADMIN = ["engineering-control-no-admin"]


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, nested in value.items():
            result.append(str(key))
            result.extend(_strings(nested))
        return result
    if isinstance(value, list):
        result = []
        for nested in value:
            result.extend(_strings(nested))
        return result
    return [str(value)]


def verify(config_path: Path, hermes_home: Path, require_no_omniroute: bool) -> None:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Hermes configuration must be a mapping")
    toolsets = config.get("platform_toolsets")
    if not isinstance(toolsets, dict) or toolsets.get("telegram") != []:
        raise ValueError("Telegram toolsets must be explicitly empty")
    platforms = config.get("platforms")
    telegram = platforms.get("telegram") if isinstance(platforms, dict) else None
    if not isinstance(telegram, dict):
        raise ValueError("Telegram slash-command policy is missing")
    for key in ("allow_admin_from", "group_allow_admin_from"):
        if telegram.get(key) != NO_ADMIN:
            raise ValueError(f"{key} must not grant unrestricted administrator access")
    for key in ("user_allowed_commands", "group_user_allowed_commands"):
        configured = telegram.get(key)
        if (
            not isinstance(configured, list)
            or len(configured) != len(ENGINEERING_COMMANDS)
            or set(configured) != ENGINEERING_COMMANDS
        ):
            raise ValueError(f"{key} must equal the Engineering Control command set")
    plugin = hermes_home / "plugins" / "engineering-control" / "plugin.yaml"
    hook = hermes_home / "hooks" / "engineering-control" / "HOOK.yaml"
    handler = hermes_home / "hooks" / "engineering-control" / "handler.py"
    if not all(path.is_file() for path in (plugin, hook, handler)):
        raise ValueError("Engineering Control Hermes plugin or hook is missing")
    if require_no_omniroute and any(OMNI_DEPENDENCY.search(item) for item in _strings(config)):
        raise ValueError("Hermes configuration still contains an OmniRoute runtime dependency")


def _has_direct_secret(config: dict[str, Any]) -> bool:
    sensitive_keys = {
        "api_key",
        "apikey",
        "password",
        "secret",
        "token",
        "bot_token",
        "access_token",
        "refresh_token",
    }

    def visit(value: Any) -> bool:
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in sensitive_keys and isinstance(nested, str):
                    candidate = nested.strip()
                    if candidate and not re.fullmatch(r"[A-Z][A-Z0-9_]*", candidate):
                        return True
                if visit(nested):
                    return True
        elif isinstance(value, list):
            return any(visit(nested) for nested in value)
        return False

    return visit(config)


def apply_telegram_lockdown(config_path: Path, backup_root: Path) -> Path | None:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Hermes configuration must be a mapping")
    toolsets = config.setdefault("platform_toolsets", {})
    platforms = config.setdefault("platforms", {})
    if not isinstance(toolsets, dict) or not isinstance(platforms, dict):
        raise ValueError("Hermes platform policy sections must be mappings")
    telegram = platforms.setdefault("telegram", {})
    if not isinstance(telegram, dict):
        raise ValueError("Hermes Telegram policy must be a mapping")
    expected_commands = sorted(ENGINEERING_COMMANDS)
    compliant = (
        toolsets.get("telegram") == []
        and telegram.get("allow_admin_from") == NO_ADMIN
        and telegram.get("group_allow_admin_from") == NO_ADMIN
        and telegram.get("user_allowed_commands") == expected_commands
        and telegram.get("group_user_allowed_commands") == expected_commands
    )
    if compliant:
        return None
    if _has_direct_secret(config):
        raise ValueError("Hermes YAML contains a direct secret; automatic backup refused")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_directory = backup_root / f"hermes-policy-{stamp}"
    backup_directory.mkdir(parents=True, mode=0o700)
    backup = backup_directory / "config.pre-telegram-lockdown.yaml"
    original = config_path.read_bytes()
    backup.write_bytes(original)
    backup.chmod(0o600)
    digest = hashlib.sha256(original).hexdigest()
    manifest = backup_directory / "BACKUP-MANIFEST.md"
    manifest.write_text(
        "# Hermes policy backup\n\n"
        f"- Created: {datetime.now(UTC).isoformat()}\n"
        f"- Source: `{config_path}`\n"
        f"- SHA256: `{digest}`\n"
        "- Direct secret values: none detected\n",
        encoding="utf-8",
    )
    manifest.chmod(0o600)

    toolsets["telegram"] = []
    telegram["allow_admin_from"] = list(NO_ADMIN)
    telegram["group_allow_admin_from"] = list(NO_ADMIN)
    telegram["user_allowed_commands"] = expected_commands
    telegram["group_user_allowed_commands"] = expected_commands
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=config_path.parent,
        prefix=".config.engineering-control.",
        delete=False,
    ) as handle:
        yaml.safe_dump(config, handle, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.replace(temporary, config_path)
    return backup_directory


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/home/hermes/.hermes/config.yaml"),
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path("/home/hermes/.hermes"),
    )
    parser.add_argument("--require-no-omniroute", action="store_true")
    parser.add_argument("--apply-telegram-lockdown", action="store_true")
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path("/home/hermes/engineering-backups"),
    )
    arguments = parser.parse_args()
    if arguments.apply_telegram_lockdown:
        backup = apply_telegram_lockdown(arguments.config, arguments.backup_root)
        print(
            "Hermes Telegram policy already compliant"
            if backup is None
            else f"Hermes Telegram policy updated; backup={backup}"
        )
    verify(arguments.config, arguments.hermes_home, arguments.require_no_omniroute)
    print("PASS Hermes Engineering Control policy")


if __name__ == "__main__":
    main()
