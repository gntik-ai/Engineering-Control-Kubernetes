#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ENGINEERING_COMMANDS = frozenset(
    {
        "audit",
        "audit-plan",
        "bootstrap-plan",
        "bootstrap",
        "campaigns",
        "campaign",
        "ready",
        "connections",
        "temporal",
        "providers",
        "workers",
        "channel-check",
        "findings",
        "loop",
        "enhancements",
        "run",
        "status",
        "evidence",
        "approve",
        "reject",
        "pause",
        "resume",
        "cancel",
        "usage",
        "verify",
        "validate",
        "rollback",
    }
)
OMNI_DEPENDENCY = re.compile(
    r"omniroute|omni\.musematic\.ai|(?<!\d)201(?:28|31|32)(?!\d)",
    re.IGNORECASE,
)
NO_ADMIN = ["engineering-control-no-admin"]
PLUGIN_NAME = "engineering-control"
RUNTIME_ASSET_SUFFIXES = {".json", ".md", ".py", ".sh", ".yaml", ".yml"}
RUNTIME_METADATA = (
    ".env",
    "auth.json",
    "context_length_cache.yaml",
    "provider_models_cache.json",
)


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


def _runtime_profiles(hermes_home: Path) -> tuple[Path, ...]:
    profile_root = hermes_home / "project-profiles"
    if not profile_root.exists():
        return ()
    if profile_root.is_symlink() or not profile_root.is_dir():
        raise ValueError("Hermes project profile root must be a real directory")
    profiles: list[Path] = []
    for path in sorted(profile_root.glob("*.yaml")):
        metadata = path.stat()
        if (
            path.is_symlink()
            or not path.is_file()
            or metadata.st_nlink != 1
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError(f"Hermes runtime profile is unsafe: {path.name}")
        profiles.append(path)
    return tuple(profiles)


def _runtime_assets(hermes_home: Path) -> tuple[Path, ...]:
    assets: list[Path] = []
    for name in ("commands", "hooks", "plugins", "skills", "tools"):
        root = hermes_home / name
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise ValueError(f"Hermes runtime asset root must be a real directory: {name}")
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            if path.suffix.lower() not in RUNTIME_ASSET_SUFFIXES or not path.is_file():
                continue
            metadata = path.stat()
            if (
                path.is_symlink()
                or metadata.st_nlink != 1
                or metadata.st_size > 2 * 1024 * 1024
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise ValueError(f"Hermes runtime asset is unsafe: {path.relative_to(hermes_home)}")
            assets.append(path)
    return tuple(assets)


def _runtime_metadata(hermes_home: Path) -> tuple[Path, ...]:
    documents: list[Path] = []
    for name in RUNTIME_METADATA:
        path = hermes_home / name
        if not path.exists():
            continue
        metadata = path.stat()
        if (
            path.is_symlink()
            or not path.is_file()
            or metadata.st_nlink != 1
            or metadata.st_size > 2 * 1024 * 1024
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError(f"Hermes runtime metadata is unsafe: {name}")
        documents.append(path)
    return tuple(documents)


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
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        raise ValueError("Hermes plugin configuration must be a mapping")
    enabled = plugins.get("enabled")
    disabled = plugins.get("disabled", [])
    if not isinstance(enabled, list) or PLUGIN_NAME not in enabled:
        raise ValueError("Engineering Control plugin must be explicitly enabled")
    if not isinstance(disabled, list) or PLUGIN_NAME in disabled:
        raise ValueError("Engineering Control plugin must not be disabled")
    plugin = hermes_home / "plugins" / "engineering-control" / "plugin.yaml"
    plugin_module = hermes_home / "plugins" / "engineering-control" / "__init__.py"
    hook = hermes_home / "hooks" / "engineering-control" / "HOOK.yaml"
    handler = hermes_home / "hooks" / "engineering-control" / "handler.py"
    if not all(path.is_file() for path in (plugin, plugin_module, hook, handler)):
        raise ValueError("Engineering Control Hermes plugin or hook is missing")
    if require_no_omniroute:
        runtime_documents: list[tuple[str, Any]] = [(config_path.name, config)]
        for profile in _runtime_profiles(hermes_home):
            parsed = yaml.safe_load(profile.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError(f"Hermes runtime profile is not a mapping: {profile.name}")
            runtime_documents.append((f"project-profiles/{profile.name}", parsed))
        offenders = [
            label
            for label, document in runtime_documents
            if any(OMNI_DEPENDENCY.search(item) for item in _strings(document))
        ]
        offenders.extend(
            str(path.relative_to(hermes_home))
            for path in _runtime_assets(hermes_home)
            if OMNI_DEPENDENCY.search(path.read_text(encoding="utf-8"))
        )
        offenders.extend(
            path.name
            for path in _runtime_metadata(hermes_home)
            if OMNI_DEPENDENCY.search(path.read_text(encoding="utf-8"))
        )
        if offenders:
            raise ValueError(
                "Hermes runtime configuration still contains an OmniRoute dependency: "
                + ", ".join(offenders)
            )


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
    plugins = config.get("plugins")
    if plugins is None:
        plugins = {}
        config["plugins"] = plugins
    if not isinstance(plugins, dict):
        raise ValueError("Hermes plugin configuration must be a mapping")
    enabled = plugins.get("enabled", [])
    disabled = plugins.get("disabled", [])
    if not isinstance(enabled, list) or not isinstance(disabled, list):
        raise ValueError("Hermes plugin enable/disable lists must be arrays")
    expected_commands = sorted(ENGINEERING_COMMANDS)
    compliant = (
        toolsets.get("telegram") == []
        and telegram.get("allow_admin_from") == NO_ADMIN
        and telegram.get("group_allow_admin_from") == NO_ADMIN
        and telegram.get("user_allowed_commands") == expected_commands
        and telegram.get("group_user_allowed_commands") == expected_commands
        and PLUGIN_NAME in enabled
        and PLUGIN_NAME not in disabled
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
    plugins["enabled"] = sorted({str(value) for value in enabled} | {PLUGIN_NAME})
    if disabled:
        plugins["disabled"] = [value for value in disabled if value != PLUGIN_NAME]
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
