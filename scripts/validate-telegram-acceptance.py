#!/usr/bin/env python3
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

RECEIPT = Path("/home/hermes/engineering-bootstrap/telegram-acceptance.jsonl")
EVIDENCE_ROOT = Path("/srv/engineering/evidence")
RELEASE = re.compile(r"^[a-f0-9]{40}$")


def _valid_digest(event: dict[str, Any]) -> bool:
    summary = event.get("summary")
    if not isinstance(summary, dict):
        return False
    serialized = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    return event.get("result_sha256") == hashlib.sha256(serialized.encode()).hexdigest()


def select_sequence(
    events: list[dict[str, Any]], release_sha: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not RELEASE.fullmatch(release_sha):
        raise ValueError("installed release is not a full Git SHA")
    eligible = [
        event
        for event in events
        if event.get("schema_version") == 1
        and event.get("release_sha") == release_sha
        and event.get("actor_class") == "allowlisted-telegram-user"
        and _valid_digest(event)
    ]
    for status_index in range(len(eligible) - 1, -1, -1):
        status = eligible[status_index]
        status_summary = status.get("summary")
        if status.get("command") != "status" or not isinstance(status_summary, dict):
            continue
        validation = status_summary.get("validation")
        if (
            status_summary.get("status") != "COMPLETED"
            or not isinstance(validation, dict)
            or validation.get("status") != "PASS"
            or validation.get("change_set_state") != "AWAITING_STAGING_APPROVAL"
            or validation.get("redaction_status") not in {"REDACTED", "SCANNED_CLEAN"}
            or not re.fullmatch(r"[a-f0-9]{64}", str(validation.get("evidence_sha256", "")))
        ):
            continue
        workflow_id = status_summary.get("workflow_id")
        if (
            not isinstance(workflow_id, str)
            or status_summary.get("requested_workflow_id") != workflow_id
        ):
            continue
        for validate_index in range(status_index - 1, -1, -1):
            validate = eligible[validate_index]
            validate_summary = validate.get("summary")
            if (
                validate.get("command") != "validate"
                or not isinstance(validate_summary, dict)
                or validate_summary.get("workflow_id") != workflow_id
                or validate_summary.get("status") != "ACCEPTED"
            ):
                continue
            for usage_index in range(validate_index - 1, -1, -1):
                usage = eligible[usage_index]
                usage_summary = usage.get("summary")
                providers = (
                    usage_summary.get("providers") if isinstance(usage_summary, dict) else None
                )
                if usage.get("command") != "usage" or not isinstance(providers, dict):
                    continue
                if all(
                    isinstance(providers.get(provider), dict)
                    and providers[provider].get("status") == "AVAILABLE"
                    and providers[provider].get("authenticated") is True
                    and providers[provider].get("cli_present") is True
                    and providers[provider].get("rate_limited") is False
                    for provider in ("codex", "claude")
                ):
                    return usage, validate, status
    raise ValueError("no complete /usage -> /validate -> /status acceptance sequence")


def _load_receipts(path: Path) -> list[dict[str, Any]]:
    metadata = path.stat()
    hermes_uid = pwd.getpwnam("hermes").pw_uid
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != hermes_uid
        or metadata.st_nlink != 1
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise PermissionError("Telegram receipt file ownership or permissions are unsafe")
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Telegram receipt line {line_number} is not an object")
            events.append(value)
    return events


def _persist(
    sequence: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    receipt: Path,
    release_sha: str,
) -> tuple[Path, str]:
    evidence_root = EVIDENCE_ROOT.resolve()
    acceptance_root = evidence_root / "acceptance"
    acceptance_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    if acceptance_root.resolve() != acceptance_root:
        raise PermissionError("canonical acceptance evidence directory must not be a symlink")
    engineering_gid = grp.getgrnam("engineering-control").gr_gid
    os.chown(acceptance_root, 0, engineering_gid)
    acceptance_root.chmod(0o750)
    directory = acceptance_root / f"telegram-{uuid4()}"
    directory.mkdir(mode=0o750)
    if directory.resolve().parent != acceptance_root:
        raise PermissionError("Telegram evidence directory escaped the canonical root")
    os.chown(directory, 0, engineering_gid)
    receipt_sha = hashlib.sha256(receipt.read_bytes()).hexdigest()
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "created_at": datetime.now(UTC).isoformat(),
        "release_sha": release_sha,
        "receipt_sha256": receipt_sha,
        "workflow_id": sequence[1]["summary"]["workflow_id"],
        "commands": [event["command"] for event in sequence],
        "events": list(sequence),
    }
    output = directory / "telegram-acceptance.json"
    descriptor = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o640,
    )
    try:
        rendered = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
        os.write(descriptor, rendered)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chown(output, 0, engineering_gid)
    output.chmod(0o640)
    return output, hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, default=RECEIPT)
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run with sudo so canonical evidence can be persisted safely")
    release_sha = Path("/opt/engineering-control/source").resolve().parent.name
    events = _load_receipts(arguments.receipt)
    sequence = select_sequence(events, release_sha)
    output, digest = _persist(sequence, arguments.receipt, release_sha)
    print(f"PASS Telegram acceptance evidence={output} sha256={digest}")


if __name__ == "__main__":
    main()
