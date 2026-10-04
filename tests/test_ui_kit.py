"""NEW-121 v2: the Hermes-side, model-free gntik-ui kit client and `ui.kit_snapshot`."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket as _socket
import stat
import sys
import threading
from argparse import Namespace
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.cli import _ui_kit_snapshot_command
from engineering_control.config import Settings
from engineering_control.engineering import ui_kit
from engineering_control.engineering.models import ProjectManifest, UiMigrationPolicy
from engineering_control.engineering.safe_files import write_text_no_follow
from engineering_control.engineering.ui_kit import (
    READ_ONLY_TOOLS,
    GntikUiKitClient,
    GntikUiSocketClient,
    KitProtocolError,
    KitToolRefused,
    KitUnavailable,
    collect_kit_snapshot,
    kit_command,
    snapshot_hash,
    snapshot_plan,
    socket_preflight,
    trusted_docker_executable,
    validate_page,
)
from engineering_control.workflows.definitions import ControlledWorkflow
from engineering_control.workflows.worker_registry import ROLE_WORKER_REGISTRY, task_queue_for

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "config" / "projects"
STUB = ROOT / "tests" / "fixtures" / "ui_kit_stub_server.py"
IMAGE = "ghcr.io/gntik-ai/gntik-ui-mcp"
DIGEST = "sha256:4613adab5939872306a11d8b428e6cab14ace498855212f20a8859a5b7e1ef63"
VERSION = "0.0.0-canary-20261003230037"


def _policy() -> UiMigrationPolicy:
    value = yaml.safe_load((PROJECTS / "falcone.yaml").read_text(encoding="utf-8"))
    policy = ProjectManifest.model_validate(value).ui_migration
    assert policy is not None
    return policy


def _stub(tmp_path: Path, mode: str = "ok", **kwargs: Any) -> tuple[GntikUiKitClient, Path]:
    log = tmp_path / f"{mode}.log"
    client = GntikUiKitClient(
        [sys.executable, str(STUB), mode, str(log)],
        environment={"PATH": "/usr/bin:/bin"},
        **kwargs,
    )
    return client, log


def _log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); it must at least not be running.
    status = Path(f"/proc/{pid}/stat")
    return status.exists() and status.read_text().split()[2] != "Z"


# -- the pinned launch -------------------------------------------------------------------


SOCKET = "unix:///var/run/docker.sock"


def test_the_container_command_is_pinned_offline_and_unprivileged() -> None:
    assert kit_command(
        "/usr/bin/docker",
        IMAGE,
        DIGEST,
        name="hermes-ui-kit-abc",
        endpoint=SOCKET,
        config_dir="/run/kit-config",
    ) == [
        "/usr/bin/docker",
        "--config",
        "/run/kit-config",
        "-H",
        SOCKET,
        "run",
        "-i",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--name",
        "hermes-ui-kit-abc",
        f"{IMAGE}@{DIGEST}",
        "--stdio",
    ]
    for docker, image, digest, endpoint in (
        ("docker", IMAGE, DIGEST, SOCKET),  # never a PATH lookup
        ("/usr/bin/docker", f"{IMAGE}:latest", DIGEST, SOCKET),
        ("/usr/bin/docker", IMAGE, "sha256:abc", SOCKET),
        ("/usr/bin/docker", IMAGE, "latest", SOCKET),
        ("/usr/bin/docker", IMAGE, DIGEST, "tcp://10.0.0.1:2375"),
        ("/usr/bin/docker", IMAGE, DIGEST, "ssh://host"),
    ):
        with pytest.raises(ValueError):
            kit_command(
                docker, image, digest, name="hermes-ui-kit-abc", endpoint=endpoint, config_dir="/c"
            )


def test_pinned_client_uses_an_explicit_endpoint_and_a_private_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    client = GntikUiKitClient.pinned(IMAGE, DIGEST, docker_executable="/usr/bin/docker")
    try:
        (config,) = client.private_dirs
        assert client.cwd == "/"
        assert client.environment == {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": config,
            "DOCKER_CONFIG": config,
            "DOCKER_CONTEXT": "default",
            "DOCKER_HOST": SOCKET,
        }
        metadata = os.lstat(config)
        assert stat.S_IMODE(metadata.st_mode) == 0o700 and metadata.st_uid == os.geteuid()
        assert os.listdir(config) == []
        flags = ["/usr/bin/docker", "--config", config, "-H", SOCKET]
        assert client.argv[:5] == flags
        assert client.argv[-2:] == [f"{IMAGE}@{DIGEST}", "--stdio"]
        assert client.preflight == [
            *flags,
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            client.argv[-2],
        ]
        name = client.argv[client.argv.index("--name") + 1]
        assert client.cleanup_argv == [*flags, "rm", "-f", name]
    finally:
        for directory in client.private_dirs:
            os.rmdir(directory)


def test_only_a_local_docker_socket_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ui_kit.docker_endpoint({}) == SOCKET
    assert ui_kit.docker_endpoint({"DOCKER_HOST": "unix:///run/hermes/docker-scan.sock"}) == (
        "unix:///run/hermes/docker-scan.sock"
    )
    for host in ("tcp://10.0.0.1:2375", "ssh://x", "unix:///run/../tmp/s", "npipe:////./pipe"):
        with pytest.raises(KitUnavailable, match="non-local docker endpoint"):
            ui_kit.docker_endpoint({"DOCKER_HOST": host})
    monkeypatch.setenv("DOCKER_HOST", "tcp://10.0.0.1:2375")
    with pytest.raises(KitUnavailable):
        GntikUiKitClient.pinned(IMAGE, DIGEST, docker_executable="/usr/bin/docker")


def _docker_usable() -> bool:
    docker = trusted_docker_executable()
    if docker is None:
        return False
    import subprocess

    try:
        return (
            subprocess.run(  # noqa: S603
                [docker, "-H", SOCKET, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                timeout=20,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _docker_usable(), reason="no usable local docker daemon")
async def test_a_planted_docker_context_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user config.json selecting a TCP context (and DOCKER_CONTEXT in the worker env)
    must not redirect the kit's docker invocations away from the local socket."""
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "config.json").write_text(json.dumps({"currentContext": "evil"}), "utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKER_CONFIG", str(planted))
    monkeypatch.setenv("DOCKER_CONTEXT", "evil")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    client = GntikUiKitClient.pinned(IMAGE, DIGEST)
    (config,) = client.private_dirs
    docker = client.argv[0]
    process = await asyncio.create_subprocess_exec(
        docker,
        "--config",
        config,
        "-H",
        SOCKET,
        "version",
        "--format",
        "{{.Server.Version}}",
        env=client.environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr  # the "evil" context would not even resolve
    assert os.listdir(config) == []
    await client.close(force=True)
    assert not os.path.exists(config)  # noqa: ASYNC240


def test_docker_must_be_absolute_and_root_owned(tmp_path: Path) -> None:
    planted = tmp_path / "docker"
    planted.write_text("#!/bin/sh\n", encoding="utf-8")
    planted.chmod(0o755)
    assert trusted_docker_executable([str(planted)]) is None
    assert trusted_docker_executable(["docker"]) is None
    resolved = trusted_docker_executable(["/bin/sh"])
    assert resolved is not None and Path(resolved).is_absolute()


def test_missing_docker_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui_kit, "trusted_docker_executable", lambda: None)
    with pytest.raises(KitUnavailable, match="no root-owned docker CLI"):
        GntikUiKitClient.pinned(IMAGE, DIGEST)
    assert KitUnavailable.code == "UI_MCP_UNAVAILABLE"


