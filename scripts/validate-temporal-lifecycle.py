#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import httpx
from temporalio.client import Client
from temporalio.worker import Worker

from engineering_control.api.main import create_app
from engineering_control.api.service import TemporalControlService
from engineering_control.config import Settings
from engineering_control.db.repository import EmptyMetadataRepository
from engineering_control.validation.lifecycle import AcceptanceActivities
from engineering_control.workflows.definitions import AcceptanceLifecycleWorkflow


async def run(target: str, namespace: str, root: Path, hold_seconds: int) -> dict[str, object]:
    if target not in {"127.0.0.1:7233", "localhost:7233"}:
        raise ValueError("validation script only connects to loopback Temporal")
    root = root.resolve()  # noqa: ASYNC240 - bounded one-time validation setup
    root.mkdir(  # noqa: ASYNC240 - bounded one-time validation setup
        parents=True, exist_ok=True, mode=0o750
    )
    run_root = root / str(uuid4())
    run_root.mkdir(mode=0o750)
    validation_id = run_root.name
    activity_task_queue = f"engineering-acceptance-validation-{validation_id}"
    control_task_queue = f"engineering-acceptance-control-validation-{validation_id}"
    client = await Client.connect(target, namespace=namespace)
    acceptance = AcceptanceActivities(root)
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="acceptance-activity")
    activity_worker = Worker(
        client,
        task_queue=activity_task_queue,
        workflows=[],
        activities=acceptance.activities,
        activity_executor=executor,
        max_concurrent_activities=2,
    )
    workflow_worker = Worker(
        client,
        task_queue=control_task_queue,
        workflows=[AcceptanceLifecycleWorkflow],
        activities=[],
    )
    activity_task = asyncio.create_task(activity_worker.run())
    workflow_task = asyncio.create_task(workflow_worker.run())
    token = secrets.token_hex(32)
    settings = Settings(
        synthetic_mode=True,
        acceptance_enabled=True,
        temporal_target=target,
        temporal_namespace=namespace,
        acceptance_root=run_root,
    )
    service = TemporalControlService(
        client,
        control_task_queue,
        EmptyMetadataRepository(),
        acceptance_activity_task_queue=activity_task_queue,
    )
    app = create_app(settings=settings, service=service, token=token)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://acceptance.invalid",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Engineering-Actor": "acceptance-cli",
            },
        ) as api:
            response = await api.post(
                "/v1/validation/lifecycle", json={"hold_seconds": hold_seconds}
            )
            response.raise_for_status()
            accepted = response.json()
    handle = client.get_workflow_handle(accepted["workflow_id"])
    change_set_id = accepted["workflow_id"].removeprefix("acceptance-")
    await asyncio.sleep(1)
    await workflow_worker.shutdown()
    await workflow_task
    replacement = Worker(
        client,
        task_queue=control_task_queue,
        workflows=[AcceptanceLifecycleWorkflow],
        activities=[],
    )
    replacement_task = asyncio.create_task(replacement.run())
    try:
        result = await asyncio.wait_for(handle.result(), timeout=180)
        if not isinstance(result, dict) or result.get("status") != "PASS":
            raise RuntimeError("acceptance lifecycle did not pass")
        if result.get("change_set_state") != "AWAITING_STAGING_APPROVAL":
            raise RuntimeError("acceptance lifecycle ended at the wrong state")
        if result.get("maker_provider") == result.get("checker_provider"):
            raise RuntimeError("acceptance lifecycle did not separate maker/checker")
        evidence = Path(str(result["evidence_path"]))
        if not evidence.is_file() or not evidence.resolve().is_relative_to(root):  # noqa: ASYNC240
            raise RuntimeError("acceptance evidence is missing or outside its root")
        return {
            "status": result["status"],
            "workflow_id": handle.id,
            "run_id": accepted["run_id"],
            "change_set_id": str(change_set_id),
            "change_set_state": result["change_set_state"],
            "maker_provider": result["maker_provider"],
            "checker_provider": result["checker_provider"],
            "evidence_path": str(evidence),
            "evidence_sha256": result["evidence_sha256"],
            "workflow_worker_restarted": True,
            "authenticated_api_path": True,
        }
    finally:
        await replacement.shutdown()
        await activity_worker.shutdown()
        for task in (replacement_task, activity_task):
            with suppress(asyncio.CancelledError):
                await task
        executor.shutdown(wait=True, cancel_futures=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="127.0.0.1:7233")
    parser.add_argument("--namespace", default="engineering-control")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/home/hermes/engineering-bootstrap/acceptance-runs"),
    )
    parser.add_argument("--hold-seconds", type=int, default=5, choices=range(2, 31))
    arguments = parser.parse_args()
    result = asyncio.run(
        run(arguments.target, arguments.namespace, arguments.root, arguments.hold_seconds)
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
