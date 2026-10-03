"""Transport receipt consumer/schema; all external boundaries are fixture-owned."""

from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from engineering_control.db.base import Base
from engineering_control.db.models import DashboardEvent
from engineering_control.observability.agent import AgentObservabilityRepository, GatewayEventInput

CORRELATION = "a8a11130-1687-46ee-a417-449c72732ee5"


@pytest.fixture
def hook(monkeypatch):
    path = Path(__file__).parents[1] / "integrations/hermes-engineering-control/hook/handler.py"
    spec = importlib.util.spec_from_file_location("fixture_receipt_hook", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        socket.socket, "connect", MagicMock(side_effect=AssertionError("fixture socket attempt"))
    )
    module._authorized = lambda actor: actor == "123"
    module._record_acceptance = lambda *args: None
    module._request = lambda *args, **kwargs: {"status": "READY"}
    module.events = []
    module._gateway_event = lambda kind, command, **kwargs: module.events.append(
        {"event_type": kind, "command": command, **(module._RECEIPT_CONTEXT.get() or {}), **kwargs}
    )
    return module


def context(**changes):
    return {
        "platform": "telegram",
        "user_id": "123",
        "command": "usage",
        "raw_args": "",
        "platform_update_id": 71,
        "delivery_correlation_id": CORRELATION,
        **changes,
    }


def test_complete_command_to_receipt_correlation(hook):
    reply = hook.handle("command:usage", context())
    assert reply["observe_delivery"] is True
    assert all(event["delivery_correlation_id"] == CORRELATION for event in hook.events)
    assert all(event["event_type"] != "telegram.response_delivered" for event in hook.events)
    assert hook._RECEIPT_CONTEXT.get() is None
    hook.handle(
        "command-reply:completed",
        context(delivery_status="ACKNOWLEDGED", acknowledged_message_ids=["91", "92"]),
    )
    receipt = hook.events[-1]
    assert receipt["event_type"] == "telegram.response_delivered"
    assert receipt["receipt"]["platform_update_id"] == 71
    assert receipt["receipt"]["delivery_correlation_id"] == CORRELATION
    assert receipt["receipt"]["acknowledged_message_ids"] == ["91", "92"]


@pytest.mark.parametrize(
    "status", ["UNACKNOWLEDGED", "UNKNOWN", "UNAVAILABLE", "EMPTY", "REJECTED_OVERSIZE"]
)
def test_transport_failure_never_becomes_success(hook, status):
    hook.handle(
        "command-reply:completed", context(delivery_status=status, acknowledged_message_ids=[])
    )
    assert hook.events[-1]["event_type"] == "telegram.response_delivery_failed"
    assert hook.events[-1]["receipt"]["delivery_status"] == status


@pytest.mark.parametrize("ids", [[], ["1", "1"], ["token=fixture"], [1], ["1"] * 65])
def test_malformed_success_receipt_is_unknown(hook, ids):
    hook.handle(
        "command-reply:completed",
        context(delivery_status="ACKNOWLEDGED", acknowledged_message_ids=ids),
    )
    assert hook.events[-1]["event_type"] == "telegram.response_delivery_failed"
    assert hook.events[-1]["outcome"] == "UNKNOWN"
    assert hook.events[-1]["receipt"]["acknowledged_message_ids"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": "999"},
        {"platform": "slack"},
        {"platform_update_id": None},
        {"platform_update_id": True},
        {"delivery_correlation_id": "invalid"},
        {"command": "shell"},
    ],
)
def test_invalid_identity_cannot_attest_delivery(hook, changes):
    hook.handle(
        "command-reply:completed",
        context(**changes, delivery_status="ACKNOWLEDGED", acknowledged_message_ids=["1"]),
    )
    assert hook.events == []


def test_duplicate_update_reuses_command_idempotency_key_without_context_leak(hook):
    keys = []
    hook._request = lambda *args, **kwargs: (
        keys.append(hook._command_idempotency_key()) or {"status": "READY"}
    )
    hook.handle("command:usage", context())
    hook.handle("command:usage", context())
    hook.handle(
        "command:usage",
        context(
            delivery_correlation_id="a8a11130-1687-46ee-a417-449c72732ee6", platform_update_id=72
        ),
    )
    assert keys[0] == keys[1] == "telegram-" + CORRELATION
    assert keys[2] != keys[1]
    assert hook._RECEIPT_CONTEXT.get() is None


def valid_event(**changes):
    return {
        "event_type": "telegram.response_delivered",
        "command": "usage",
        "platform_update_id": 71,
        "delivery_correlation_id": CORRELATION,
        "delivery_status": "ACKNOWLEDGED",
        "acknowledged_message_ids": ["91", "92"],
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"platform_update_id": None},
        {"platform_update_id": True},
        {"delivery_correlation_id": None},
        {"acknowledged_message_ids": []},
        {"acknowledged_message_ids": ["1", "1"]},
        {"acknowledged_message_ids": ["secret=fixture"]},
        {"delivery_status": "UNKNOWN"},
        {"event_type": "telegram.response_prepared"},
    ],
)
def test_schema_rejects_unattested_or_mislabelled_success(changes):
    with pytest.raises(ValidationError):
        GatewayEventInput.model_validate(valid_event(**changes))


def test_legacy_producer_success_without_transport_receipt_rejected():
    with pytest.raises(ValidationError, match="transport receipt"):
        GatewayEventInput(event_type="telegram.response_delivered", command="usage")


@pytest.mark.asyncio
async def test_receipt_persistence_contains_only_bounded_identifiers(tmp_path):
    repository = AgentObservabilityRepository(
        f"sqlite:///{tmp_path / 'fixture.db'}", maximum_transcript_bytes=1024
    )
    Base.metadata.create_all(repository.engine)
    result = await repository.gateway_event(
        "hermes-gateway", GatewayEventInput.model_validate(valid_event())
    )
    assert result["status"] == "ACCEPTED"
    with repository.sessions() as session:
        event = session.query(DashboardEvent).one()
        assert event.metadata_json["delivery_correlation_id"] == str(UUID(CORRELATION))
        assert event.metadata_json["platform_update_id"] == 71
        assert event.metadata_json["acknowledged_message_ids"] == ["91", "92"]
        assert event.metadata_json["delivery_status"] == "ACKNOWLEDGED"
        assert "user_id" not in json.dumps(event.metadata_json)