async def test_a_missing_image_fails_closed_before_any_container_starts(tmp_path: Path) -> None:
    calls = tmp_path / "docker-calls"
    docker = tmp_path / "docker"
    docker.write_text(
        f'#!/bin/sh\necho "$5" >> {calls}\necho "Error: No such image: x" >&2\nexit 1\n',
        encoding="utf-8",
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    client = GntikUiKitClient.pinned(IMAGE, DIGEST, docker_executable=str(docker))
    with pytest.raises(KitUnavailable, match="is not pre-pulled"):
        async with client:
            pass
    # inspect only (after --config <dir> -H <socket>): no run, no rm
    assert calls.read_text(encoding="utf-8").split() == ["image"]
    assert not any(os.path.exists(directory) for directory in client.private_dirs)  # noqa: ASYNC240


# -- the session -------------------------------------------------------------------------


async def test_only_read_only_allowlisted_tools_can_be_called(tmp_path: Path) -> None:
    assert READ_ONLY_TOOLS == {
        "list_kit",
        "get_component",
        "get_block",
        "get_template",
        "get_app_shell",
        "get_tokens",
        "get_rules",
        "get_adoption_guide",
        "analyze_page",
        "validate_page",
    }
    client, log = _stub(tmp_path)
    async with client:
        for tool in ("sync_design_system", "scaffold_page", "overview", "rm_rf"):
            with pytest.raises(KitToolRefused):
                await client.call(tool)
        assert (await client.call("get_rules"))["text"] == "ruleone"  # NUL removed
    called = [entry["params"]["name"] for entry in _log(log) if entry["method"] == "tools/call"]
    assert called == ["get_rules"]


async def test_snapshot_is_versioned_bounded_and_deterministic(tmp_path: Path) -> None:
    plan = snapshot_plan(templates=["home-dashboard"], components=["button"])
    documents = []
    for attempt in range(2):
        client, _log_path = _stub(tmp_path / str(attempt))
        (tmp_path / str(attempt)).mkdir()
        async with client:
            documents.append(
                await collect_kit_snapshot(
                    client, image=IMAGE, digest=DIGEST, gntik_ui_version=VERSION, plan=plan
                )
            )
    first, second = documents
    assert first == second
    assert first["schema_version"] == 1
    assert first["image"] == f"{IMAGE}@{DIGEST}"
    assert first["gntik_ui_version"] == VERSION
    assert first["server_info"] == {"name": "gntik-ui", "version": "0.1.0"}
    assert len(first["tools_available"]) == 16
    assert first["snapshot_sha256"] == snapshot_hash(first)
    calls = {
        (call["tool"], json.dumps(call["arguments"], sort_keys=True)): call
        for call in first["calls"]
    }
    assert len(first["calls"]) == len(plan) == 10
    tokens = calls[("get_tokens", '{"format": "json", "theme": "all"}')]
    assert tokens["truncated"] is True and tokens["bytes"] == 512 * 1024
    template = calls[("get_template", '{"id": "home-dashboard", "include_source": true}')]
    assert template["text"].startswith("# get_template ")
    assert first["summary"] == {
        "calls": 10,
        "errors": 0,
        "truncated": 1,
        "text_bytes": sum(call["bytes"] for call in first["calls"]),
    }


def test_snapshot_plan_refuses_unsafe_ids() -> None:
    for bad in ("../x", "a b", "", "x" * 81):
        with pytest.raises(ValueError):
            snapshot_plan(templates=[bad])


async def test_validate_page_is_a_bounded_deterministic_check(tmp_path: Path) -> None:
    client, log = _stub(tmp_path)
    async with client:
        result = await validate_page(client, "export const P = 1", filename="pages/P.tsx")
        with pytest.raises(ValueError):
            await validate_page(client, "x" * (256 * 1024 + 1))
        with pytest.raises(ValueError):
            await validate_page(client, "x", filename="../P.tsx")
        analyzed = await client.call("analyze_page", {"source": "x"})
    assert result["tool"] == "validate_page"
    assert result["is_error"] is False
    assert analyzed["is_error"] is True
    sent = [entry["params"] for entry in _log(log) if entry["method"] == "tools/call"]
    assert sent[0] == {
        "name": "validate_page",
        "arguments": {"source": "export const P = 1", "filename": "pages/P.tsx"},
    }


async def test_timeout_kills_and_reaps_the_whole_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "cleanup-ran"
    client, log = _stub(
        tmp_path,
        "hang",
        deadline_seconds=1.5,
        cleanup=["/bin/sh", "-c", f"touch {marker}"],
    )
    with pytest.raises(TimeoutError):
        async with client:
            pass
    (entry,) = _log(log)
    assert client.process is not None and client.process.returncode is not None
    assert not _alive(entry["server"])
    assert not _alive(entry["grandchild"])  # the stub's own child, same process group
    assert marker.exists()  # the named container is removed too


async def test_cancellation_kills_and_reaps_the_whole_process_group(tmp_path: Path) -> None:
    client, log = _stub(tmp_path, "hang", deadline_seconds=60)

    async def session() -> None:
        async with client:
            pass

    task = asyncio.create_task(session())
    for _ in range(100):
        if _log(log):
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (entry,) = _log(log)
    assert not _alive(entry["server"])
    assert not _alive(entry["grandchild"])


async def test_a_closed_or_broken_server_is_a_protocol_error(tmp_path: Path) -> None:
    client = GntikUiKitClient(["/bin/true"], environment={"PATH": "/usr/bin:/bin"})
    with pytest.raises(KitProtocolError, match="closed"):
        async with client:
            pass
    with pytest.raises(ValueError, match="absolute"):
        GntikUiKitClient(["docker", "run"], environment={})


# -- the scan step and the CLI -----------------------------------------------------------


def test_kit_snapshot_step_runs_on_the_scan_worker_only() -> None:
    owners = {
        kind
        for kind in ("controller", *ROLE_WORKER_REGISTRY)
        if "ui.kit_snapshot" in ControlActivities.steps_for_worker(kind)
    }
    assert owners == {"scan"}
    assert "ui.kit_snapshot" in ScanActivityHandler.supported_steps
    assert ControlledWorkflow._activity_queue("ui.kit_snapshot", {}) == task_queue_for("scan")


def _handler(tmp_path: Path, factory: Any) -> ScanActivityHandler:
    settings = Settings(
        worktree_root=tmp_path / "worktrees",
        evidence_root=tmp_path / "evidence",
        project_config_root=PROJECTS,
    )
    return ScanActivityHandler(settings, kit_client_factory=factory)


async def test_kit_snapshot_step_stores_evidence_and_returns_a_summary(tmp_path: Path) -> None:
    seen: list[UiMigrationPolicy] = []

    def factory(policy: UiMigrationPolicy) -> GntikUiKitClient:
        seen.append(policy)
        return _stub(tmp_path)[0]

    result = await _handler(tmp_path, factory).handle(
        "ui.kit_snapshot", {"project": "falcone", "templates": ["home-dashboard"]}
    )
    assert seen == [_policy()]
    assert result["status"] == "UI_KIT_SNAPSHOT_PASS"
    assert result["image"] == f"{IMAGE}@{DIGEST}"
    assert result["gntik_ui_version"] == VERSION
    assert "calls" not in result
    stored = tmp_path / "evidence/scan/ui-kit" / result["snapshot_sha256"][:32]
    document = json.loads((stored / "ui-kit-snapshot.json").read_text(encoding="utf-8"))
    assert document["snapshot_sha256"] == result["snapshot_sha256"]
    assert [receipt["status"] for receipt in result["evidence"]] == ["LOCAL_DEVELOPMENT_ONLY"]


async def test_kit_snapshot_step_is_blocked_when_docker_or_the_image_is_missing(
    tmp_path: Path,
) -> None:
    def unavailable(policy: UiMigrationPolicy) -> GntikUiKitClient:
        raise KitUnavailable("image ghcr.io/gntik-ai/gntik-ui-mcp@sha256:... is not pre-pulled")

    result = await _handler(tmp_path, unavailable).handle("ui.kit_snapshot", {"project": "falcone"})
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "UI_MCP_UNAVAILABLE"
    assert "not pre-pulled" in result["reason"]
    assert not (tmp_path / "evidence").exists()


async def test_kit_snapshot_step_refuses_bad_selections(tmp_path: Path) -> None:
    from temporalio.exceptions import ApplicationError

    handler = _handler(tmp_path, lambda policy: _stub(tmp_path)[0])
    for payload in (
        {"project": "musematic"},
        {"project": "falcone", "templates": "home-dashboard"},
        {"project": "falcone", "templates": ["../etc"]},
    ):
        with pytest.raises(ApplicationError):
            await handler.handle("ui.kit_snapshot", payload)


def test_cli_writes_a_local_snapshot_or_reports_the_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = Namespace(
        project="falcone",
        manifest=PROJECTS / "falcone.yaml",
        templates=["home-dashboard"],
        blocks=[],
        components=[],
        output_dir=tmp_path / "out",
    )
    monkeypatch.setattr(
        GntikUiKitClient, "from_policy", classmethod(lambda cls, policy: _stub(tmp_path)[0])
    )
    result = _ui_kit_snapshot_command(arguments)
    assert result["status"] == "UI_KIT_SNAPSHOT_PASS"
    (path,) = result["outputs"]
    assert (
        json.loads(Path(path).read_text(encoding="utf-8"))["snapshot_sha256"]
        == (result["snapshot_sha256"])
    )

    def unavailable(cls: Any, policy: UiMigrationPolicy) -> GntikUiKitClient:
        raise KitUnavailable("the docker daemon is unreachable")

    monkeypatch.setattr(GntikUiKitClient, "from_policy", classmethod(unavailable))
    assert _ui_kit_snapshot_command(arguments) == {
        "status": "BLOCKED",
        "blocker": "UI_MCP_UNAVAILABLE",
        "reason": "the docker daemon is unreachable",
    }


# -- review round 3 ----------------------------------------------------------------------


async def test_a_killed_cli_always_removes_the_named_container(tmp_path: Path) -> None:
    marker = tmp_path / "rm-ran"
    client, log = _stub(
        tmp_path,
        "linger",  # answers, then keeps running after stdin closes
        shutdown_seconds=0.5,
        cleanup=["/bin/sh", "-c", f"touch {marker}"],
    )
    async with client:
        await client.call("get_rules")
    assert client.process is not None and client.process.returncode == -9
    assert client.removed_container is True
    assert marker.exists()
    eof = next(entry["eof"] for entry in _log(log) if "eof" in entry)
    assert not _alive(eof)


async def test_a_clean_exit_needs_no_container_removal(tmp_path: Path) -> None:
    marker = tmp_path / "rm-ran"
    client, _log_path = _stub(tmp_path, cleanup=["/bin/sh", "-c", f"touch {marker}"])
    async with client:
        await client.call("get_rules")
    assert client.process is not None and client.process.returncode == 0
    assert client.removed_container is False and not marker.exists()


async def test_cancellation_during_exit_completes_every_cleanup_step(tmp_path: Path) -> None:
    marker = tmp_path / "rm-ran"
    private = tmp_path / "private-config"
    private.mkdir()
    client, log = _stub(
        tmp_path,
        "linger",
        shutdown_seconds=60,  # the graceful wait would block for a minute
        cleanup=["/bin/sh", "-c", f"touch {marker}"],
        private_dirs=[str(private)],
    )

    async def session() -> None:
        async with client:
            await client.call("get_rules")

    task = asyncio.create_task(session())
    for _ in range(200):  # until __aexit__ closed stdin and is waiting
        if any("eof" in entry for entry in _log(log)):
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=20)
    eof = next(entry["eof"] for entry in _log(log) if "eof" in entry)
    assert not _alive(eof)
    assert client.process is not None and client.process.returncode is not None
    assert client._stderr_task is None
    assert marker.exists() and client.removed_container is True
    assert not private.exists()


