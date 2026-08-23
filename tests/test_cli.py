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
