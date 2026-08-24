from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def load_validator() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "validate-telegram-acceptance.py"
    spec = importlib.util.spec_from_file_location("telegram_acceptance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(command: str, release: str, summary: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": 1,
        "release_sha": release,
        "actor_class": "allowlisted-telegram-user",
        "command": command,
        "summary": summary,
        "result_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
    }


def test_selects_complete_release_bound_sequence() -> None:
    module = load_validator()
    release = "a" * 40
    workflow_id = "acceptance-11111111-1111-4111-8111-111111111111"
    events = [
        event(
            "usage",
            release,
            {
                "providers": {
                    provider: {
                        "status": "AVAILABLE",
                        "authenticated": True,
                        "cli_present": True,
                        "rate_limited": False,
                    }
                    for provider in ("codex", "claude")
                }
            },
        ),
        event(
            "validate",
            release,
            {"workflow_id": workflow_id, "run_id": "run", "status": "ACCEPTED"},
        ),
        event(
            "status",
            release,
            {
                "requested_workflow_id": workflow_id,
                "workflow_id": workflow_id,
                "status": "COMPLETED",
                "validation": {
                    "status": "PASS",
                    "change_set_state": "AWAITING_STAGING_APPROVAL",
                    "evidence_sha256": "b" * 64,
                    "redaction_status": "SCANNED_CLEAN",
                },
            },
        ),
    ]

    selected = module.select_sequence(events, release)

    assert [value["command"] for value in selected] == ["usage", "validate", "status"]


def test_rejects_status_from_a_different_release_or_workflow() -> None:
    module = load_validator()
    release = "a" * 40
    with pytest.raises(ValueError, match="no complete"):
        module.select_sequence(
            [
                event("usage", release, {"providers": {}}),
                event(
                    "validate",
                    release,
                    {"workflow_id": "acceptance-one", "status": "ACCEPTED"},
                ),
                event(
                    "status",
                    "c" * 40,
                    {
                        "requested_workflow_id": "acceptance-two",
                        "workflow_id": "acceptance-two",
                        "status": "COMPLETED",
                    },
                ),
            ],
            release,
        )