async def test_snapshot_text_is_redacted_before_it_is_hashed(tmp_path: Path) -> None:
    client, _log_path = _stub(tmp_path)
    async with client:
        document = await collect_kit_snapshot(
            client,
            image=IMAGE,
            digest=DIGEST,
            gntik_ui_version=VERSION,
            plan=[("get_adoption_guide", {})],
        )
    (call,) = document["calls"]
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in call["text"]
    assert "Bearer [REDACTED]" in call["text"] or "[REDACTED]" in call["text"]
    data = call["text"].encode("utf-8")
    assert call["sha256"] == hashlib.sha256(data).hexdigest() and call["bytes"] == len(data)
    assert document["snapshot_sha256"] == snapshot_hash(document)


async def test_stored_evidence_verifies_against_the_reported_digest(tmp_path: Path) -> None:
    result = await _handler(tmp_path, lambda policy: _stub(tmp_path)[0]).handle(
        "ui.kit_snapshot", {"project": "falcone"}
    )
    stored_path = (
        tmp_path / "evidence/scan/ui-kit" / result["snapshot_sha256"][:32] / "ui-kit-snapshot.json"
    )
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    assert snapshot_hash(stored) == stored["snapshot_sha256"] == result["snapshot_sha256"]
    for call in stored["calls"]:
        assert call["sha256"] == hashlib.sha256(call["text"].encode("utf-8")).hexdigest()
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in stored_path.read_text(encoding="utf-8")


