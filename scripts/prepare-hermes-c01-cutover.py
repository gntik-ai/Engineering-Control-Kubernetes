#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from engineering_control.validation.c01 import (
    FULL_SHA,
    digest,
    validate_lifecycle,
    validate_provider,
)

OMNI_DEPENDENCY = re.compile(
    r"omniroute|omni\.musematic\.ai|(?:127\.0\.0\.1|localhost):201(?:28|31|32)",
    re.IGNORECASE,
)
CONFIRMATION = "APPLY NATIVE HERMES CONFIG CUTOVER"
ACTIVE_CONFIG = Path("/home/hermes/.hermes/config.yaml")
ACTIVE_PROFILE_ROOT = Path("/home/hermes/.hermes/project-profiles")
ACTIVE_QUALITY_SKILL = Path("/home/hermes/.hermes/skills/quality-cross/SKILL.md")
BACKUP_ROOT = Path("/home/hermes/engineering-backups")
PROVIDER_EVIDENCE_ROOT = Path("/home/hermes/engineering-bootstrap/provider-acceptance")
LIFECYCLE_EVIDENCE_ROOT = Path("/home/hermes/engineering-bootstrap/acceptance-runs")
ALIAS_MODELS = {
    "coding": "gpt-5.6-sol",
    "cheap": "gpt-5.6-luna",
    "smart": "gpt-5.6-terra",
}
QUALITY_SKILL_OMNI_TEXT = (
    "Prefer a separate delegated subagent/model route from the implementer when available "
    "through Hermes/OmniRoute."
)
QUALITY_SKILL_NATIVE_TEXT = (
    "Prefer an independent checker assigned by Engineering Control, separate from the implementer."
)
DIRECT_SECRET_TEXT = re.compile(
    rb"(?i)(?:token|password|secret|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]+"
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


def _has_direct_secret(config: dict[str, Any]) -> bool:
    sensitive = {
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
                if str(key).lower() in sensitive and isinstance(nested, str):
                    candidate = nested.strip()
                    if candidate and not re.fullmatch(r"[A-Z][A-Z0-9_]*", candidate):
                        return True
                if visit(nested):
                    return True
        if isinstance(value, list):
            return any(visit(nested) for nested in value)
        return False

    return visit(config)


def transform(config: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    candidate = copy.deepcopy(config)
    changes: list[str] = []
    model = candidate.setdefault("model", {})
    if not isinstance(model, dict):
        raise ValueError("Hermes model configuration must be a mapping")
    for key in ("api_mode", "base_url", "api_key", "key_env"):
        if key in model:
            model.pop(key)
            changes.append(f"model.{key}:removed")
    if model.get("provider") != "openai-codex":
        model["provider"] = "openai-codex"
        changes.append("model.provider:openai-codex")
    if model.get("default") != ALIAS_MODELS["smart"]:
        model["default"] = ALIAS_MODELS["smart"]
        changes.append("model.default:subscription-model")

    providers = candidate.get("providers")
    if isinstance(providers, dict):
        for key in tuple(providers):
            if str(key).lower() == "omniroute" or any(
                OMNI_DEPENDENCY.search(item) for item in _strings(providers[key])
            ):
                providers.pop(key)
                changes.append(f"providers.{key}:removed")
        if not providers:
            candidate.pop("providers")

    custom = candidate.get("custom_providers")
    if isinstance(custom, list):
        retained = [
            item
            for item in custom
            if not any(OMNI_DEPENDENCY.search(value) for value in _strings(item))
        ]
        if len(retained) != len(custom):
            changes.append("custom_providers.omniroute:removed")
        if retained:
            candidate["custom_providers"] = retained
        else:
            candidate.pop("custom_providers")

    aliases = candidate.setdefault("model_aliases", {})
    if not isinstance(aliases, dict):
        raise ValueError("Hermes model aliases must be a mapping")
    for name, model_name in ALIAS_MODELS.items():
        alias = aliases.setdefault(name, {})
        if not isinstance(alias, dict):
            raise ValueError(f"Hermes model alias {name} must be a mapping")
        alias_changed = False
        for key in ("api_mode", "base_url", "api_key", "key_env"):
            if key in alias:
                alias.pop(key)
                alias_changed = True
        if alias.get("provider") != "openai-codex":
            alias["provider"] = "openai-codex"
            alias_changed = True
        if alias.get("model") != model_name:
            alias["model"] = model_name
            alias_changed = True
        if alias_changed:
            changes.append(f"model_aliases.{name}:openai-codex")

    delegation = candidate.get("delegation")
    if isinstance(delegation, dict):
        delegation_changed = False
        for key in ("api_mode", "base_url", "api_key", "key_env", "provider"):
            if key in delegation:
                delegation.pop(key)
                delegation_changed = True
        if delegation.get("model") != ALIAS_MODELS["cheap"]:
            delegation["model"] = ALIAS_MODELS["cheap"]
            delegation_changed = True
        if delegation_changed:
            changes.append("delegation:subscription-model")

    if any(OMNI_DEPENDENCY.search(value) for value in _strings(candidate)):
        raise ValueError("transformed Hermes configuration still contains an OmniRoute dependency")
    return candidate, tuple(changes)


def transform_profile(config: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    candidate = copy.deepcopy(config)
    changes: list[str] = []
    policy = candidate.get("model_policy")
    if isinstance(policy, dict):
        provider = policy.get("provider")
        if isinstance(provider, str) and OMNI_DEPENDENCY.search(provider):
            policy["provider"] = "openai-codex"
            changes.append("model_policy.provider:openai-codex")
    if any(OMNI_DEPENDENCY.search(value) for value in _strings(candidate)):
        raise ValueError(
            "transformed Hermes project profile still contains an OmniRoute dependency"
        )
    return candidate, tuple(changes)


def transform_runtime_asset(content: bytes) -> tuple[bytes, tuple[str, ...]]:
    if DIRECT_SECRET_TEXT.search(content):
        raise ValueError("Hermes runtime asset appears to contain an inline credential")
    text = content.decode("utf-8")
    if not OMNI_DEPENDENCY.search(text):
        return content, ()
    if text.count(QUALITY_SKILL_OMNI_TEXT) != 1:
        raise ValueError("Hermes runtime asset has an unreviewed OmniRoute reference")
    candidate = text.replace(QUALITY_SKILL_OMNI_TEXT, QUALITY_SKILL_NATIVE_TEXT)
    if OMNI_DEPENDENCY.search(candidate):
        raise ValueError("transformed Hermes runtime asset still contains OmniRoute")
    return candidate.encode(), ("quality-cross:engineering-control-checker",)


def _profiles(profile_root: Path) -> tuple[Path, ...]:
    if not profile_root.exists():
        return ()
    if profile_root.is_symlink() or not profile_root.is_dir():
        raise ValueError("Hermes project profile root must be a real directory")
    profiles: list[Path] = []
    for path in sorted(profile_root.glob("*.yaml")):
        metadata = path.stat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise PermissionError(f"Hermes runtime profile is unsafe: {path.name}")
        profiles.append(path)
    return tuple(profiles)


def _atomic_yaml(path: Path, value: dict[str, Any], mode: int) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.c01-cutover.",
        delete=False,
    ) as handle:
        yaml.safe_dump(value, handle, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(mode)
    os.replace(temporary, path)


def _atomic_bytes(path: Path, value: bytes, mode: int) -> None:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.c01-restore.",
        delete=False,
    ) as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(mode)
    os.replace(temporary, path)


def _read_owned(path: Path, maximum: int) -> tuple[bytes, int]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or metadata.st_size > maximum
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise PermissionError(f"Hermes runtime input is unsafe: {path.name}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise PermissionError(f"Hermes runtime input changed during validation: {path.name}")
        value = os.read(descriptor, maximum + 1)
        if len(value) > maximum or os.read(descriptor, 1):
            raise ValueError(f"Hermes runtime input exceeded its size bound: {path.name}")
        return value, stat.S_IMODE(metadata.st_mode)
    finally:
        os.close(descriptor)


def _provider_ready() -> bool:
    result = subprocess.run(  # noqa: S603 - fixed local executable and arguments
        ["/home/hermes/.local/bin/hermes", "auth", "status", "openai-codex"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        env={"HOME": "/home/hermes", "PATH": "/home/hermes/.local/bin:/usr/bin:/bin"},
    )
    return result.returncode == 0


def _load_evidence(path: Path, root: Path, label: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"{label} escaped its allowlisted evidence root")
    metadata = resolved.stat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > 2 * 1024 * 1024
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO)
    ):
        raise PermissionError(f"{label} ownership, mode, link count, or size is unsafe")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not a JSON object")
    return value


def _validate_pre_cutover_evidence(
    provider_path: Path,
    lifecycle_path: Path,
    expected_release: str | None = None,
    expected_agent_pack: str | None = None,
    provider_root: Path = PROVIDER_EVIDENCE_ROOT,
    lifecycle_root: Path = LIFECYCLE_EVIDENCE_ROOT,
) -> None:
    release = expected_release or Path("/opt/engineering-control/source").resolve().parent.name
    agent_pack = (
        expected_agent_pack or Path("/opt/engineering-agent-pack/source").resolve().parent.name
    )
    if not FULL_SHA.fullmatch(release) or not FULL_SHA.fullmatch(agent_pack):
        raise ValueError("installed release identities are not full Git SHAs")

    provider = _load_evidence(provider_path, provider_root, "provider evidence")
    validate_provider(provider, release, agent_pack)

    lifecycle = _load_evidence(lifecycle_path, lifecycle_root, "lifecycle evidence")
    validate_lifecycle(lifecycle, release, lifecycle_root)


def apply(
    config_path: Path,
    backup_root: Path,
    provider_evidence: Path,
    lifecycle_evidence: Path,
    confirmation: str,
) -> Path:
    if confirmation != CONFIRMATION:
        raise ValueError(f"exact confirmation required: {CONFIRMATION}")
    if config_path.resolve() != ACTIVE_CONFIG.resolve():
        raise ValueError("apply is restricted to the canonical Hermes configuration")
    if backup_root.resolve() != BACKUP_ROOT.resolve():
        raise ValueError("backup root is outside the canonical engineering backup root")
    profile_root = config_path.parent / "project-profiles"
    if profile_root.resolve() != ACTIVE_PROFILE_ROOT.resolve():
        raise ValueError("profile root is outside the canonical Hermes configuration")
    runtime_asset = config_path.parent / "skills/quality-cross/SKILL.md"
    if runtime_asset.resolve() != ACTIVE_QUALITY_SKILL.resolve():
        raise ValueError("runtime asset is outside the canonical Hermes configuration")
    _validate_pre_cutover_evidence(provider_evidence, lifecycle_evidence)
    if not _provider_ready():
        raise RuntimeError("Hermes openai-codex subscription authentication is not healthy")
    original, config_mode = _read_owned(config_path, 2 * 1024 * 1024)
    parsed = yaml.safe_load(original)
    if not isinstance(parsed, dict):
        raise ValueError("Hermes configuration must be a mapping")
    if _has_direct_secret(parsed):
        raise ValueError("Hermes YAML contains a direct secret; automatic backup refused")
    candidate, _ = transform(parsed)
    profile_candidates: list[tuple[Path, bytes, dict[str, Any], int]] = []
    for profile in _profiles(profile_root):
        profile_original = profile.read_bytes()
        profile_parsed = yaml.safe_load(profile_original)
        if not isinstance(profile_parsed, dict):
            raise ValueError(f"Hermes runtime profile is not a mapping: {profile.name}")
        if _has_direct_secret(profile_parsed):
            raise ValueError(
                "Hermes runtime profile contains a direct secret; automatic backup refused: "
                f"{profile.name}"
            )
        profile_candidate, changes = transform_profile(profile_parsed)
        if changes:
            profile_candidates.append(
                (
                    profile,
                    profile_original,
                    profile_candidate,
                    stat.S_IMODE(profile.stat().st_mode),
                )
            )
    runtime_original = runtime_asset.read_bytes()
    runtime_metadata = runtime_asset.lstat()
    if (
        not stat.S_ISREG(runtime_metadata.st_mode)
        or runtime_metadata.st_uid != os.getuid()
        or runtime_metadata.st_nlink != 1
        or runtime_metadata.st_size > 2 * 1024 * 1024
        or runtime_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise PermissionError("Hermes quality-cross runtime asset is unsafe")
    runtime_candidate, runtime_changes = transform_runtime_asset(runtime_original)
    runtime_mode = stat.S_IMODE(runtime_metadata.st_mode)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_directory = backup_root / f"hermes-native-cutover-{stamp}"
    backup_directory.mkdir(parents=True, mode=0o700)
    backup = backup_directory / "config.pre-native-cutover.yaml"
    backup.write_bytes(original)
    backup.chmod(0o600)
    if backup.read_bytes() != original:
        raise OSError("Hermes configuration backup readability check failed")
    original_digest = digest(backup)
    profile_manifest: list[str] = []
    profile_backup_root = backup_directory / "project-profiles"
    if profile_candidates:
        profile_backup_root.mkdir(mode=0o700)
    for profile, profile_original, _, _ in profile_candidates:
        profile_backup = profile_backup_root / profile.name
        profile_backup.write_bytes(profile_original)
        profile_backup.chmod(0o600)
        if profile_backup.read_bytes() != profile_original:
            raise OSError(f"Hermes profile backup readability check failed: {profile.name}")
        profile_mode = next(mode for path, _, _, mode in profile_candidates if path == profile)
        profile_manifest.append(
            f"- `{profile}` mode: `{profile_mode:04o}` SHA256: `{digest(profile_backup)}`"
        )
    runtime_manifest_text = "- Runtime text assets changed: none\n"
    runtime_backup: Path | None = None
    if runtime_changes:
        runtime_backup = backup_directory / "runtime-assets/skills/quality-cross/SKILL.md"
        runtime_backup.parent.mkdir(parents=True, mode=0o700)
        runtime_backup.write_bytes(runtime_original)
        runtime_backup.chmod(0o600)
        if runtime_backup.read_bytes() != runtime_original:
            raise OSError("Hermes runtime asset backup readability check failed")
        runtime_manifest_text = (
            f"- `{runtime_asset}` mode: `{runtime_mode:04o}` SHA256: `{digest(runtime_backup)}`\n"
        )
    profile_manifest_text = (
        "- Runtime project profiles:\n" + "\n".join(profile_manifest) + "\n"
        if profile_manifest
        else "- Runtime project profiles changed: none\n"
    )
    manifest = backup_directory / "BACKUP-MANIFEST.md"
    manifest.write_text(
        "# Hermes native-provider configuration backup\n\n"
        f"- Created: {datetime.now(UTC).isoformat()}\n"
        f"- Source: `{config_path}`\n"
        f"- Source mode: `{config_mode:04o}`\n"
        f"- SHA256: `{original_digest}`\n"
        f"{profile_manifest_text}"
        f"{runtime_manifest_text}"
        "- Direct secret values: none detected\n"
        "- Restore: copy `config.pre-native-cutover.yaml` to the source path and each "
        "protected `project-profiles/*.yaml` and `runtime-assets/*` file to its matching "
        "runtime path, restore the recorded modes, then restart `hermes-gateway.service`. "
        "OmniRoute itself "
        "remains installed and available for rollback.\n",
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    if not manifest.read_text(encoding="utf-8").startswith(
        "# Hermes native-provider configuration backup"
    ):
        raise OSError("Hermes backup manifest readability check failed")
    restore = backup_directory / "RESTORE.md"
    restore.write_text(
        "# Restore Hermes runtime configuration\n\n"
        "Restore `config.pre-native-cutover.yaml`, every protected file beneath "
        "`project-profiles/` and `runtime-assets/` to the source path recorded in "
        "`BACKUP-MANIFEST.md`, apply each recorded mode, then restart "
        "`hermes-gateway.service`. Do not delete or copy `.omniroute` during rollback.\n",
        encoding="utf-8",
    )
    restore.chmod(0o600)
    if not restore.read_text(encoding="utf-8").startswith("# Restore Hermes"):
        raise OSError("Hermes restore procedure readability check failed")
    updated: list[tuple[Path, bytes, int]] = []
    try:
        _atomic_yaml(config_path, candidate, config_mode)
        updated.append((config_path, original, config_mode))
        for profile, profile_original, profile_candidate, mode in profile_candidates:
            _atomic_yaml(profile, profile_candidate, mode)
            updated.append((profile, profile_original, mode))
        if runtime_changes:
            _atomic_bytes(runtime_asset, runtime_candidate, runtime_mode)
            updated.append((runtime_asset, runtime_original, runtime_mode))
    except Exception as mutation_error:
        rollback_errors: list[str] = []
        for path, prior, mode in reversed(updated):
            try:
                _atomic_bytes(path, prior, mode)
            except Exception:
                rollback_errors.append(path.name)
        if rollback_errors:
            raise RuntimeError(
                "Hermes profile cutover failed and compensation was incomplete for: "
                + ", ".join(rollback_errors)
            ) from mutation_error
        raise
    return backup_directory


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ACTIVE_CONFIG)
    parser.add_argument("--backup-root", type=Path, default=BACKUP_ROOT)
    parser.add_argument("--provider-evidence", type=Path)
    parser.add_argument("--lifecycle-evidence", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirmation", default="")
    arguments = parser.parse_args()
    if arguments.check == arguments.apply:
        raise SystemExit("choose exactly one of --check or --apply")
    parsed = yaml.safe_load(arguments.config.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise SystemExit("Hermes configuration must be a mapping")
    _, config_changes = transform(parsed)
    profile_changes = 0
    for profile in _profiles(arguments.config.parent / "project-profiles"):
        profile_parsed = yaml.safe_load(profile.read_text(encoding="utf-8"))
        if not isinstance(profile_parsed, dict):
            raise SystemExit(f"Hermes runtime profile is not a mapping: {profile.name}")
        _, changes = transform_profile(profile_parsed)
        profile_changes += len(changes)
    runtime_asset = arguments.config.parent / "skills/quality-cross/SKILL.md"
    _, runtime_changes = transform_runtime_asset(runtime_asset.read_bytes())
    if arguments.check:
        print(
            "PASS Hermes C-01 candidate can remove OmniRoute dependencies; "
            f"config_changes={len(config_changes)} profile_changes={profile_changes} "
            f"runtime_asset_changes={len(runtime_changes)}"
        )
        return
    if arguments.provider_evidence is None or arguments.lifecycle_evidence is None:
        raise SystemExit("--apply requires --provider-evidence and --lifecycle-evidence")
    backup = apply(
        arguments.config,
        arguments.backup_root,
        arguments.provider_evidence,
        arguments.lifecycle_evidence,
        arguments.confirmation,
    )
    print(f"PASS Hermes native-provider configuration applied; backup={backup}")


if __name__ == "__main__":
    main()
