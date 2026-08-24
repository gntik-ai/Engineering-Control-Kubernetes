import os
import sys
from typing import Any

import pytest

from engineering_control.adapters.claude.client import ClaudeCLIAdapter
from engineering_control.adapters.codex.client import CodexCLIAdapter
from engineering_control.adapters.process import (
    run_jsonl_process,
    subscription_only_environment,
)


class _FakeProcess:
    def __init__(self, stdout: bytes = b"") -> None:
        self.returncode = 0
        self.stdout = stdout

    async def wait(self) -> int:
        return 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, b""


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
@pytest.mark.parametrize(
    ("adapter", "stdout"),
    (
        (CodexCLIAdapter(), b""),
        (ClaudeCLIAdapter(), b'{"loggedIn":true}'),
    ),
)
async def test_authentication_probe_removes_api_paid_credentials(
    adapter: CodexCLIAdapter | ClaudeCLIAdapter,
    stdout: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args: str, **kwargs: Any) -> _FakeProcess:
        captured.update(kwargs)
        return _FakeProcess(stdout)

    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    assert await adapter.authenticated() is True
    environment = captured["env"]
    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment


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


@pytest.mark.asyncio
async def test_jsonl_process_preserves_numeric_usage_counters_during_redaction() -> None:
    script = (
        "import json; print(json.dumps({'type':'result','usage':"
        "{'input_tokens':123,'output_tokens':17,'cache_read_input_tokens':9}}))"
    )
    result = await run_jsonl_process(
        [sys.executable, "-c", script],
        prompt="bounded",
        cwd=os.getcwd(),
        timeout_seconds=30,
        provider="claude",
    )

    usage = ClaudeCLIAdapter._usage(result.events)
    assert usage.input_tokens == 123
    assert usage.output_tokens == 17
    assert usage.cached_input_tokens == 9
