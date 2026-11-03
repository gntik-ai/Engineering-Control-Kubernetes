"""Behavioral regression for HERMES-2026-017; no product/Temporal network calls."""

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import NotificationOutbox, Run
from engineering_control.workflows.definitions import CleanupWorkflow
from engineering_control.workflows.inputs import CleanupInput


class CleanupInspectionOutcomeTests(IsolatedAsyncioTestCase):
    async def exercise(self, *, blocked: bool, dry_run: bool, modern: bool) -> None:
        with TemporaryDirectory() as temporary:
            handler = MetadataActivityHandler(
                Settings(database_url=f"sqlite:///{Path(temporary) / 'fixture.sqlite'}")
            )
            Base.metadata.create_all(handler.engine)
            identifier = uuid4()
            workflow_id = f"fixture-cleanup-{identifier}"
            with handler.sessions.begin() as session:
                session.add(
                    Run(
                        id=identifier,
                        workflow_id=workflow_id,
                        run_type="cleanup",
                        status="RUNNING",
                        project="platform",
                        started_at=datetime.now(UTC),
                    )
                )
            steps: list[str] = []
            final_payload: dict = {}

            async def step(name: str, payload: dict) -> dict:
                nonlocal final_payload
                steps.append(name)
                self.assertNotIn(".execute", name)
                self.assertNotEqual(name, "cleanup.authorize")
                self.assertNotEqual(name, "identity.recover_stale")
                payload = {**payload, "orchestration": {"workflow_id": workflow_id}}
                if name == "identity.inspect_stale":
                    payload["identity_recovery_blockers"] = (
                        {"musematic": "SUPERADMIN_AUTHENTICATION_FAILED"} if blocked else {}
                    )
                    payload["status"] = "BLOCKED" if blocked else "PASS"
                elif name == "cleanup.inspect_preview_namespaces":
                    # This subsequent success must not erase the earlier blocker.
                    payload["status"] = "PREVIEW_CLEANUP_INSPECTED"
                elif name == "cleanup.finalize":
                    final_payload = payload
                    return await handler.handle(name, payload)
                return payload

            instance = CleanupWorkflow()
            with (
                patch.object(instance, "_step", side_effect=step),
                # NEW-245: the legacy (unpaged) path's command contract.
                patch.object(instance, "_paged_cleanup_patched", return_value=False),
                patch(
                    "engineering_control.workflows.definitions.workflow.patched",
                    # Merged with main: the dry run also consults main's NEW-85 marker,
                    # held off here so the 2b678d9 command contract stays exact.
                    side_effect=lambda name: (
                        modern if name == "cleanup-inspection-outcome-v1" else False
                    ),
                ) as marker,
            ):
                result = await instance.run(CleanupInput(dry_run=dry_run))
            markers = [call.args[0] for call in marker.call_args_list]
            self.assertEqual(markers[0], "cleanup-inspection-outcome-v1")
            # NEW-119 is consulted only with a blocker and held off here (legacy path).
            self.assertLessEqual(
                set(markers[1:]),
                {"cleanup-orphan-worktrees-v1", "cleanup-identity-blocker-project-scope-v1"},
            )
            expected = "BLOCKED" if modern and blocked else "DRY_RUN"
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["context"]["status"], expected)
            self.assertEqual(len(steps), 9)
            self.assertEqual(steps[-1], "cleanup.finalize")
            # A retry of the real finalization activity must not duplicate its outbox row.
            await handler.handle("cleanup.finalize", final_payload)
            with handler.sessions() as session:
                run = session.get(Run, identifier)
                alerts = list(session.scalars(select(NotificationOutbox)))
                self.assertEqual(run.status, "BLOCKED" if expected == "BLOCKED" else "SUCCESS")
                self.assertEqual(len(alerts), 1 if expected == "BLOCKED" else 0)
                if alerts:
                    self.assertEqual(run.error_code, "CLEANUP_IDENTITY_INSPECTION_BLOCKED")
                    self.assertEqual(alerts[0].payload_json["error_code"], run.error_code)
            handler.engine.dispose()

    async def test_new_dry_run_preserves_blocker_and_durable_alert(self) -> None:
        await self.exercise(blocked=True, dry_run=True, modern=True)

    async def test_new_execution_never_reaches_destructive_steps_with_blocker(self) -> None:
        await self.exercise(blocked=True, dry_run=False, modern=True)

    async def test_healthy_inspection_still_finishes_dry_run(self) -> None:
        await self.exercise(blocked=False, dry_run=True, modern=True)

    async def test_legacy_marker_path_preserves_prior_command_contract(self) -> None:
        await self.exercise(blocked=True, dry_run=True, modern=False)
