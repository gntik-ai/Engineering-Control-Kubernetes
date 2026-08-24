#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
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

OMNI_DEPENDENCY = re.compile(
    r"omniroute|omni\.musematic\.ai|(?:127\.0\.0\.1|localhost):201(?:28|31|32)",
    re.IGNORECASE,
)
CONFIRMATION = "APPLY NATIVE HERMES CONFIG CUTOVER"
ACTIVE_CONFIG = Path("/home/hermes/.hermes/config.yaml")
BACKUP_ROOT = Path("/home/hermes/engineering-backups")
PROVIDER_EVIDENCE_ROOT = Path("/home/hermes/engineering-bootstrap/provider-acceptance")
LIFECYCLE_EVIDENCE_ROOT = Path("/home/hermes/engineering-bootstrap/acceptance-runs")
FULL_SHA = re.compile(r"^[a-f0-9]{40}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
REQUIRED_PROVIDER_RUNS = {
    "codex_maker": ("codex", "implementer"),
    "claude_checker": ("claude", "code-reviewer"),
    "claude_maker": ("claude", "implementer"),
    "codex_checker": ("codex", "code-reviewer"),
}
ALIAS_MODELS = {
    "coding": "gpt-5.6-sol",
    "cheap": "gpt-5.6-luna",
    "smart": "gpt-5.6-terra",
}


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
        for key in ("api_mode", "base_url", "api_key", "key_env"):
            alias.pop(key, None)
        alias["provider"] = "openai-codex"
        alias["model"] = model_name
        changes.append(f"model_aliases.{name}:openai-codex")

    delegation = candidate.get("delegation")
    if isinstance(delegation, dict):
        for key in ("api_mode", "base_url", "api_key", "key_env", "provider"):
            delegation.pop(key, None)
        delegation["model"] = ALIAS_MODELS["cheap"]
        changes.append("delegation:subscription-model")

    if any(OMNI_DEPENDENCY.search(value) for value in _strings(candidate)):
        raise ValueError("transformed Hermes configuration still contains an OmniRoute dependency")
    return candidate, tuple(changes)


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


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    if (
        provider.get("status") != "PASS"
        or provider.get("release_sha") != release
        or provider.get("redaction_status") != "REDACTED"
        or provider.get("authentication_mode") != "native-subscription-cli"
        or provider.get("paid_api_fallback") is not False
        or provider.get("directions") != {"codex_to_claude": "PASS", "claude_to_codex": "PASS"}
    ):
        raise ValueError("provider evidence does not prove the native replacement matrix")
    runs = provider.get("provider_runs")
    if not isinstance(runs, dict) or set(runs) != set(REQUIRED_PROVIDER_RUNS):
        raise ValueError("provider evidence has an incomplete invocation matrix")
    for key, (provider_name, role) in REQUIRED_PROVIDER_RUNS.items():
        run = runs[key]
        if (
            not isinstance(run, dict)
            or run.get("provider") != provider_name
            or run.get("role") != role
            or run.get("status") != "SUCCESS"
            or run.get("agent_pack_revision") != agent_pack
            or not DIGEST.fullmatch(str(run.get("instruction_bundle_sha256", "")))
        ):
            raise ValueError(f"provider evidence run {key} is invalid")

    lifecycle = _load_evidence(lifecycle_path, lifecycle_root, "lifecycle evidence")
    if (
        lifecycle.get("status") != "PASS"
        or lifecycle.get("release_sha") != release
        or lifecycle.get("change_set_state") != "AWAITING_STAGING_APPROVAL"
        or lifecycle.get("workflow_worker_restarted") is not True
        or lifecycle.get("authenticated_api_path") is not True
        or lifecycle.get("maker_provider") == lifecycle.get("checker_provider")
        or lifecycle.get("redaction_status") not in {"REDACTED", "SCANNED_CLEAN"}
        or not DIGEST.fullmatch(str(lifecycle.get("evidence_sha256", "")))
    ):
        raise ValueError("lifecycle evidence does not prove restart/resume and persistence")
    lifecycle_artifact = Path(str(lifecycle.get("evidence_path", ""))).resolve()
    if (
        not lifecycle_artifact.is_file()
        or not lifecycle_artifact.is_relative_to(lifecycle_root.resolve())
        or _digest(lifecycle_artifact) != lifecycle["evidence_sha256"]
    ):
        raise ValueError("lifecycle artifact path or digest does not match")


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
    _validate_pre_cutover_evidence(provider_evidence, lifecycle_evidence)
    if not _provider_ready():
        raise RuntimeError("Hermes openai-codex subscription authentication is not healthy")
    original = config_path.read_bytes()
    parsed = yaml.safe_load(original)
    if not isinstance(parsed, dict):
        raise ValueError("Hermes configuration must be a mapping")
    if _has_direct_secret(parsed):
        raise ValueError("Hermes YAML contains a direct secret; automatic backup refused")
    candidate, _ = transform(parsed)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_directory = backup_root / f"hermes-native-cutover-{stamp}"
    backup_directory.mkdir(parents=True, mode=0o700)
    backup = backup_directory / "config.pre-native-cutover.yaml"
    backup.write_bytes(original)
    backup.chmod(0o600)
    digest = hashlib.sha256(original).hexdigest()
    manifest = backup_directory / "BACKUP-MANIFEST.md"
    manifest.write_text(
        "# Hermes native-provider configuration backup\n\n"
        f"- Created: {datetime.now(UTC).isoformat()}\n"
        f"- Source: `{config_path}`\n"
        f"- SHA256: `{digest}`\n"
        "- Direct secret values: none detected\n"
        "- Restore: copy the protected YAML back, mode 0600, then restart "
        "`hermes-gateway.service`. OmniRoute itself remains installed and available for "
        "rollback.\n",
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=config_path.parent,
        prefix=".config.c01-cutover.",
        delete=False,
    ) as handle:
        yaml.safe_dump(candidate, handle, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.replace(temporary, config_path)
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
    _, changes = transform(parsed)
    if arguments.check:
        print(
            f"PASS Hermes C-01 candidate can remove OmniRoute dependencies; changes={len(changes)}"
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
