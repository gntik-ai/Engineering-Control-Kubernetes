import os
import sys

import pytest

from engineering_control.adapters.process import (
    run_jsonl_process,
    subscription_only_environment,
)


def test_subscription_environment_removes_api_paid_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake")
    environment = subscription_only_environment("codex")
    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment
    assert "AWS_ACCESS_KEY_ID" not in environment


@pytest.mark.asyncio
async def test_jsonl_process_redacts_events_and_prompt(tmp_path: object) -> None:
    fake_token = bytes((103, 104, 112, 95)).decode() + "A" * 36
    script = (
        "import json,sys; value=sys.stdin.read(); "
        "print(json.dumps({'type':'result','secret':value.strip()}))"
    )
    result = await run_jsonl_process(
        [sys.executable, "-c", script],
        prompt=fake_token,
        cwd=os.getcwd(),
        timeout_seconds=30,
        provider="synthetic",
    )
    assert result.exit_code == 0
    assert fake_token not in str(result.events)
    assert result.events[0]["secret"] == "[REDACTED]"  # noqa: S105
