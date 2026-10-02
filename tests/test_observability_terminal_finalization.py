"""Isolated deterministic sequencing tests, not Temporal replay or provider evidence."""

import asyncio
import json
import unittest
from unittest.mock import patch

from temporalio.exceptions import ActivityError, RetryState

from engineering_control.workflows.definitions import (
    ObservabilityAcceptanceWorkflow,
    ReverseObservabilityAcceptanceWorkflow,
)
from engineering_control.workflows.inputs import ObservabilityAcceptanceInput


def failure():
    return ActivityError(
        "INERT_EXCEPTION_CANARY_DO_NOT_PERSIST",
        scheduled_event_id=1,
        started_event_id=2,
        identity="fixture",
        activity_type="execute_control_step",
        activity_id="fixture-1",
        retry_state=RetryState.NON_RETRYABLE_FAILURE,
    )


class FinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, reverse, mode):
        cls = ReverseObservabilityAcceptanceWorkflow if reverse else ObservabilityAcceptanceWorkflow
        instance = cls()
        calls = []
        primary, cleanup, final_error = failure(), failure(), failure()
        maker = "claude_maker" if reverse else "codex"
        checker = "codex_checker" if reverse else "claude"
        maker_invoke = "observability.invoke_" + maker
        checker_invoke = "observability.invoke_" + checker
        targets = {
            "register": "observability.register",
            "workspace": "observability.prepare_workspace",
            "legacy_workspace": "observability.prepare_workspace",
            "maker_prepare": "observability.prepare_" + maker,
            "checker_prepare": "observability.prepare_" + checker,
            "primary_and_cleanup": "observability.prepare_" + maker,
        }

        async def step(name, payload, **kwargs):
            calls.append((name, dict(payload)))
            if name == targets.get(mode):
                raise primary
            if name == "agent.record_start" and (
                mode == "maker_record"
                and payload["provider_step"] == maker_invoke
                or mode == "checker_record"
                and payload["provider_step"] == checker_invoke
            ):
                raise primary
            if name == "agent.persist_result" and mode == "persist":
                raise primary
            if name == maker_invoke:
                if mode == "cancel":
                    raise asyncio.CancelledError()
                if mode == "programming_error":
                    raise ValueError("INERT_PROGRAMMING_ERROR")
                if mode == "invoke_error":
                    raise primary
            if name == "observability.cleanup_workspace" and mode in (
                "cleanup",
                "primary_and_cleanup",
            ):
                raise cleanup
            if name == "observability.finalize":
                if mode == "finalize":
                    raise final_error
                return {**payload, "status": payload["terminal_status"]}
            if name in (maker_invoke, checker_invoke):
                failed = mode in ("unknown_blocker", "known_blocker") or (
                    mode == "maker_non_success"
                    and name == maker_invoke
                    or mode == "checker_non_success"
                    and name == checker_invoke
                )
                return {
                    **payload,
                    "status": "FAILED" if failed else "SUCCESS",
                    "blocker": "RATE_LIMITED"
                    if mode == "known_blocker"
                    else "INERT_UNTRUSTED_BLOCKER",
                }
            return dict(payload)

        instance._step = step
        caught = None
        returned = None
        patched = not mode.startswith("legacy_")
        with patch(
            "engineering_control.workflows.definitions.workflow.patched", return_value=patched
        ):
            try:
                returned = await instance.run(
                    ObservabilityAcceptanceInput(
                        workspace_id="fe523fe6-1702-42ea-ad2c-a3cf4c15002a"
                    )
                )
            except (ActivityError, asyncio.CancelledError, ValueError) as error:
                caught = error
        names = [name for name, _ in calls]
        finalized = [payload for name, payload in calls if name == "observability.finalize"]
        self.assertNotIn("INERT_EXCEPTION_CANARY_DO_NOT_PERSIST", json.dumps(calls))
        if mode == "register":
            self.assertIs(caught, primary)
            self.assertEqual(names, ["observability.register"])
            return
        if mode == "legacy_workspace":
            self.assertIs(caught, primary)
            self.assertEqual(names, ["observability.register", "observability.prepare_workspace"])
            return
        self.assertEqual(names.count("observability.cleanup_workspace"), 1)
        if mode in ("cancel", "programming_error"):
            self.assertIsInstance(
                caught, asyncio.CancelledError if mode == "cancel" else ValueError
            )
            self.assertEqual(finalized, [])
            return
        self.assertEqual(len(finalized), 1)
        if mode == "legacy_success":
            self.assertLess(
                names.index("observability.finalize"),
                names.index("observability.cleanup_workspace"),
            )
        else:
            self.assertLess(
                names.index("observability.cleanup_workspace"),
                names.index("observability.finalize"),
            )
        if mode in ("success", "legacy_success"):
            self.assertIsNone(caught)
            self.assertEqual(returned["status"], "COMPLETED")
            self.assertLess(names.index(maker_invoke), names.index(checker_invoke))
        elif mode == "finalize":
            self.assertIs(caught, final_error)
            self.assertIsNone(returned)
        else:
            self.assertEqual(finalized[0]["terminal_status"], "FAILED")
            self.assertNotIn("INERT_UNTRUSTED_BLOCKER", finalized[0]["blocker"])
            if mode == "cleanup":
                self.assertIs(caught, cleanup)
                self.assertEqual(finalized[0]["blocker"], "OBSERVABILITY_CLEANUP_FAILED")
            elif mode in targets or mode in ("maker_record", "checker_record", "persist"):
                self.assertIs(caught, primary)
                if mode == "primary_and_cleanup":
                    self.assertEqual(
                        finalized[0]["blocker"], "OBSERVABILITY_ACTIVITY_AND_CLEANUP_FAILED"
                    )
            else:
                self.assertIsNone(caught)
                self.assertEqual(returned["status"], "FAILED")
                if mode == "known_blocker":
                    self.assertEqual(finalized[0]["blocker"], "RATE_LIMITED")
                if mode == "invoke_error":
                    self.assertEqual(finalized[0]["blocker"], "PROVIDER_ACTIVITY_FAILED")


def make_case(reverse, mode):
    async def test(self):
        await self.exercise(reverse, mode)

    return test


for direction in (False, True):
    for scenario in (
        "success",
        "register",
        "workspace",
        "maker_prepare",
        "checker_prepare",
        "maker_record",
        "checker_record",
        "persist",
        "invoke_error",
        "cleanup",
        "primary_and_cleanup",
        "maker_non_success",
        "checker_non_success",
        "finalize",
        "cancel",
        "programming_error",
        "unknown_blocker",
        "known_blocker",
        "legacy_success",
        "legacy_workspace",
    ):
        setattr(
            FinalizationTests,
            f"test_{'reverse' if direction else 'forward'}_{scenario}",
            make_case(direction, scenario),
        )

if __name__ == "__main__":
    unittest.main(verbosity=1)
