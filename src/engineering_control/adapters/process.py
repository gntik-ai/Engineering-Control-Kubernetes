from __future__ import annotations

import asyncio
import os
import signal
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engineering_control.security.redaction import SecretRedactor

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
_PROVIDER_STREAM_LIMIT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    events: tuple[dict[str, Any], ...]
    stderr: str
    timed_out: bool = False


# NEW-79a: offline toolchains the scan worker prepares for makers and checkers
# (delivery.prepare_toolchain fills these caches with network; they are read-only here).
TOOLCHAIN_CACHE_ROOT = Path(
    os.environ.get("ENGINEERING_TOOLCHAIN_CACHE_ROOT", "/srv/engineering/cache/scan/toolchain")
)
GO_ROOT_BIN = Path("/usr/local/go/bin")


@dataclass(frozen=True)
class SandboxToolchain:
    """NEW-159: a project's non-default Node.js/pnpm selection for the agent sandbox.

    ``path_prefix`` directories go first on PATH; ``variables`` are added verbatim. The
    default toolchain (Node 22, worker pnpm) is represented by ``None``, never by an
    empty instance, so the default environment stays byte-identical.
    """

    path_prefix: tuple[str, ...]
    variables: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.path_prefix or any(
            not entry.startswith("/") or ":" in entry for entry in self.path_prefix
        ):
            raise ValueError("sandbox toolchain PATH entries must be absolute directories")
        if any(key == "PATH" for key, _ in self.variables):
            raise ValueError("sandbox toolchain variables cannot replace PATH")


def sandbox_toolchain_environment(
    base: Mapping[str, str], toolchain: SandboxToolchain | None = None
) -> dict[str, str]:
    """Go (module cache prefilled, no proxy) and Trivy (DB prefilled, offline) settings."""
    path = base.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if GO_ROOT_BIN.is_dir() and str(GO_ROOT_BIN) not in path.split(":"):
        path = f"{GO_ROOT_BIN}:{path}"
    extra: dict[str, str] = {}
    if toolchain is not None:
        # NEW-159: the project's selected node and pnpm win over /usr/local/bin.
        path = ":".join((*toolchain.path_prefix, path))
        extra = dict(toolchain.variables)
    return {
        "PATH": path,
        "GOMODCACHE": str(TOOLCHAIN_CACHE_ROOT / "go-mod"),
        # The sandbox may write only the worktree and the temp directory.
        "GOCACHE": str(Path(tempfile.gettempdir()) / f"go-build-{os.getuid()}"),
        "GOPROXY": "off",
        "GOTOOLCHAIN": "local",
        "GOTELEMETRY": "off",
        # Two parallel compile jobs: the worker has little RAM (NEW-77 OOM kills).
        "GOFLAGS": "-mod=mod -p=2",
        "TRIVY_CACHE_DIR": str(TOOLCHAIN_CACHE_ROOT / "trivy"),
        "TRIVY_SKIP_DB_UPDATE": "true",
        "TRIVY_SKIP_JAVA_DB_UPDATE": "true",
        "TRIVY_OFFLINE_SCAN": "true",
        "TRIVY_CACHE_BACKEND": "memory",
        "TRIVY_NO_PROGRESS": "true",
        **extra,
    }


def subscription_only_environment(
    provider: str,
    overrides: Mapping[str, str] | None = None,
    *,
    toolchain: SandboxToolchain | None = None,
) -> dict[str, str]:
    """Build an environment that cannot silently select API-paid provider credentials."""
    env = dict(os.environ)
    forbidden = {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "OMNIROUTE_URL",
        "OMNIROUTE_BASE_URL",
        "LLM_BASE_URL",
        "MODEL_GATEWAY",
        "MODEL_ROUTER",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
    for key in forbidden:
        env.pop(key, None)
    env["ENGINEERING_PROVIDER"] = provider
    env["NO_COLOR"] = "1"
    # NEW-45: makers/checkers run the project's tests in shared worktrees. pytest
    # creates .pytest_cache through a 0700 temp directory, so the scan worker's next
    # `git clean` of that worktree failed with "Permission denied". No cache, no
    # bytecode: nothing an agent's test run leaves behind is unremovable.
    env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # NEW-48: a worktree .mypy_cache written by the maker is not writable by the
    # checker (another runner uid). /dev/null is mypy's documented "no cache".
    env["MYPY_CACHE_DIR"] = "/dev/null"
    env.update(sandbox_toolchain_environment(env, toolchain))
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
    toolchain: SandboxToolchain | None = None,
) -> ProcessResult:
    if not command or any(not isinstance(part, str) or "\x00" in part for part in command):
        raise ValueError("command must be a non-empty string sequence without NUL bytes")

    redactor = SecretRedactor()
    safe_prompt = str(redactor.redact_text(prompt).value)
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=subscription_only_environment(provider, environment, toolchain=toolchain),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=_PROVIDER_STREAM_LIMIT_BYTES,
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
        while True:
            try:
                line = await stdout.readline()
            except ValueError as error:
                raise RuntimeError(
                    "provider JSONL event exceeds the bounded stream limit"
                ) from error
            if not line:
                break
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


# Error code of a transient provider-capacity failure ("Selected model is at capacity").
# The provider activity retries the same invocation (same provider, same model) with a
# bounded backoff before it reports the failure; nothing ever switches provider or model.
PROVIDER_CAPACITY_ERROR = "PROVIDER_MODEL_AT_CAPACITY"
_CAPACITY_MARKERS = (
    "selected model is at capacity",
    "model is at capacity",
    "model is currently at capacity",
    "overloaded_error",
)


def _error_event_text(events: tuple[dict[str, Any], ...]) -> str:
    """Text of the provider's own error events only (never agent messages or output)."""
    parts: list[str] = []
    for event in events[-50:]:
        event_type = str(event.get("type", ""))
        if event_type == "result":
            # Claude's final envelope: only an explicit error result counts; a successful
            # result carries agent output and is never inspected.
            if event.get("is_error") is not True:
                continue
            keys: tuple[str, ...] = ("errors", "subtype", "result", "error")
        elif "error" in event or "error" in event_type or event_type.endswith(".failed"):
            keys = ("error", "message", "detail")
        else:
            continue
        for key in keys:
            value = event.get(key)
            if isinstance(value, list):
                parts.extend(str(item)[:2000] for item in value[:20])
            elif value is not None:
                parts.append(str(value)[:2000])
    return "\n".join(parts).lower()


def is_capacity_failure(result: ProcessResult) -> bool:
    if result.timed_out or result.exit_code == 0:
        return False
    text = f"{result.stderr.lower()}\n{_error_event_text(result.events)}"
    return any(marker in text for marker in _CAPACITY_MARKERS)


def classify_failure(result: ProcessResult) -> tuple[str, str]:
    if result.timed_out:
        return "TIMED_OUT", "PROVIDER_TIMEOUT"
    if is_capacity_failure(result):
        return "FAILED", PROVIDER_CAPACITY_ERROR
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
