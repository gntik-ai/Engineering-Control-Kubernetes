"""The command handler cannot attest a send performed later by the transport.

This standalone unittest file also runs in the credential-free, network-none
closure fixture. It executes the real hook with injected API/event boundaries.
"""

from __future__ import annotations

import importlib.util
import socket
import unittest
from pathlib import Path
from unittest.mock import patch


class GatewayReplyTruthfulnessTests(unittest.TestCase):
    def setUp(self):
        supplied = Path("/input/handler.py")
        source = (
            supplied
            if supplied.exists()
            else (
                Path(__file__).resolve().parents[1]
                / "integrations/hermes-engineering-control/hook/handler.py"
            )
        )
        spec = importlib.util.spec_from_file_location("gateway_reply_truthfulness_hook", source)
        assert spec and spec.loader
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)
        self.events = []

        def capture_event(kind, command, **kwargs):
            self.events.append((kind, command))

        self.hook._gateway_event = capture_event
        self.hook._authorized = lambda actor: actor == "123"
        self.hook._record_acceptance = lambda *args: None
        self.hook._request = lambda *args, **kwargs: {"status": "READY", "capacity": "UNKNOWN"}
        self.context = {"platform": "telegram", "user_id": "123", "raw_args": ""}
        # Independent network-none containment is supplied by the runner. This
        # additional guard turns any accidental socket attempt into a test error.
        self.sockets = patch.object(
            socket, "socket", side_effect=AssertionError("fixture socket attempt")
        )
        self.sockets.start()
        self.addCleanup(self.sockets.stop)

    def names(self):
        return [kind for kind, _command in self.events]

    def test_success_is_prepared_not_transport_delivered(self):
        result = self.hook.handle("command:usage", self.context)
        self.assertEqual(result["decision"], "handled")
        self.assertIn("READY", result["message"])
        self.assertIn("engineering.api_response_completed", self.names())
        self.assertIn("telegram.response_prepared", self.names())
        self.assertNotIn("telegram.response_delivered", self.names())

    def test_api_failure_is_not_a_telegram_delivery_failure(self):
        def failed(*args, **kwargs):
            raise RuntimeError("synthetic API unavailable")

        self.hook._request = failed
        result = self.hook.handle("command:usage", self.context)
        self.assertEqual(result["decision"], "handled")
        self.assertIn("engineering.request_failed", self.names())
        self.assertNotIn("telegram.response_delivery_failed", self.names())
        self.assertNotIn("telegram.response_delivered", self.names())

    def test_formatting_failure_cannot_claim_delivery(self):
        def failed(*args, **kwargs):
            raise ValueError("synthetic formatter failure")

        self.hook._format = failed
        self.hook.handle("command:usage", self.context)
        self.assertNotIn("telegram.response_prepared", self.names())
        self.assertNotIn("telegram.response_delivered", self.names())
        self.assertIn("engineering.request_failed", self.names())

    def test_unauthorized_sender_has_no_request_or_delivery_claim(self):
        self.context["user_id"] = "999"
        result = self.hook.handle("command:usage", self.context)
        self.assertEqual(result["decision"], "deny")
        self.assertNotIn("engineering.api_request_started", self.names())
        self.assertNotIn("telegram.response_delivered", self.names())

    def test_unsupported_command_is_denied(self):
        result = self.hook.handle("command:arbitrary-shell", self.context)
        self.assertEqual(result["decision"], "deny")
        self.assertNotIn("engineering.api_request_started", self.names())
        self.assertNotIn("telegram.response_delivered", self.names())


if __name__ == "__main__":
    unittest.main(verbosity=2)
