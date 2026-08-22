from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import httpx

from engineering_control.security.redaction import SecretRedactor


class APIClient:
    def __init__(self, base_url: str, token_file: Path) -> None:
        metadata = token_file.stat()
        if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError("API token file permissions are too broad")
        token = token_file.read_text(encoding="utf-8").strip()
        self.client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        response = self.client.request(method, path, json=payload)
        response.raise_for_status()
        return SecretRedactor().redact(response.json()).value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="engineering-ctl")
    root.add_argument(
        "--api-url",
        default=os.getenv("ENGINEERING_API_URL", "http://127.0.0.1:8740"),
    )
    root.add_argument(
        "--token-file",
        type=Path,
        default=Path(
            os.getenv("ENGINEERING_API_TOKEN_FILE", "/etc/engineering-control/env/api.token")
        ),
    )
    commands = root.add_subparsers(dest="command", required=True)

    audit = commands.add_parser("audit")
    audit.add_argument("project", choices=("falcone", "musematic"))
    audit.add_argument("--name", required=True)
    audit.add_argument("--source-sha", required=True)
    audit.add_argument("--deployment-sha", required=True)
    audit.add_argument("--domains", nargs="+", required=True)
    audit.add_argument("--personas", nargs="+", required=True)
    audit.add_argument("--lenses", nargs="+", required=True)
    audit.add_argument("--environments", nargs="+", required=True)
    audit.add_argument("--execute", action="store_true")

    commands.add_parser("campaigns").add_argument("--limit", type=int, default=50)
    commands.add_parser("findings").add_argument("--limit", type=int, default=50)
    for name in ("status", "run"):
        commands.add_parser(name).add_argument("workflow_id")
    for name in ("pause", "resume", "cancel"):
        commands.add_parser(name).add_argument("workflow_id")
    commands.add_parser("usage")
    commands.add_parser("evidence").add_argument("artifact_id")
    delivery = commands.add_parser("delivery")
    delivery.add_argument("finding_id")
    delivery.add_argument("--risk", choices=("R0", "R1", "R2", "R3"), required=True)
    delivery.add_argument("--execute", action="store_true")
    for decision in ("approve", "reject"):
        approval = commands.add_parser(decision)
        approval.add_argument("change_set_id")
        approval.add_argument("artifact_binding_sha256")
        approval.add_argument("--typed-confirmation")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("deployment_id")
    rollback.add_argument("artifact_binding_sha256")
    rollback.add_argument("--execute", action="store_true")
    return root


def dispatch(client: APIClient, arguments: argparse.Namespace) -> Any:
    command = arguments.command
    if command == "audit":
        return client.request(
            "POST",
            "/v1/campaigns",
            {
                "project": arguments.project,
                "name": arguments.name,
                "source_sha": arguments.source_sha,
                "deployment_sha": arguments.deployment_sha,
                "domains": arguments.domains,
                "personas": arguments.personas,
                "lenses": arguments.lenses,
                "environments": arguments.environments,
                "dry_run": not arguments.execute,
            },
        )
    if command in {"campaigns", "findings"}:
        return client.request("GET", f"/v1/{command}?limit={arguments.limit}")
    if command in {"status", "run"}:
        return client.request("GET", f"/v1/runs/{arguments.workflow_id}")
    if command in {"pause", "resume"}:
        return client.request("POST", f"/v1/campaigns/{arguments.workflow_id}/{command}")
    if command == "cancel":
        return client.request("POST", f"/v1/runs/{arguments.workflow_id}/cancel")
    if command == "usage":
        return client.request("GET", "/v1/usage")
    if command == "evidence":
        return client.request("GET", f"/v1/evidence/{arguments.artifact_id}")
    if command == "delivery":
        return client.request(
            "POST",
            "/v1/deliveries",
            {
                "finding_id": arguments.finding_id,
                "risk_class": arguments.risk,
                "dry_run": not arguments.execute,
            },
        )
    if command in {"approve", "reject"}:
        return client.request(
            "POST",
            "/v1/approvals",
            {
                "change_set_id": arguments.change_set_id,
                "artifact_binding_sha256": arguments.artifact_binding_sha256,
                "decision": command,
                "typed_confirmation": arguments.typed_confirmation,
            },
        )
    if command == "rollback":
        return client.request(
            "POST",
            "/v1/deployments/rollback",
            {
                "deployment_id": arguments.deployment_id,
                "expected_artifact_binding_sha256": arguments.artifact_binding_sha256,
                "dry_run": not arguments.execute,
            },
        )
    raise ValueError(f"unsupported command: {command}")


def main() -> None:
    arguments = parser().parse_args()
    try:
        result = dispatch(APIClient(arguments.api_url, arguments.token_file), arguments)
    except (OSError, ValueError, httpx.HTTPError) as error:
        print(f"engineering-ctl: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
