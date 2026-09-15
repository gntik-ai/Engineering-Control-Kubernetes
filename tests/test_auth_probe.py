# These tests deliberately inspect audit-owned tmpfs files and /proc while
# coordinating real child processes; asyncio.Event cannot signal across them.
# ruff: noqa: ASYNC110, ASYNC240
import asyncio
import json
import sys
from pathlib import Path

import pytest

from engineering_control.adapters import auth_probe
from engineering_control.adapters.auth_probe import AuthProbeError, AuthProbeResult, run_auth_probe
from engineering_control.adapters.claude.client import ClaudeCLIAdapter
from engineering_control.adapters.codex.client import CodexCLIAdapter


@pytest.fixture
def executable(tmp_path: Path) -> str:
    target = tmp_path / "native-status"
    source = Path(__file__).parent / "fixtures/auth_status_probe.py"
    target.write_text(f"#!{sys.executable}\n" + source.read_text())
    target.chmod(0o700)
    return str(target)


def running(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0]
        return state not in ("Z", "X")
    except FileNotFoundError:
        return False


async def wait_marker(path: Path) -> int:
    async with asyncio.timeout(2):
        while not path.exists() or not path.read_text():
            await asyncio.sleep(0.01)
    return int(path.read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_type", [CodexCLIAdapter, ClaudeCLIAdapter])
async def test_real_adapter_native_status_success(
    executable: str, adapter_type: type[CodexCLIAdapter] | type[ClaudeCLIAdapter]
) -> None:
    assert await adapter_type(executable).authenticated() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("logged-out", False),
        ("conflict", False),
        ("authenticated", True),
        ("nonzero", False),
        ("stderr", True),
    ],
)
async def test_claude_status_requires_bounded_valid_boolean_output(
    executable: str, monkeypatch: pytest.MonkeyPatch, mode: str, expected: bool
) -> None:
    monkeypatch.setenv("AUTH_PROBE_FIXTURE_MODE", mode)
    assert await ClaudeCLIAdapter(executable).authenticated() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bad-json", "array", "string-true", "invalid-utf8"])
async def test_invalid_claude_response_is_unknown_not_logged_out(
    executable: str, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("AUTH_PROBE_FIXTURE_MODE", mode)
    with pytest.raises(AuthProbeError, match="INVALID_RESPONSE"):
        await ClaudeCLIAdapter(executable).authenticated()


@pytest.mark.asyncio
async def test_environment_filter_is_used_by_real_child(
    executable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_PROBE_FIXTURE_MODE", "environment")
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "MODEL_GATEWAY"):
        monkeypatch.setenv(key, "inert-fixture-value")
    result = await run_auth_probe(
        [executable, "auth", "status"], provider="claude", capture_stdout=True
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"forbidden_present": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sleep", "term-resistant", "early-stdout-close", "overflow"])
async def test_timeout_and_output_limit_kill_and_reap_owned_child(
    executable: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    marker = tmp_path / "pid"
    monkeypatch.setenv("AUTH_PROBE_FIXTURE_MODE", mode)
    monkeypatch.setenv("AUTH_PROBE_FIXTURE_PID", str(marker))
    started = asyncio.get_running_loop().time()
    result = await run_auth_probe(
        [executable, "auth", "status"],
        provider="claude",
        capture_stdout=True,
        timeout_seconds=0.3,
        max_stdout_bytes=512,
    )
    assert result.failure == ("OUTPUT_LIMIT" if mode == "overflow" else "TIMED_OUT")
    assert result.stdout == b"" and result.exit_code is None
    pid = await wait_marker(marker)
    assert not Path(f"/proc/{pid}").exists(), "direct child must be reaped before returning"
    assert asyncio.get_running_loop().time() - started < 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["child-held-pipe", "child-detached-output"])
async def test_descendant_is_terminated_even_after_leader_exits(
    executable: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    leader, child = tmp_path / "leader", tmp_path / "child"
    monkeypatch.setenv("AUTH_PROBE_FIXTURE_MODE", mode)
    monkeypatch.setenv("AUTH_PROBE_FIXTURE_PID", str(leader))
    monkeypatch.setenv("AUTH_PROBE_FIXTURE_CHILD_PID", str(child))
    result = await run_auth_probe(
        [executable, "auth", "status"], provider="claude", capture_stdout=True, timeout_seconds=0.3
    )
    assert result.failure == ("TIMED_OUT" if mode == "child-held-pipe" else None)
    assert not Path(f"/proc/{await wait_marker(leader)}").exists()
    child_pid = await wait_marker(child)
    async with asyncio.timeout(1):
        while running(child_pid):
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_repeated_cancellation_joins_cleanup_before_propagating(
    executable: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "pid"
    monkeypatch.setenv("AUTH_PROBE_FIXTURE_MODE", "term-resistant")
    monkeypatch.setenv("AUTH_PROBE_FIXTURE_PID", str(marker))
    task = asyncio.create_task(run_auth_probe([executable, "login", "status"], provider="codex"))
    pid = await wait_marker(marker)
    task.cancel()
    for _ in range(3):
        await asyncio.sleep(0.03)
        assert not task.done(), "cancellation must not detach the cleanup task"
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert not Path(f"/proc/{pid}").exists()


@pytest.mark.asyncio
async def test_cancellation_at_creation_handoff_still_owns_child(
    executable: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = auth_probe.subprocess.Popen
    spawned: list[int] = []
    task = asyncio.current_task()
    assert task is not None

    def cancel_after_spawn(*args: object, **kwargs: object) -> object:
        process = original(*args, **kwargs)  # type: ignore[call-overload]
        spawned.append(process.pid)
        task.cancel()
        return process

    monkeypatch.setattr(auth_probe.subprocess, "Popen", cancel_after_spawn)
    with pytest.raises(asyncio.CancelledError):
        await run_auth_probe([executable, "login", "status"], provider="codex")
    assert len(spawned) == 1 and not Path(f"/proc/{spawned[0]}").exists()


@pytest.mark.asyncio
async def test_missing_executable_is_not_false_authentication_success(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await run_auth_probe([str(tmp_path / "absent")], provider="codex")


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [0, -1, 31, float("nan"), float("inf")])
async def test_invalid_timeout_rejected_before_spawn(duration: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        await run_auth_probe(["must-not-launch"], provider="codex", timeout_seconds=duration)


def test_authentication_output_is_not_in_result_representation() -> None:
    assert "inert-private-output" not in repr(AuthProbeResult(0, b"inert-private-output"))
