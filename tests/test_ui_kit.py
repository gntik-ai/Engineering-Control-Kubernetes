"""NEW-121 v2: the Hermes-side, model-free gntik-ui kit client and `ui.kit_snapshot`."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from argparse import Namespace
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
from engineering_control.engineering.ui_kit import (
    READ_ONLY_TOOLS,
    GntikUiKitClient,
    KitProtocolError,
    KitToolRefused,
    KitUnavailable,
    collect_kit_snapshot,
    kit_command,
    kit_environment,
    snapshot_hash,
    snapshot_plan,
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


def test_the_container_command_is_pinned_offline_and_unprivileged() -> None:
    assert kit_command("/usr/bin/docker", IMAGE, DIGEST, name="hermes-ui-kit-abc") == [
        "/usr/bin/docker",
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
    for docker, image, digest in (
        ("docker", IMAGE, DIGEST),  # never a PATH lookup
        ("/usr/bin/docker", f"{IMAGE}:latest", DIGEST),
        ("/usr/bin/docker", IMAGE, "sha256:abc"),
        ("/usr/bin/docker", IMAGE, "latest"),
    ):
        with pytest.raises(ValueError):
            kit_command(docker, image, digest, name="hermes-ui-kit-abc")


def test_pinned_client_runs_in_root_with_a_minimal_environment() -> None:
    client = GntikUiKitClient.pinned(IMAGE, DIGEST, docker_executable="/usr/bin/docker")
    assert client.cwd == "/"
    assert set(client.environment) <= {"PATH", "DOCKER_HOST"}
    assert client.argv[0] == "/usr/bin/docker"
    assert client.argv[-2:] == [f"{IMAGE}@{DIGEST}", "--stdio"]
    assert client.preflight == [
        "/usr/bin/docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        f"{IMAGE}@{DIGEST}",
    ]
    name = client.argv[client.argv.index("--name") + 1]
    assert client.cleanup_argv == ["/usr/bin/docker", "rm", "-f", name]


def test_environment_forwards_only_a_local_docker_socket() -> None:
    assert kit_environment({"HOME": "/home/x", "LD_PRELOAD": "/var/lib/x.so"}) == {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"
    }
    assert kit_environment({"DOCKER_HOST": "unix:///run/hermes/docker-scan.sock"}) == {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "DOCKER_HOST": "unix:///run/hermes/docker-scan.sock",
    }
    for host in ("tcp://10.0.0.1:2375", "ssh://x", "unix:///run/../tmp/s"):
        assert "DOCKER_HOST" not in kit_environment({"DOCKER_HOST": host})


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
        f'#!/bin/sh\necho "$1" >> {calls}\necho "Error: No such image: x" >&2\nexit 1\n',
        encoding="utf-8",
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    client = GntikUiKitClient.pinned(IMAGE, DIGEST, docker_executable=str(docker))
    with pytest.raises(KitUnavailable, match="is not pre-pulled"):
        async with client:
            pass
    assert calls.read_text(encoding="utf-8").split() == ["image"]  # inspect only: no run


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
