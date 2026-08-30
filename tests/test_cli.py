import os
from pathlib import Path

import pytest

from engineering_control.cli import APIClient, dispatch, parser


def test_cli_accepts_service_group_read_token(tmp_path: Path) -> None:
    token = tmp_path / "api.token"
    token.write_text("x" * 48, encoding="utf-8")
    os.chmod(token, 0o640)

    client = APIClient("http://127.0.0.1:8740", token)

    client.client.close()


def test_cli_rejects_group_writable_token(tmp_path: Path) -> None:
    token = tmp_path / "api.token"
    token.write_text("x" * 48, encoding="utf-8")
    os.chmod(token, 0o660)

    with pytest.raises(PermissionError, match="permissions are too broad"):
        APIClient("http://127.0.0.1:8740", token)


def test_cli_reconciliation_defaults_to_observation_only() -> None:
    class FakeClient:
        def request(
            self, method: str, path: str, payload: dict[str, object] | None = None
        ) -> dict[str, object]:
            return {"method": method, "path": path, "payload": payload or {}}

    arguments = parser().parse_args(
        ["reconcile", "11111111-1111-4111-8111-111111111111", "--candidate-build-run-id", "42"]
    )
    result = dispatch(FakeClient(), arguments)  # type: ignore[arg-type]

    assert result["path"] == "/v1/deliveries/reconcile"
    assert result["payload"]["dry_run"] is True
    assert result["payload"]["candidate_build_run_id"] == 42


def test_cli_finding_verification_uses_only_the_bounded_route() -> None:
    class FakeClient:
        def request(
            self, method: str, path: str, payload: dict[str, object] | None = None
        ) -> dict[str, object]:
            return {"method": method, "path": path, "payload": payload}

    finding_id = "11111111-1111-4111-8111-111111111111"
    arguments = parser().parse_args(["verify-finding", finding_id])
    result = dispatch(FakeClient(), arguments)  # type: ignore[arg-type]

    assert result == {
        "method": "POST",
        "path": "/v1/findings/verify",
        "payload": {"finding_id": finding_id},
    }


def test_cli_backlog_import_and_start_use_only_fixed_routes() -> None:
    class FakeClient:
        def request(
            self, method: str, path: str, payload: dict[str, object] | None = None
        ) -> dict[str, object]:
            return {"method": method, "path": path, "payload": payload or {}}

    imported = dispatch(
        FakeClient(),  # type: ignore[arg-type]
        parser().parse_args(["backlog-import", "falcone", "bugs-and-docs", "--max-items", "1"]),
    )
    started = dispatch(
        FakeClient(),  # type: ignore[arg-type]
        parser().parse_args(
            [
                "backlog-start",
                "falcone",
                "11111111-1111-4111-8111-111111111111",
                "R2",
                "full-delivery",
                "--max-items",
                "1",
            ]
        ),
    )
    assert imported["path"] == "/v1/backlogs/import"
    assert imported["payload"]["source"] == "github-open"
    assert started["path"] == "/v1/commands"
    assert started["payload"]["action"] == "solve_backlog"
    assert started["payload"]["delivery_mode"] == "full-delivery"


@pytest.mark.parametrize(
    ("command", "action"),
    [
        ("backlog-pause-after-current", "pause_after_current"),
        ("backlog-stop-after-current", "stop_after_current"),
    ],
)
def test_cli_backlog_boundary_controls_use_the_typed_command_bus(command: str, action: str) -> None:
    class FakeClient:
        def request(
            self, method: str, path: str, payload: dict[str, object] | None = None
        ) -> dict[str, object]:
            return {"method": method, "path": path, "payload": payload or {}}

    result = dispatch(
        FakeClient(),  # type: ignore[arg-type]
        parser().parse_args([command, "backlog-loop-synthetic"]),
    )

    assert result == {
        "method": "POST",
        "path": "/v1/commands",
        "payload": {"action": action, "workflow_id": "backlog-loop-synthetic"},
    }


def test_cli_advanced_audit_uses_the_shared_typed_command_bus() -> None:
    class FakeClient:
        def request(
            self, method: str, path: str, payload: dict[str, object] | None = None
        ) -> dict[str, object]:
            return {"method": method, "path": path, "payload": payload or {}}

    result = dispatch(
        FakeClient(),  # type: ignore[arg-type]
        parser().parse_args(
            [
                "audit",
                "falcone",
                "--name",
                "bounded-audit",
                "--source-sha",
                "a" * 40,
                "--deployment-sha",
                "b" * 40,
                "--domains",
                "gateway",
                "--personas",
                "tenant-admin",
                "--lenses",
                "correctness",
                "--environments",
                "staging",
                "--execute",
            ]
        ),
    )

    assert result["path"] == "/v1/commands"
    assert result["payload"]["action"] == "audit_campaign"
    assert result["payload"]["execute"] is True
    assert result["payload"]["production"] is False


def test_cli_evidence_exports_use_typed_commands_and_fixed_retrieval_route() -> None:
    class FakeClient:
        def request(
            self, method: str, path: str, payload: dict[str, object] | None = None
        ) -> dict[str, object]:
            return {"method": method, "path": path, "payload": payload or {}}

    campaign_id = "11111111-1111-4111-8111-111111111111"
    operation_id = "22222222-2222-4222-8222-222222222222"
    campaign = dispatch(
        FakeClient(),  # type: ignore[arg-type]
        parser().parse_args(["export-campaign", campaign_id]),
    )
    workflow = dispatch(
        FakeClient(),  # type: ignore[arg-type]
        parser().parse_args(["export-workflow", "workflow-safe-id"]),
    )
    download = dispatch(
        FakeClient(),  # type: ignore[arg-type]
        parser().parse_args(["operation-export", operation_id]),
    )

    assert campaign == {
        "method": "POST",
        "path": "/v1/commands",
        "payload": {
            "action": "evidence_export_campaign",
            "campaign_id": campaign_id,
            "format": "json",
        },
    }
    assert workflow["path"] == "/v1/commands"
    assert workflow["payload"] == {
        "action": "evidence_export_workflow",
        "workflow_id": "workflow-safe-id",
        "format": "json",
    }
    assert download == {
        "method": "GET",
        "path": f"/v1/operations/{operation_id}/export",
        "payload": {},
    }


def test_cli_backlog_parser_rejects_arbitrary_filters_repositories_and_modes() -> None:
    with pytest.raises(SystemExit):
        parser().parse_args(["backlog-import", "falcone", "arbitrary"])
    with pytest.raises(SystemExit):
        parser().parse_args(["backlog-skip", "loop", "outside", "1", "DUPLICATE"])
    with pytest.raises(SystemExit):
        parser().parse_args(
            [
                "backlog-start",
                "falcone",
                "11111111-1111-4111-8111-111111111111",
                "R2",
                "production",
            ]
        )