async def test_server_controlled_fields_are_bounded_in_the_workflow_result(
    tmp_path: Path,
) -> None:
    result = await _handler(tmp_path, lambda policy: _stub(tmp_path, "manytools")[0]).handle(
        "ui.kit_snapshot", {"project": "falcone"}
    )
    assert result["tools_available_count"] == 20016
    assert len(result["tools_available"]) <= 64
    assert all(len(name) <= 64 for name in result["tools_available"])
    assert len(json.dumps(result).encode("utf-8")) < 16 * 1024
    stored_path = (
        tmp_path / "evidence/scan/ui-kit" / result["snapshot_sha256"][:32] / "ui-kit-snapshot.json"
    )
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    assert len(stored["tools_available"]) == 20016  # the full list is evidence only


def test_evidence_and_cli_writes_never_follow_a_destination_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = tmp_path / "checkout-file.ts"
    victim.write_text("original\n", encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    (output / "target.json").symlink_to(victim)
    with pytest.raises(ValueError, match="symlink"):
        write_text_no_follow(output / "target.json", "overwritten\n")
    assert victim.read_text(encoding="utf-8") == "original\n"
    write_text_no_follow(output / "fresh.json", "ok\n")
    assert (output / "fresh.json").read_text(encoding="utf-8") == "ok\n"
    assert [path.name for path in output.iterdir() if path.name.endswith(".tmp")] == []
    linked_dir = tmp_path / "linked-dir"
    linked_dir.symlink_to(output, target_is_directory=True)
    with pytest.raises(OSError):
        write_text_no_follow(linked_dir / "fresh.json", "x\n")

    # The kit CLI refuses a planted symlink at its output name.
    monkeypatch.setattr(
        GntikUiKitClient, "from_policy", classmethod(lambda cls, policy: _stub(tmp_path)[0])
    )
    arguments = Namespace(
        project="falcone",
        manifest=PROJECTS / "falcone.yaml",
        templates=[],
        blocks=[],
        components=[],
        output_dir=tmp_path / "kit-out",
    )
    first = _ui_kit_snapshot_command(arguments)
    (path,) = first["outputs"]
    Path(path).unlink()
    Path(path).symlink_to(victim)
    with pytest.raises(ValueError, match="symlink"):
        _ui_kit_snapshot_command(arguments)
    assert victim.read_text(encoding="utf-8") == "original\n"


# -- review round 4 (orchestrator) -------------------------------------------------------


async def test_cancellation_before_the_process_is_assigned_still_removes_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "rm-ran"
    client, _log_path = _stub(tmp_path, cleanup=["/bin/sh", "-c", f"touch {marker}"])
    real_exec = asyncio.create_subprocess_exec
    calls = {"n": 0}

    async def spawn_then_cancel(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if args and "touch" in " ".join(map(str, args)):
            return await real_exec(*args, **kwargs)  # the cleanup command itself
        raise asyncio.CancelledError  # the CLI was spawned; `self.process` never assigned

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        async with client:
            await client.call("get_rules")
    assert client.process is None
    assert client.removed_container is True and marker.exists()


def test_evidence_directories_refuse_a_symlink_at_any_component(tmp_path: Path) -> None:
    from engineering_control.engineering.safe_files import open_directory_no_follow

    root = tmp_path / "evidence"
    (root / "scan").mkdir(parents=True)
    outside = tmp_path / "checkout"
    outside.mkdir()
    (root / "scan" / "ui-kit").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        open_directory_no_follow(root, root / "scan" / "ui-kit" / "abc")
    assert not (outside / "abc").exists()
    os.close(open_directory_no_follow(root, root / "scan" / "ui-inventory" / "abc"))
    assert (root / "scan" / "ui-inventory" / "abc").is_dir()
    with pytest.raises(ValueError):
        open_directory_no_follow(root, root / "scan" / ".." / "x")


def test_writes_stay_in_the_verified_directory_after_a_parent_swap(tmp_path: Path) -> None:
    from engineering_control.engineering.safe_files import open_directory_no_follow, write_text_at

    root = tmp_path / "evidence"
    target = root / "scan" / "ui-kit" / "abc"
    verified = open_directory_no_follow(root, target)
    try:
        # After verification a parent is swapped for a symlink to a checkout that has the
        # same digest directory: writes must still land in the verified directory.
        outside = tmp_path / "checkout"
        (outside / "abc").mkdir(parents=True)
        (root / "scan" / "ui-kit").rename(tmp_path / "moved")
        (root / "scan" / "ui-kit").symlink_to(outside, target_is_directory=True)
        write_text_at(verified, "ui-kit-snapshot.json", "{}\n")
        with pytest.raises(ValueError):
            write_text_at(verified, "../escape.json", "x\n")
    finally:
        os.close(verified)
    assert (tmp_path / "moved" / "abc" / "ui-kit-snapshot.json").read_text() == "{}\n"
    assert not (outside / "abc" / "ui-kit-snapshot.json").exists()


# -- NEW-129: the root-owned service socket ----------------------------------------------


SERVICE_SOCKET = "/run/gntik-ui-mcp/mcp.sock"


class _SocketService:
    """A stand-in for gntik-ui-mcp.socket (Accept=yes): each connection becomes the stdin
    and stdout of one stub server process, exactly like StandardInput/Output=socket."""

    def __init__(self, path: Path, mode: str, log: Path) -> None:
        self.path, self.mode, self.log = path, mode, log
        self.processes: list[Any] = []
        self.eof = threading.Event()
        self.release = threading.Event()
        self.listener = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        import subprocess

        while True:
            try:
                connection, _address = self.listener.accept()
            except OSError:
                return
            if self.mode == "silent":  # never answers; records the client's close
                threading.Thread(target=self._silent, args=(connection,), daemon=True).start()
                continue
            if self.mode == "stall":  # answers the handshake, then stops reading
                threading.Thread(target=self._stall, args=(connection,), daemon=True).start()
                continue
            if self.mode == "oversize":
                threading.Thread(target=self._oversize, args=(connection,), daemon=True).start()
                continue
            self.processes.append(
                subprocess.Popen(  # noqa: S603 - the fixed stub server
                    [sys.executable, str(STUB), self.mode, str(self.log)],
                    stdin=connection.fileno(),
                    stdout=connection.fileno(),
                    start_new_session=True,
                )
            )
            connection.close()

    def _silent(self, connection: _socket.socket) -> None:
        with connection:
            while connection.recv(65536):
                pass
        self.eof.set()

    def _stall(self, connection: _socket.socket) -> None:
        with connection:
            stream = connection.makefile("rb")
            for request_id in (1, 2):  # initialize, then (after the notification) tools/list
                while json.loads(stream.readline()).get("id") != request_id:
                    pass
                result: dict[str, Any] = (
                    {"protocolVersion": "2025-06-18", "serverInfo": {"name": "gntik-ui"}}
                    if request_id == 1
                    else {"tools": [{"name": "get_rules"}]}
                )
                reply = {"jsonrpc": "2.0", "id": request_id, "result": result}
                connection.sendall(json.dumps(reply).encode() + b"\n")
            self.release.wait(60)  # a peer that does not read: the client's output stays buffered
            with contextlib.suppress(OSError):
                while connection.recv(1 << 20):
                    pass
        self.eof.set()

    def _oversize(self, connection: _socket.socket) -> None:
        with connection:
            connection.recv(65536)
            with contextlib.suppress(OSError):
                connection.sendall(b'{"jsonrpc":"2.0","id":1,"x":"' + b"x" * (5 << 20) + b'"}\n')
            while connection.recv(65536):
                pass
        self.eof.set()

    def stop(self) -> None:
        self.release.set()
        self.listener.close()
        for process in self.processes:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, 9)
            process.wait(timeout=10)


@contextmanager
def _service(tmp_path: Path, mode: str = "ok") -> Iterator[tuple[_SocketService, str]]:
    directory = tmp_path / "run"
    directory.mkdir(mode=0o750)
    directory.chmod(0o750)
    path = directory / "mcp.sock"
    service = _SocketService(path, mode, tmp_path / f"{mode}.log")
    try:
        yield service, str(path)
    finally:
        service.stop()


def _socket_client(path: str, **kwargs: Any) -> GntikUiSocketClient:
    return GntikUiSocketClient(path, image=IMAGE, digest=DIGEST, trusted_uid=os.geteuid(), **kwargs)


def test_manifest_accepts_only_a_normalised_absolute_socket_path() -> None:
    policy = _policy()
    assert policy.mcp.socket == SERVICE_SOCKET
    value = policy.model_dump(mode="json")
    value["mcp"].pop("socket")
    assert UiMigrationPolicy.model_validate(value).mcp.socket is None
    for bad in (
        "run/gntik-ui-mcp/mcp.sock",
        "/run/gntik-ui-mcp/../mcp.sock",
        "/run/./mcp.sock",
        "/run//mcp.sock",
        "/run/gntik-ui-mcp/",
        "/run/a b.sock",
        "/" + "x" * 120,
        "",
    ):
        value["mcp"]["socket"] = bad
        with pytest.raises(ValueError, match="socket"):
            UiMigrationPolicy.model_validate(value)


def test_a_configured_socket_never_runs_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_docker(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("docker must not be used with a service socket")

    monkeypatch.setattr(ui_kit, "trusted_docker_executable", no_docker)
    monkeypatch.setattr(GntikUiKitClient, "pinned", classmethod(no_docker))
    client = GntikUiKitClient.from_policy(_policy())
    assert isinstance(client, GntikUiSocketClient)
    assert client.path == SERVICE_SOCKET and client.trusted_uid == 0
    assert client.transport["expected"] == {"image": f"{IMAGE}@{DIGEST}", "digest": DIGEST}


def test_without_a_socket_the_docker_transport_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _policy().model_dump(mode="json")
    value["mcp"].pop("socket")
    policy = UiMigrationPolicy.model_validate(value)
    seen: list[tuple[str, str]] = []

    def pinned(cls: Any, image: str, digest: str, **kwargs: Any) -> str:
        seen.append((image, digest))
        return "pinned"

    monkeypatch.setattr(GntikUiKitClient, "pinned", classmethod(pinned))
    assert GntikUiKitClient.from_policy(policy) == "pinned"
    assert seen == [(IMAGE, DIGEST)]


async def test_socket_session_snapshot_matches_the_docker_one(tmp_path: Path) -> None:
    plan = snapshot_plan(templates=["home-dashboard"], components=["button"])
    (tmp_path / "d").mkdir()
    docker_client, _log_path = _stub(tmp_path / "d")
    async with docker_client:
        via_docker = await collect_kit_snapshot(
            docker_client, image=IMAGE, digest=DIGEST, gntik_ui_version=VERSION, plan=plan
        )
    with _service(tmp_path) as (service, path):
        client = _socket_client(path)
        async with client:
            via_socket = await collect_kit_snapshot(
                client, image=IMAGE, digest=DIGEST, gntik_ui_version=VERSION, plan=plan
            )
        (process,) = service.processes
        assert process.wait(timeout=10) == 0  # the server saw EOF: the connection closed
    assert client.connection_closed is True
    assert via_docker["transport"] == {"kind": "docker"}
    assert via_socket["transport"] == {
        "kind": "socket",
        "socket": path,
        "expected": {"image": f"{IMAGE}@{DIGEST}", "digest": DIGEST},
        "digest_enforced_by": ui_kit.SOCKET_SERVICE,
        "peer_uid": os.geteuid(),
    }
    strip = ("transport", "snapshot_sha256")
    assert {k: v for k, v in via_docker.items() if k not in strip} == {
        k: v for k, v in via_socket.items() if k not in strip
    }
    assert via_socket["snapshot_sha256"] == snapshot_hash(via_socket)


async def test_socket_session_keeps_the_read_only_allowlist(tmp_path: Path) -> None:
    with _service(tmp_path) as (_service_handle, path):
        async with _socket_client(path) as client:
            for tool in ("sync_design_system", "scaffold_page", "overview"):
                with pytest.raises(KitToolRefused):
                    await client.call(tool)
            assert (await client.call("get_rules"))["text"] == "ruleone"
    called = [
        entry["params"]["name"]
        for entry in _log(tmp_path / "ok.log")
        if entry["method"] == "tools/call"
    ]
    assert called == ["get_rules"]


async def test_socket_session_deadline_closes_the_connection(tmp_path: Path) -> None:
    with _service(tmp_path, "silent") as (service, path):
        client = _socket_client(path, deadline_seconds=1.0)
        with pytest.raises(TimeoutError):
            async with client:
                pass
        assert client.connection_closed is True
        assert await asyncio.to_thread(service.eof.wait, 10)


async def test_socket_session_cancellation_closes_the_connection(tmp_path: Path) -> None:
    with _service(tmp_path, "silent") as (service, path):
        client = _socket_client(path, deadline_seconds=60)

        async def session() -> None:
            async with client:
                pass

        task = asyncio.create_task(session())
        for _ in range(200):
            if client._writer is not None:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=20)
        assert client.connection_closed is True
        assert client._writer is not None and client._writer.transport.is_closing()
        assert await asyncio.to_thread(service.eof.wait, 10)


async def test_socket_session_refuses_an_oversize_line(tmp_path: Path) -> None:
    with _service(tmp_path, "oversize") as (service, path):
        client = _socket_client(path)
        with pytest.raises(KitProtocolError, match="size bound"):
            async with client:
                pass
        assert client.connection_closed is True
        assert await asyncio.to_thread(service.eof.wait, 10)


async def test_socket_session_enforces_the_total_byte_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui_kit, "MAX_SESSION_BYTES", 64 * 1024)
    with _service(tmp_path) as (_service_handle, path):
        async with _socket_client(path) as client:
            with pytest.raises(KitProtocolError, match="session size bound"):
                await client.call("get_tokens", {"theme": "all", "format": "json"})


