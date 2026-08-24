from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

FULL_SHA = re.compile(r"^[a-f0-9]{40}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
REQUIRED_PROVIDER_RUNS = {
    "codex_maker": ("codex", "implementer"),
    "claude_checker": ("claude", "code-reviewer"),
    "claude_maker": ("claude", "implementer"),
    "codex_checker": ("codex", "code-reviewer"),
}
REQUIRED_GATES = (
    "engineering_api",
    "temporal_restart_resume",
    "postgresql",
    "codex_structured_call",
    "claude_structured_call",
    "codex_maker_claude_checker",
    "claude_maker_codex_checker",
    "quota_broker",
    "no_paid_fallback",
    "hermes_api",
    "telegram_usage_status_validate",
    "persistence_evidence_redaction",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_provider(value: dict[str, Any], release_sha: str, agent_pack_sha: str) -> None:
    if (
        value.get("status") != "PASS"
        or value.get("release_sha") != release_sha
        or value.get("redaction_status") != "REDACTED"
        or value.get("authentication_mode") != "native-subscription-cli"
        or value.get("paid_api_fallback") is not False
        or value.get("directions") != {"codex_to_claude": "PASS", "claude_to_codex": "PASS"}
    ):
        raise ValueError("provider evidence does not prove the native replacement matrix")
    runs = value.get("provider_runs")
    if not isinstance(runs, dict) or set(runs) != set(REQUIRED_PROVIDER_RUNS):
        raise ValueError("provider evidence has an incomplete invocation matrix")
    for key, (provider, role) in REQUIRED_PROVIDER_RUNS.items():
        run = runs[key]
        if (
            not isinstance(run, dict)
            or run.get("provider") != provider
            or run.get("role") != role
            or run.get("status") != "SUCCESS"
            or run.get("agent_pack_revision") != agent_pack_sha
            or not DIGEST.fullmatch(str(run.get("instruction_bundle_sha256", "")))
        ):
            raise ValueError(f"provider evidence run {key} is invalid")


def validate_lifecycle(
    value: dict[str, Any],
    release_sha: str,
    evidence_root: Path = Path("/home/hermes/engineering-bootstrap/acceptance-runs"),
) -> None:
    if (
        value.get("status") != "PASS"
        or value.get("release_sha") != release_sha
        or value.get("change_set_state") != "AWAITING_STAGING_APPROVAL"
        or value.get("workflow_worker_restarted") is not True
        or value.get("authenticated_api_path") is not True
        or value.get("maker_provider") == value.get("checker_provider")
        or value.get("redaction_status") not in {"REDACTED", "SCANNED_CLEAN"}
        or not DIGEST.fullmatch(str(value.get("evidence_sha256", "")))
    ):
        raise ValueError("lifecycle evidence does not prove restart/resume and persistence")
    evidence_path = Path(str(value.get("evidence_path", ""))).resolve()
    if (
        not evidence_path.is_file()
        or not evidence_path.is_relative_to(evidence_root.resolve())
        or digest(evidence_path) != value["evidence_sha256"]
    ):
        raise ValueError("lifecycle artifact path or digest does not match")


def validate_telegram(value: dict[str, Any], release_sha: str) -> None:
    events = value.get("events")
    if (
        value.get("status") != "PASS"
        or value.get("release_sha") != release_sha
        or value.get("commands") != ["usage", "validate", "status"]
        or not isinstance(events, list)
        or len(events) != 3
        or any(not isinstance(event, dict) for event in events)
        or any(event.get("release_sha") != release_sha for event in events)
    ):
        raise ValueError("Telegram evidence is not a release-bound three-command sequence")


def validate_usage(value: dict[str, Any]) -> None:
    for provider in ("codex", "claude"):
        snapshot = value.get(provider)
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("status") != "AVAILABLE"
            or snapshot.get("authenticated") is not True
            or snapshot.get("cli_present") is not True
            or snapshot.get("rate_limited") is not False
        ):
            raise ValueError(f"quota snapshot for {provider} is not operational")
        for unknown_safe in ("remaining_capacity", "total_capacity", "reset_at"):
            if unknown_safe not in snapshot:
                raise ValueError(f"quota snapshot for {provider} omits {unknown_safe}")


def validate_c01_manifest(value: dict[str, Any], release_sha: str, agent_pack_sha: str) -> None:
    gates = value.get("gates")
    if (
        value.get("schema_version") != 1
        or value.get("status") != "PASS"
        or value.get("release_sha") != release_sha
        or value.get("agent_pack_sha") != agent_pack_sha
        or not isinstance(gates, dict)
        or any(gates.get(gate) != "PASS" for gate in REQUIRED_GATES)
    ):
        raise ValueError("C-01 acceptance manifest is incomplete or release-mismatched")
    for key in (
        "provider_evidence_sha256",
        "telegram_evidence_sha256",
        "lifecycle_evidence_sha256",
    ):
        if not DIGEST.fullmatch(str(value.get(key, ""))):
            raise ValueError(f"C-01 acceptance manifest {key} is invalid")


def parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{label} timestamp is not timezone-aware")
    return parsed


def validate_post_stop_freshness(
    provider: dict[str, Any],
    telegram: dict[str, Any],
    lifecycle: dict[str, Any],
    stopped_at: datetime,
) -> None:
    for label, value in (("provider", provider), ("lifecycle", lifecycle)):
        if parse_time(value.get("created_at"), label) <= stopped_at:
            raise ValueError(f"{label} evidence predates the OmniRoute stop")
    events = telegram.get("events")
    if not isinstance(events, list) or len(events) != 3:
        raise ValueError("Telegram evidence does not contain three events")
    if any(
        not isinstance(event, dict)
        or parse_time(event.get("recorded_at"), "Telegram event") <= stopped_at
        for event in events
    ):
        raise ValueError("Telegram evidence predates the OmniRoute stop")


def validate_post_stop_manifest(
    value: dict[str, Any],
    release_sha: str,
    agent_pack_sha: str,
    stopped_state_sha256: str,
) -> None:
    gates = value.get("gates")
    if (
        value.get("schema_version") != 1
        or value.get("status") != "PASS"
        or value.get("release_sha") != release_sha
        or value.get("agent_pack_sha") != agent_pack_sha
        or value.get("stopped_state_sha256") != stopped_state_sha256
        or not isinstance(gates, dict)
        or any(
            gates.get(gate) != "PASS"
            for gate in (
                "omniroute_stopped_disabled",
                "hermes_restarted",
                "worker_services_restarted",
                "provider_matrix_after_stop",
                "temporal_restart_resume_after_stop",
                "telegram_round_trip_after_stop",
                "runtime_health_after_stop",
            )
        )
    ):
        raise ValueError("post-stop C-01 acceptance manifest is incomplete or mismatched")
    for key in (
        "provider_evidence_sha256",
        "telegram_evidence_sha256",
        "lifecycle_evidence_sha256",
    ):
        if not DIGEST.fullmatch(str(value.get(key, ""))):
            raise ValueError(f"post-stop C-01 manifest {key} is invalid")
