from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from engineering_control.security.redaction import SecretRedactor

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    events: tuple[dict[str, Any], ...]
    stderr: str
    timed_out: bool = False


def subscription_only_environment(
    provider: str, overrides: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build an environment that cannot silently select API-paid provider credentials."""
    env = dict(os.environ)
    forbidden = {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
    for key in forbidden:
        env.pop(key, None)
    env["ENGINEERING_PROVIDER"] = provider
    env["NO_COLOR"] = "1"
    if overrides:
        disallowed = forbidden.intersection(overrides)
        if disallowed:
            raise ValueError(
                f"API-paid or third-party provider variables are forbidden: {sorted(disallowed)}"
            )
        env.update(overrides)
    return env


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def run_jsonl_process(
    command: Sequence[str],
    *,
    prompt: str,
    cwd: str,
    timeout_seconds: int,
    provider: str,
    on_event: EventCallback | None = None,
    environment: Mapping[str, str] | None = None,
    max_stderr_bytes: int = 256_000,
) -> ProcessResult:
    if not command or any(not isinstance(part, str) or "\x00" in part for part in command):
        raise ValueError("command must be a non-empty string sequence without NUL bytes")

    redactor = SecretRedactor()
    safe_prompt = str(redactor.redact_text(prompt).value)
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=subscription_only_environment(provider, environment),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdin = process.stdin
    stdout = process.stdout
    stderr_stream = process.stderr
    stdin.write(safe_prompt.encode("utf-8"))
    await stdin.drain()
    stdin.close()

    events: list[dict[str, Any]] = []
    stderr_chunks: list[bytes] = []
    stderr_size = 0

    async def consume_stdout() -> None:
        while line := await stdout.readline():
            report = redactor.redact_json_line(line.decode("utf-8", errors="replace"))
            try:
                import json

                event = json.loads(str(report.value))
            except (ValueError, TypeError):
                event = {"type": "provider.unstructured", "text": str(report.value).rstrip()}
            if not isinstance(event, dict):
                event = {"type": "provider.value", "value": event}
            events.append(event)
            if on_event is not None:
                await on_event(event)

    async def consume_stderr() -> None:
        nonlocal stderr_size
        pending = ""

        async def emit_visible(value: str) -> None:
            report = redactor.redact_text(value.rstrip("\r\n"))
            if on_event is not None and str(report.value):
                await on_event({"type": "provider.stderr", "text": str(report.value)})

        while chunk := await stderr_stream.read(8192):
            remaining = max_stderr_bytes - stderr_size
            if remaining > 0:
                stderr_chunks.append(chunk[:remaining])
                stderr_size += min(len(chunk), remaining)
            pending += chunk.decode("utf-8", errors="replace")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                await emit_visible(line)
            while len(pending.encode("utf-8")) > 16_384:
                encoded = pending.encode("utf-8")
                segment = encoded[:16_384].decode("utf-8", errors="ignore")
                await emit_visible(segment)
                pending = pending[len(segment) :]
        if pending:
            await emit_visible(pending)

    timed_out = False
    readers = asyncio.gather(consume_stdout(), consume_stderr())
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        await readers
    except TimeoutError:
        timed_out = True
        await _terminate_process_group(process)
        await readers
    except asyncio.CancelledError:
        await _terminate_process_group(process)
        readers.cancel()
        await asyncio.gather(readers, return_exceptions=True)
        raise

    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    stderr = str(redactor.redact_text(stderr).value)
    return ProcessResult(process.returncode or 0, tuple(events), stderr, timed_out)


def classify_failure(result: ProcessResult) -> tuple[str, str]:
    if result.timed_out:
        return "TIMED_OUT", "PROVIDER_TIMEOUT"
    combined = result.stderr.lower()
    if any(
        marker in combined
        for marker in (
            "rate limit",
            "rate_limit",
            "usage limit",
            "quota exceeded",
            "too many requests",
            "http 429",
        )
    ):
        return "RATE_LIMITED", "PROVIDER_RATE_LIMIT"
    return "FAILED", "PROVIDER_EXIT_NONZERO"