def _preflight_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "run"
    directory.mkdir(mode=0o750)
    directory.chmod(0o750)
    return directory


def test_socket_preflight_refuses_a_regular_file_or_symlink(tmp_path: Path) -> None:
    directory = _preflight_dir(tmp_path)
    regular = directory / "mcp.sock"
    regular.write_text("", encoding="utf-8")
    with pytest.raises(KitUnavailable, match="is not a socket"):
        socket_preflight(str(regular), trusted_uid=os.geteuid())
    regular.unlink()
    real = tmp_path / "elsewhere.sock"
    listener = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    listener.bind(str(real))
    try:
        regular.symlink_to(real)
        with pytest.raises(KitUnavailable, match="is a symlink"):
            socket_preflight(str(regular), trusted_uid=os.geteuid())
    finally:
        listener.close()
    regular.unlink()
    with pytest.raises(KitUnavailable, match="does not exist"):
        socket_preflight(str(regular), trusted_uid=os.geteuid())
    with pytest.raises(KitUnavailable, match="does not exist"):
        socket_preflight("/nonexistent-new129/mcp.sock")
    linked = tmp_path / "linked"
    linked.symlink_to(directory, target_is_directory=True)
    with pytest.raises(KitUnavailable, match="symlink"):
        socket_preflight(str(linked / "mcp.sock"), trusted_uid=os.geteuid())


