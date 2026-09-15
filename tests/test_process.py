import os
import sys
from typing import Any

import pytest

from engineering_control.adapters.auth_probe import AuthProbeResult
from engineering_control.adapters.claude.client import ClaudeCLIAdapter
from engineering_control.adapters.codex.client import CodexCLIAdapter
from engineering_control.adapters.process import (
    run_jsonl_process,
    subscription_only_environment,
)


def test_subscription_environment_removes_api_paid_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "OMNIROUTE_URL",
        "OMNIROUTE_BASE_URL",
        "OPENAI_BASE_URL",
        "ANTHROPIC_BASE_URL",
        "LLM_BASE_URL",
        "MODEL_GATEWAY",
        "MODEL_ROUTER",
        "AWS_ACCESS_KEY_ID",
    }
    for key in forbidden:
        monkeypatch.setenv(key, "fake")
    environment = subscription_only_environment("codex")
    assert forbidden.isdisjoint(environment)


def test_subscription_environment_rejects_every_forbidden_override() -> None:
    for key in (
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "OMNIROUTE_URL",
        "OMNIROUTE_BASE_URL",
        "LLM_BASE_URL",
        "MODEL_GATEWAY",
        "MODEL_ROUTER",
    ):
        with pytest.raises(ValueError, match="forbidden"):
            subscription_only_environment("claude", {key: "fake"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter", "stdout"),
    (
        (CodexCLIAdapter(), b""),
        (ClaudeCLIAdapter(), b'{"loggedIn":true}'),
    ),
)
async def test_authentication_adapter_uses_owned_probe(
    adapter: CodexCLIAdapter | ClaudeCLIAdapter,
    stdout: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_probe(command: list[str], **kwargs: Any) -> AuthProbeResult:
        captured["command"] = command
        captured.update(kwargs)
        return AuthProbeResult(0, stdout)

    module = type(adapter).__module__
    monkeypatch.setattr(f"{module}.run_auth_probe", fake_probe)

    assert await adapter.authenticated() is True
    assert captured["provider"] == adapter.provider.value
    assert captured["command"] == [
        adapter.executable,
        "auth" if adapter.provider.value == "claude" else "login",
        "status",
    ]


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


@pytest.mark.asyncio
async def test_jsonl_process_accepts_bounded_result_larger_than_asyncio_default_line_limit() -> (
    None
):
    value = "x" * (128 * 1024)
    script = "import json; value='x'*(128*1024); print(json.dumps({'type':'result','value':value}))"

    result = await run_jsonl_process(
        [sys.executable, "-c", script],
        prompt="bounded",
        cwd=os.getcwd(),
        timeout_seconds=30,
        provider="claude",
    )

    assert result.exit_code == 0
    assert result.events == ({"type": "result", "value": value},)