def test_socket_preflight_requires_root_ownership_and_a_closed_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _preflight_dir(tmp_path)
    path = directory / "mcp.sock"
    listener = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    listener.bind(str(path))
    try:
        socket_preflight(str(path), trusted_uid=os.geteuid())  # the trusted shape passes
        # Production trusts root only: a directory owned by this user is refused.
        with pytest.raises(KitUnavailable, match=f"{directory} is not owned by root"):
            socket_preflight(str(path))
        directory.chmod(0o770)
        with pytest.raises(KitUnavailable, match="writable by group or others"):
            socket_preflight(str(path), trusted_uid=os.geteuid())
        directory.chmod(0o757)
        with pytest.raises(KitUnavailable, match="writable by group or others"):
            socket_preflight(str(path), trusted_uid=os.geteuid())
        directory.chmod(0o750)
        # A socket owned by someone else (simulated: no chown without root).
        real_lstat = os.lstat

        def foreign_socket(target: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            result = real_lstat(target, *args, **kwargs)
            if str(target) == str(path):
                fields = list(result)
                fields[stat.ST_UID] = os.geteuid() + 4242
                return os.stat_result(fields)
            return result

        monkeypatch.setattr(ui_kit.os, "lstat", foreign_socket)
        with pytest.raises(KitUnavailable, match=f"{path} is not owned by root"):
            socket_preflight(str(path), trusted_uid=os.geteuid())
    finally:
        listener.close()


async def test_socket_session_is_blocked_by_preflight_peer_and_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _preflight_dir(tmp_path)
    stale = directory / "mcp.sock"
    listener = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    listener.bind(str(stale))  # bound, never listening: connect is refused
    try:
        with pytest.raises(KitUnavailable, match="refused the connection"):
            async with _socket_client(str(stale)):
                pass
    finally:
        listener.close()
        stale.unlink()
    (tmp_path / "svc").mkdir()
    with _service(tmp_path / "svc") as (_service_handle, path):
        monkeypatch.setattr(ui_kit, "_peer_uid", lambda writer: os.geteuid() + 4242)
        client = _socket_client(path)
        with pytest.raises(KitUnavailable, match="not served by root"):
            async with client:
                pass
        assert client.connection_closed is True
        # Production trusts root only: the same socket owned by this user is refused.
        with pytest.raises(KitUnavailable, match="not owned by root"):
            async with GntikUiSocketClient(path, image=IMAGE, digest=DIGEST):
                pass


async def test_scan_step_is_blocked_while_the_service_socket_is_missing(
    tmp_path: Path,
) -> None:
    if os.path.lexists(SERVICE_SOCKET):  # noqa: ASYNC240
        pytest.skip("the gntik-ui MCP socket is installed on this host")
    settings = Settings(
        worktree_root=tmp_path / "worktrees",
        evidence_root=tmp_path / "evidence",
        project_config_root=PROJECTS,
    )
    result = await ScanActivityHandler(settings).handle("ui.kit_snapshot", {"project": "falcone"})
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "UI_MCP_UNAVAILABLE"
    assert result["transport"] == "socket"
    assert "gntik-ui-mcp.socket installed and active" in result["reason"]


async def test_scan_step_reports_the_socket_transport(tmp_path: Path) -> None:
    with _service(tmp_path) as (_service_handle, path):
        result = await _handler(tmp_path, lambda policy: _socket_client(path)).handle(
            "ui.kit_snapshot", {"project": "falcone"}
        )
    assert result["status"] == "UI_KIT_SNAPSHOT_PASS"
    assert result["transport"] == "socket"
    stored = json.loads(
        (
            tmp_path
            / "evidence/scan/ui-kit"
            / result["snapshot_sha256"][:32]
            / "ui-kit-snapshot.json"
        ).read_text(encoding="utf-8")
    )
    assert stored["transport"]["kind"] == "socket"
    assert stored["transport"]["expected"]["digest"] == DIGEST


def test_cli_docker_flag_forces_the_local_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(cls: Any, policy: UiMigrationPolicy) -> Any:
        raise AssertionError("--docker must bypass the manifest socket")

    seen: list[tuple[str, str]] = []

    def pinned(cls: Any, image: str, digest: str, **kwargs: Any) -> GntikUiKitClient:
        seen.append((image, digest))
        return _stub(tmp_path)[0]

    monkeypatch.setattr(GntikUiKitClient, "from_policy", classmethod(refuse))
    monkeypatch.setattr(GntikUiKitClient, "pinned", classmethod(pinned))
    arguments = Namespace(
        project="falcone",
        manifest=PROJECTS / "falcone.yaml",
        templates=[],
        blocks=[],
        components=[],
        output_dir=None,
        docker=True,
    )
    result = _ui_kit_snapshot_command(arguments)
    assert result["status"] == "UI_KIT_SNAPSHOT_PASS" and result["transport"] == "docker"
    assert seen == [(IMAGE, DIGEST)]


def _fill_send_buffer(client: GntikUiSocketClient) -> None:
    """Queue far more than the kernel buffers hold: the peer reads none of it."""
    assert client._writer is not None
    client._writer.write(b"x" * (32 << 20))
    assert client._writer.transport.get_write_buffer_size() > 0


async def test_a_close_timeout_aborts_and_closes_the_descriptor(tmp_path: Path) -> None:
    with _service(tmp_path, "stall") as (service, path):
        client = _socket_client(path, shutdown_seconds=0.5)
        async with client:
            _fill_send_buffer(client)
            writer = client._writer
            assert writer is not None
        # The graceful close could not flush: the transport was aborted, the fd is closed.
        assert client.connection_closed is True
        assert writer.get_extra_info("socket").fileno() == -1
        service.release.set()
        assert await asyncio.to_thread(service.eof.wait, 10)  # the service sees EOF


async def test_cancellation_during_a_graceful_close_aborts_the_connection(
    tmp_path: Path,
) -> None:
    with _service(tmp_path, "stall") as (service, path):
        client = _socket_client(path, shutdown_seconds=60)  # the graceful wait would block

        async def session() -> None:
            async with client:
                _fill_send_buffer(client)

        task = asyncio.create_task(session())
        for _ in range(500):
            if client._writer is not None and client._writer.transport.is_closing():
                break
            await asyncio.sleep(0.01)
        assert client._writer is not None and client._writer.transport.is_closing()
        writer = client._writer
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        assert client.connection_closed is True
        assert writer.get_extra_info("socket").fileno() == -1
        service.release.set()
        assert await asyncio.to_thread(service.eof.wait, 10)
