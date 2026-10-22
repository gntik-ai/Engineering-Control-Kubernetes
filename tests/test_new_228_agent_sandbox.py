"""NEW-228 (reduced): loopback-only Claude sandbox (the shell prefix) and the Claude
checker's private scratch copy of the maker's worktree; Codex keeps today's sandbox.

The live bubblewrap tests run scripts/agent-loopback-shell exactly as Claude Code does
(`<prefix> '<command>'`); scripts/agent-loopback-smoke.py is the deploy-time check that
also drives the real provider CLIs.
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.adapters import agent_sandbox as sandbox_module
from engineering_control.adapters.agent_sandbox import (
    AgentSandbox,
    ScratchCopyError,
    copy_worktree,
    loopback_shell_problem,
    payload_agent_sandbox,
    remove_tree,
)
from engineering_control.adapters.claude import client as claude_module
from engineering_control.adapters.claude.client import ClaudeCLIAdapter
from engineering_control.adapters.process import TOOLCHAIN_CACHE_ROOT, ProcessResult
from engineering_control.agents.instructions import InstructionBundle
from engineering_control.config import Settings
from engineering_control.engineering.toolchain import load_agent_sandbox
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import ModelClass, Project, Provider, RunStatus

REPOSITORY = Path(__file__).resolve().parents[1]
PROJECTS = REPOSITORY / "config" / "projects"
LOOPBACK_SHELL = REPOSITORY / "scripts" / "agent-loopback-shell"
LOOPBACK = AgentSandbox(loopback_network=True)


def _shell_constant(name: str) -> Any:
    """A literal constant of scripts/agent-loopback-shell (a script, not a module)."""
    for node in ast.parse(LOOPBACK_SHELL.read_text()).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return ast.literal_eval(node.value)
    raise KeyError(name)


ETC_ENTRIES: tuple[str, ...] = _shell_constant("ETC_ENTRIES")


def _request(
    worktree: Path, provider: Provider, *, writable: bool, role: str = "implementer"
) -> AgentRequest:
    return AgentRequest(
        project=Project.GNTIK_UI,
        role=role,
        provider=provider,
        model_class=ModelClass.WORKER,
        prompt="perform a bounded synthetic task",
        working_directory=str(worktree),
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        },
        writable=writable,
        allowed_tools=("Read", "Glob", "Grep", "Bash"),
    )


# -- contract and transport -----------------------------------------------------------


def test_payload_sandbox_is_absent_by_default_and_strict_when_present() -> None:
    assert payload_agent_sandbox({}) is None
    assert payload_agent_sandbox({"toolchain_preparation": {"status": "TOOLCHAIN_READY"}}) is None
    both = {"loopback_network": True, "checker_scratch_copy": True}
    assert payload_agent_sandbox({"toolchain_preparation": {"agent_sandbox": both}}) == (
        AgentSandbox(loopback_network=True, checker_scratch_copy=True)
    )
    for malformed in (
        None,
        [],
        {"loopback_network": "yes"},
        {"loopback_network": False},
        {"network": True},
        {"loopback_shell": "/srv/agent-owned/shell"},  # only the worker sets the shell
        {"readable_roots": ["/run/user/1000"]},  # and the roots the shell mounts
        {"writable_roots": ["/"]},
    ):
        with pytest.raises(ValueError, match="agent_sandbox is malformed"):
            payload_agent_sandbox({"toolchain_preparation": {"agent_sandbox": malformed}})


def test_only_gntik_ui_opts_in() -> None:
    assert load_agent_sandbox(PROJECTS, "gntik-ui") == {
        "loopback_network": True,
        "checker_scratch_copy": True,
    }
    for project in ("falcone", "musematic", "llmwiki"):
        assert load_agent_sandbox(PROJECTS, project) is None


def test_prepare_toolchain_carries_the_opt_ins_to_the_agents(tmp_path: Path) -> None:
    from test_delivery_toolchain import FakeToolchainRunner, _handler, _payload, _source_repo

    repo = _source_repo(tmp_path / "worktrees")
    projects = tmp_path / "projects"
    projects.mkdir()
    for manifest in PROJECTS.glob("*.yaml"):
        shutil.copy(manifest, projects / manifest.name)
    with (projects / "musematic.yaml").open("a") as handle:
        handle.write("sandbox:\n  loopback_network: true\n  checker_scratch_copy: true\n")
    result = _handler(
        tmp_path, FakeToolchainRunner(), project_config_root=projects
    )._prepare_toolchain(_payload(repo))
    assert result["status"] == "TOOLCHAIN_READY"
    assert result["toolchain_preparation"]["agent_sandbox"] == {
        "loopback_network": True,
        "checker_scratch_copy": True,
    }
    assert payload_agent_sandbox(result) == AgentSandbox(True, True)
    other = _handler(tmp_path, FakeToolchainRunner())._prepare_toolchain(_payload(repo))
    assert other["status"] == "TOOLCHAIN_READY"
    assert "agent_sandbox" not in other["toolchain_preparation"]


# -- Claude ---------------------------------------------------------------------------


async def _claude_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: ClaudeCLIAdapter,
    sandbox: AgentSandbox | None,
) -> dict[str, Any]:
    observed: dict[str, Any] = {}

    async def fake_run(command: list[str], **kwargs: Any) -> ProcessResult:
        observed["command"] = command
        observed["environment"] = kwargs.get("environment")
        return ProcessResult(
            exit_code=0,
            events=({"type": "result", "structured_output": {"summary": "x"}, "result": "x"},),
            stderr="",
        )

    monkeypatch.setattr(claude_module, "run_jsonl_process", fake_run)
    request = _request(tmp_path, Provider.CLAUDE, writable=False, role="code-reviewer")
    if sandbox is None:
        await adapter.execute(request)
    else:
        await adapter.execute(request, sandbox=sandbox)
    return observed


@pytest.mark.asyncio
async def test_claude_bash_runs_through_the_loopback_shell_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = "/opt/engineering-control/source/scripts/agent-loopback-shell"
    observed = await _claude_call(
        tmp_path, monkeypatch, ClaudeCLIAdapter(loopback_shell=shell), LOOPBACK
    )
    command = observed["command"]
    pinned = {
        "CLAUDE_CODE_SHELL_PREFIX": shell,
        "CLAUDE_CODE_SHELL": "/bin/bash",
        "HERMES_LOOPBACK_WRITABLE": str(tmp_path),
        "HERMES_LOOPBACK_READABLE": "",
    }
    assert json.loads(command[command.index("--settings") + 1]) == {"env": pinned}
    assert observed["environment"] == pinned
    assert "--safe-mode" in command

    unchanged = await _claude_call(
        tmp_path, monkeypatch, ClaudeCLIAdapter(loopback_shell=shell), None
    )
    assert "--settings" not in unchanged["command"]
    assert unchanged["environment"] is None
    with pytest.raises(ValueError, match="without a loopback shell"):
        await _claude_call(tmp_path, monkeypatch, ClaudeCLIAdapter(), LOOPBACK)
    verified = AgentSandbox(loopback_network=True, loopback_shell="/verified/shell")
    preferred = await _claude_call(
        tmp_path, monkeypatch, ClaudeCLIAdapter(loopback_shell=shell), verified
    )
    assert preferred["environment"]["CLAUDE_CODE_SHELL_PREFIX"] == "/verified/shell"


@pytest.mark.asyncio
async def test_claude_loopback_mounts_the_run_directories_and_pins_every_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = "/opt/engineering-control/source/scripts/agent-loopback-shell"
    adapter = ClaudeCLIAdapter(temp_root=str(tmp_path / "temp"), loopback_shell=shell)
    sandbox = AgentSandbox(
        loopback_network=True,
        readable_roots=("/srv/engineering/mirrors", "/srv/maker"),
        writable_roots=("/srv/engineering/cache/scan/worktree-git",),
    )
    observed = await _claude_call(tmp_path, monkeypatch, adapter, sandbox)
    command = observed["command"]
    settings = json.loads(command[command.index("--settings") + 1])["env"]
    writable = settings["HERMES_LOOPBACK_WRITABLE"].split(":")
    run_temp = writable[-1]
    assert writable == [str(tmp_path), "/srv/engineering/cache/scan/worktree-git", run_temp]
    assert Path(run_temp).parent == tmp_path / "temp" / "run-tmp"
    assert settings["HERMES_LOOPBACK_READABLE"] == "/srv/engineering/mirrors:/srv/maker"
    assert settings["CLAUDE_CODE_TMPDIR"] == run_temp
    assert observed["environment"]["TMPDIR"] == run_temp
    assert {key: observed["environment"][key] for key in settings} == settings

    # Without the opt-in the run keeps exactly NEW-226's environment.
    plain = await _claude_call(tmp_path, monkeypatch, adapter, None)
    assert "--settings" not in plain["command"]
    assert set(plain["environment"]) == {"TMPDIR", "TMP", "TEMP"}
    with pytest.raises(ValueError, match="absolute paths"):
        await _claude_call(
            tmp_path,
            monkeypatch,
            adapter,
            AgentSandbox(loopback_network=True, readable_roots=("relative",)),
        )


def test_loopback_shell_must_not_be_replaceable_by_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "not an absolute path" in str(loopback_shell_problem(Path("agent-loopback-shell")))
    assert "FileNotFoundError" in str(loopback_shell_problem(tmp_path / "missing"))
    script = tmp_path / "shell"
    script.write_text("#!/bin/sh\n")
    assert "not an executable file" in str(loopback_shell_problem(script))
    script.chmod(0o755)
    assert "writable by this user" in str(loopback_shell_problem(script))  # agent-owned

    def access(path: Any, mode: int) -> bool:
        if Path(path) == sandbox_module.LOOPBACK_SHELL_BWRAP:
            return True
        return mode != os.W_OK  # an executable nobody here can write: the release tree

    monkeypatch.setattr(sandbox_module.os, "access", access)
    assert loopback_shell_problem(script) is None


# -- the provider handler -------------------------------------------------------------


class RecordingAdapter:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.calls: list[dict[str, Any]] = []

    async def authenticated(self) -> bool:
        return True

    async def execute(self, request: AgentRequest, on_event: Any = None, **options: Any) -> Any:
        directory = Path(request.working_directory)
        call: dict[str, Any] = {"request": request, "options": options}
        call["files"] = sorted(os.listdir(directory))
        (directory / "dist").mkdir()  # a build in the checker's working directory
        self.calls.append(call)
        now = datetime.now(UTC)
        return AgentResult(
            run_id=request.run_id,
            provider=request.provider,
            role=request.role,
            status=RunStatus.SUCCESS,
            summary="ok",
            structured_result={"summary": "ok"},
            started_at=now,
            finished_at=now,
        )


class PassThroughPack:
    def enrich(self, request: AgentRequest, _step: str) -> tuple[AgentRequest, InstructionBundle]:
        return request, InstructionBundle(
            prompt_prefix="", sha256="a" * 64, revision=None, sources=("x",), allowed_tools=()
        )


def _handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> tuple[ProviderActivityHandler, RecordingAdapter, Path]:
    monkeypatch.setattr(
        "engineering_control.activities.provider.activity.heartbeat", lambda _: None
    )
    worktree = tmp_path / "worktrees" / "gntik-ui" / "source"
    worktree.mkdir(parents=True)
    (worktree / "package.json").write_text("{}\n")
    adapter = RecordingAdapter(provider)
    handler = ProviderActivityHandler(
        Settings(
            worktree_root=tmp_path / "worktrees",
            worktree_metadata_root=tmp_path / "metadata",
            mirror_root=tmp_path / "mirrors",
            run_root=tmp_path / "runs",
            temporary_root=tmp_path / "tmp",
            agent_loopback_shell=LOOPBACK_SHELL,
        ),
        provider.value,
        adapter,
        PassThroughPack(),
    )
    return handler, adapter, worktree


def _git(*arguments: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed git executable, test-owned repositories
        [shutil.which("git") or "/usr/bin/git", "-c", "user.name=t", "-c", "user.email=t@t"]
        + list(arguments),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _changeset(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """The worker layout: a canonical mirror, a ChangeSet's metadata repository
    (``metadata/<repository>/<id>.git``, objects borrowed from the mirror) with the linked
    worktree ``worktrees/<repository>/<id>``, and another ChangeSet's metadata beside it."""
    seed = tmp_path / "seed"
    _git("init", "-q", str(seed))
    _git("-C", str(seed), "commit", "-q", "--allow-empty", "-m", "base")
    mirror = tmp_path / "mirrors" / "gntik-ui.git"
    _git("clone", "-q", "--bare", str(seed), str(mirror))
    paths = []
    for _ in range(2):
        identity = str(uuid4())
        metadata = tmp_path / "metadata" / "gntik-ui" / f"{identity}.git"
        _git("clone", "-q", "--bare", "--shared", str(mirror), str(metadata))
        (metadata / "logs" / "refs").mkdir(parents=True)  # as seal_metadata leaves it
        worktree = tmp_path / "worktrees" / "gntik-ui" / identity
        _git("--git-dir", str(metadata), "worktree", "add", "-q", "-b", identity, str(worktree))
        paths.append((worktree, metadata))
    (worktree, metadata), (_other_worktree, other) = paths
    return worktree, metadata, other, mirror


def _payload(key: str, request: AgentRequest, sandbox: dict[str, bool] | None) -> dict[str, Any]:
    preparation: dict[str, Any] = {"status": "TOOLCHAIN_READY"}
    if sandbox is not None:
        preparation["agent_sandbox"] = sandbox
    return {key: request.model_dump(mode="json"), "toolchain_preparation": preparation}


@pytest.mark.asyncio
async def test_claude_checker_builds_in_a_private_copy_removed_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, adapter, worktree = _handler(tmp_path, monkeypatch, Provider.CLAUDE)
    monkeypatch.setattr(
        "engineering_control.activities.provider.verified_loopback_shell",
        lambda path: (Path("/opt/release/scripts/agent-loopback-shell"), None),
    )
    request = _request(worktree, Provider.CLAUDE, writable=False, role="code-reviewer")

    result = await handler.handle(
        "delivery.invoke_checker",
        _payload(
            "checker_request",
            request,
            {"loopback_network": True, "checker_scratch_copy": True},
        ),
    )

    assert result["status"] == "SUCCESS"
    (call,) = adapter.calls
    copy = Path(call["request"].working_directory)
    assert copy != worktree and copy.name == worktree.name
    assert copy.is_relative_to(tmp_path / "tmp" / "claude")
    assert call["files"] == ["package.json"]
    assert f"private, writable copy of the maker's worktree {worktree}" in (call["request"].prompt)
    # The shell that was verified (resolved) is the one the adapter is given.
    assert call["options"] == {
        "sandbox": AgentSandbox(
            True,
            True,
            "/opt/release/scripts/agent-loopback-shell",
            # Not a ChangeSet worktree: no Git metadata at all.
            readable_roots=(str(TOOLCHAIN_CACHE_ROOT), str(worktree)),
        )
    }
    assert not (worktree / "dist").exists()  # the maker's worktree is untouched
    assert not os.path.exists(copy)  # noqa: ASYNC240 - synchronous test assertion
    assert os.listdir(tmp_path / "tmp" / "claude") == []
    # The recorded request is the original one.
    assert result["agent_result"]["run_id"] == str(request.run_id)


@pytest.mark.asyncio
async def test_a_copy_that_cannot_be_made_leaves_the_checker_on_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, adapter, worktree = _handler(tmp_path, monkeypatch, Provider.CLAUDE)

    def too_large(_source: Path, _destination: Path) -> None:
        raise ScratchCopyError("worktree exceeds 1 entries")

    monkeypatch.setattr("engineering_control.activities.provider.copy_worktree", too_large)
    request = _request(worktree, Provider.CLAUDE, writable=False, role="code-reviewer")
    result = await handler.handle(
        "delivery.invoke_checker",
        _payload("checker_request", request, {"checker_scratch_copy": True}),
    )
    assert result["status"] == "SUCCESS"
    assert adapter.calls[0]["request"].working_directory == str(worktree)
    assert list((tmp_path / "tmp" / "claude").iterdir()) == []


@pytest.mark.asyncio
async def test_codex_keeps_todays_sandbox_whatever_the_payload_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, adapter, worktree = _handler(tmp_path, monkeypatch, Provider.CODEX)
    maker = _request(worktree, Provider.CODEX, writable=True)
    for sandbox in ({"loopback_network": True, "checker_scratch_copy": True}, {"x": "on"}):
        result = await handler.handle(
            "delivery.invoke_maker", _payload("maker_request", maker, sandbox)
        )
        assert result["status"] == "SUCCESS"
        assert adapter.calls[-1]["options"] == {}  # the adapter is called as on main
        assert adapter.calls[-1]["request"].working_directory == str(worktree)
        (worktree / "dist").rmdir()
    checker = _request(worktree, Provider.CODEX, writable=False, role="code-reviewer")
    await handler.handle(
        "delivery.invoke_checker",
        _payload("checker_request", checker, {"checker_scratch_copy": True}),
    )
    assert adapter.calls[-1]["request"].working_directory == str(worktree)


@pytest.mark.asyncio
async def test_claude_loopback_maker_gets_writable_git_metadata_and_other_steps_keep_today(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, adapter, _source = _handler(tmp_path, monkeypatch, Provider.CLAUDE)
    monkeypatch.setattr(
        "engineering_control.activities.provider.verified_loopback_shell",
        lambda path: (Path("/opt/release/scripts/agent-loopback-shell"), None),
    )
    worktree, metadata, other, mirror = _changeset(tmp_path)
    (admin,) = (metadata / "worktrees").iterdir()
    maker = _request(worktree, Provider.CLAUDE, writable=True)
    result = await handler.handle(
        "delivery.invoke_maker", _payload("maker_request", maker, {"loopback_network": True})
    )
    assert result["status"] == "SUCCESS"
    sandbox = adapter.calls[-1]["options"]["sandbox"]
    # This ChangeSet's metadata and its mirror's objects only, never the metadata root.
    assert sandbox.readable_roots == (
        str(metadata.resolve()),
        str(mirror.resolve() / "objects"),
        str(TOOLCHAIN_CACHE_ROOT),
    )
    common = metadata.resolve()
    assert sandbox.writable_roots == tuple(
        str(path)
        for path in (admin.resolve(), common / "objects", common / "refs", common / "logs")
        if path == admin.resolve() or path.is_dir()
    )
    assert not any(
        str(other) in root for root in (*sandbox.readable_roots, *sandbox.writable_roots)
    )
    (worktree / "dist").rmdir()
    checker = _request(worktree, Provider.CLAUDE, writable=False, role="code-reviewer")
    await handler.handle(
        "delivery.invoke_checker",
        _payload("checker_request", checker, {"loopback_network": True}),
    )
    read_only = adapter.calls[-1]["options"]["sandbox"]
    assert read_only.writable_roots == ()
    assert read_only.readable_roots[:2] == sandbox.readable_roots[:2]
    (worktree / "dist").rmdir()
    # A metadata path redirected elsewhere (a symlink) gets no Git mounts.
    moved = metadata.with_name("moved.git")
    metadata.rename(moved)
    metadata.symlink_to(moved)
    await handler.handle(
        "delivery.invoke_maker", _payload("maker_request", maker, {"loopback_network": True})
    )
    assert adapter.calls[-1]["options"]["sandbox"].readable_roots == (str(TOOLCHAIN_CACHE_ROOT),)
    metadata.unlink()
    moved.rename(metadata)
    (worktree / "dist").rmdir()

    malformed = await handler.handle(
        "delivery.invoke_maker", _payload("maker_request", maker, {"loopback_network": "on"})
    )
    assert malformed["blocker"] == "AGENT_SANDBOX_CONTRACT_INVALID"

    # Not a maker/checker step: the opt-in does not apply, the adapter is called as before.
    spec = _request(worktree, Provider.CLAUDE, writable=False, role="product-spec-architect")
    await handler.handle(
        "delivery.invoke_spec", _payload("spec_request", spec, {"loopback_network": True})
    )
    assert adapter.calls[-1]["options"] == {}


@pytest.mark.asyncio
async def test_claude_loopback_refuses_a_shell_the_runner_could_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, adapter, worktree = _handler(tmp_path, monkeypatch, Provider.CLAUDE)
    request = _request(worktree, Provider.CLAUDE, writable=False, role="code-reviewer")
    result = await handler.handle(
        "delivery.invoke_checker",
        _payload("checker_request", request, {"loopback_network": True}),
    )
    # This test runs from a checkout the current user owns.
    assert result["blocker"] == "AGENT_SANDBOX_LOOPBACK_UNAVAILABLE"
    assert adapter.calls == []


# -- the scratch copy ------------------------------------------------------------------


def test_copy_keeps_files_and_links_links_the_pnpm_store_and_skips_special_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "packages" / "ui" / "node_modules").mkdir(parents=True)
    (source / "packages" / "tokens").mkdir()
    (source / "node_modules" / ".pnpm" / "react@19" / "node_modules" / "react").mkdir(parents=True)
    (source / "node_modules" / ".pnpm" / "react@19" / "node_modules" / "react" / "i.js").write_text(
        "x"
    )
    (source / "packages" / "ui" / "node_modules" / "tokens").symlink_to("../../tokens")
    (source / "outside").symlink_to("/etc")
    (source / ".git").write_text("gitdir: /srv/engineering/cache/scan/worktree-git/x\n")
    tool = source / "packages" / "tokens" / "build.sh"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o4755)  # a set-id bit is never copied
    os.utime(tool, ns=(1_000_000_000, 2_000_000_000))
    os.mkfifo(source / "pipe")
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(source / "s"))
    destination = tmp_path / "copy" / "source"
    destination.parent.mkdir()

    copy_worktree(source, destination)
    listener.close()

    store = destination / "node_modules" / ".pnpm"
    assert store.is_symlink() and store.resolve() == (source / "node_modules" / ".pnpm")
    assert os.readlink(destination / "packages" / "ui" / "node_modules" / "tokens") == (
        "../../tokens"
    )
    assert (destination / "packages" / "ui" / "node_modules" / "tokens").resolve() == (
        destination / "packages" / "tokens"
    )
    assert os.readlink(destination / "outside") == "/etc"
    copied = destination / "packages" / "tokens" / "build.sh"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o755
    assert copied.stat().st_mtime_ns == 2_000_000_000
    assert (destination / ".git").read_text() == (source / ".git").read_text()
    assert not (destination / "pipe").exists() and not (destination / "s").exists()
    with pytest.raises(FileExistsError):
        copy_worktree(source, destination)


def test_copy_is_bounded_and_removal_handles_unwritable_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for index in range(5):
        (source / f"f{index}").write_text("x" * 10)
    monkeypatch.setattr(sandbox_module, "SCRATCH_COPY_MAX_ENTRIES", 3)
    with pytest.raises(ScratchCopyError, match="entries"):
        copy_worktree(source, tmp_path / "a")
    assert len(list((tmp_path / "a").iterdir())) == 3  # refused before the 4th entry
    monkeypatch.setattr(sandbox_module, "SCRATCH_COPY_MAX_ENTRIES", 100)
    monkeypatch.setattr(sandbox_module, "SCRATCH_COPY_MAX_BYTES", 25)
    with pytest.raises(ScratchCopyError, match="bytes"):
        copy_worktree(source, tmp_path / "b")
    locked = tmp_path / "b" / "dist" / "deep"
    locked.mkdir(parents=True)
    (locked / "out.js").write_text("x")
    (tmp_path / "b" / "link").symlink_to(source)
    locked.chmod(0o500)
    (tmp_path / "b" / "dist").chmod(0)
    remove_tree(tmp_path / "b")
    assert not (tmp_path / "b").exists()
    assert len(list(source.iterdir())) == 5  # never through a symlink
    remove_tree(tmp_path / "missing")


# -- live: the loopback shell under bubblewrap ------------------------------------------

_PROBE = r"""
import ctypes, errno, json, os, socket, stat, sys
def check(function):
    try:
        function()
        return "ok"
    except OSError as error:
        return errno.errorcode.get(error.errno, type(error).__name__)
def loopback(family, host):
    server = socket.socket(family)
    server.bind((host, 0))
    server.listen()
    client = socket.create_connection((host, server.getsockname()[1]), timeout=3)
    peer, _ = server.accept()
    client.sendall(b"x")
    assert peer.recv(1) == b"x"
def connect(host, port):
    return lambda: socket.create_connection((host, port), timeout=3).close()
def unix(path):
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(3)
    client.connect(path)
def unix_inside():
    # A unix socket made inside (Chrome's singleton) works between processes inside.
    path = os.path.join(sys.argv[3], "inside.sock")
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    server.listen()
    unix(path)
    os.unlink(path)
def io_uring():
    libc = ctypes.CDLL(None, use_errno=True)
    params = ctypes.create_string_buffer(120)
    if libc.syscall(425, 1, params) >= 0:
        return
    raise OSError(ctypes.get_errno(), "io_uring_setup")
def home():
    with open(os.path.join(os.path.expanduser("~"), ".hermes-new228-test"), "w") as handle:
        handle.write("x")
results = {
    "netns": os.readlink("/proc/self/ns/net"),
    "pid1": open("/proc/1/comm").read().strip(),
    "loopback4": check(lambda: loopback(socket.AF_INET, "127.0.0.1")),
    "loopback6": check(lambda: loopback(socket.AF_INET6, "::1")),
    "socketpair": check(socket.socketpair),
    "unix_inside": check(unix_inside),
    "home_write": check(home),
    "public4": check(connect("1.1.1.1", 443)),
    "public6": check(connect("2606:4700:4700::1111", 443)),
    "host_unix_socket": check(lambda: unix(sys.argv[2])),
    "run_user": check(lambda: os.stat(f"/run/user/{os.getuid()}")),
    "io_uring": check(io_uring),
}
for address in sys.argv[4:]:
    results[address] = check(connect(address, int(sys.argv[1])))
print(json.dumps(results))
"""

_WALK = r"""
import json, os, socket, stat, sys
SCRATCH = ("/tmp", "/var/tmp", "/run", "/dev/shm", "/dev", os.path.expanduser("~"))
mounts = []
with open("/proc/self/mountinfo") as handle:
    for line in handle:
        fields, kind = line.split(" - ", 1)
        mounts.append((fields.split()[4].encode().decode("unicode_escape"), kind.split()[0]))
def own_tmpfs(path):
    point, fstype = "", ""
    for mount, kind in mounts:  # the last, longest match is the visible mount
        if (path == mount or path.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(point):
            point, fstype = mount, kind
    return fstype == "tmpfs" and point in SCRATCH
inside = socket.socket(socket.AF_UNIX)
inside.bind("/tmp/made-inside.sock")  # the sandbox's own tmpfs: not a host socket
found, unlisted, entries, stack = [], [], 0, ["/"]
while stack and entries <= 1_500_000:
    top = stack.pop()
    try:
        with os.scandir(top) as listing:
            items = list(listing)
    except PermissionError:
        if os.access(top, os.X_OK):
            unlisted.append(top)  # enterable, not listable: inconclusive
        continue
    except OSError:
        continue
    for item in items:
        entries += 1
        if item.path in ("/proc", "/sys"):
            continue
        try:
            info = item.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISSOCK(info.st_mode) and not own_tmpfs(item.path):
            found.append(item.path)
        elif stat.S_ISDIR(info.st_mode):
            stack.append(item.path)
print(json.dumps({"found": found, "unlisted": unlisted, "entries": entries,
                  "truncated": bool(stack),
                  "top": sorted(os.listdir("/"))}))
"""


def _bwrap_usable() -> bool:
    if not Path("/usr/bin/bwrap").exists():
        return False
    probe = subprocess.run(  # noqa: S603 - fixed bubblewrap probe
        ["/usr/bin/bwrap", "--unshare-net", "--unshare-pid", "--dev-bind", "/", "/", "true"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def _host_addresses() -> list[str]:
    output = subprocess.run(  # noqa: S603 - fixed read-only command
        [shutil.which("hostname") or "hostname", "-I"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return [address for address in output.split() if not address.startswith("fe80")]


def _in_shell(
    command: str, cwd: Path, writable: str, readable: str = "", temp: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Exactly how Claude Code applies CLAUDE_CODE_SHELL_PREFIX: `'<prefix>' '<cmd>'`."""
    environment = {
        **{key: value for key, value in os.environ.items() if key not in {"TMPDIR", "TMP"}},
        "HERMES_LOOPBACK_WRITABLE": writable,
        "HERMES_LOOPBACK_READABLE": readable,
    }
    if temp is not None:
        environment["TMPDIR"] = str(temp)
    return subprocess.run(  # noqa: S603 - fixed shell, test-owned command
        ["/bin/bash", "-c", f"'{LOOPBACK_SHELL}' {shlex.quote(command)}"],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _unix_listener(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen()
    return listener


@pytest.mark.skipif(not _bwrap_usable(), reason="bubblewrap user namespaces unavailable")
def test_loopback_shell_allows_loopback_only(tmp_path: Path) -> None:
    work, outside = tmp_path / "work", tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    host_listener = socket.socket(socket.AF_INET6)
    host_listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    host_listener.bind(("::", 0))
    host_listener.listen()
    port = host_listener.getsockname()[1]
    targets = ["127.0.0.1", "::1", *_host_addresses()]
    # Outside the sandbox the host listeners are reachable.
    socket.create_connection(("127.0.0.1", port), timeout=3).close()
    relay = outside / "relay.sock"  # e.g. a host DNS, proxy or tmux socket
    unix_listener = _unix_listener(relay)
    socket.socket(socket.AF_UNIX).connect(str(relay))
    (work / "probe.py").write_text(_PROBE)
    marker = Path.home() / ".hermes-new228-test"
    command = " ".join(["python3", "probe.py", str(port), str(relay), str(work), *targets])
    try:
        completed = _in_shell(command, work, str(work))
    finally:
        host_listener.close()
        unix_listener.close()
    assert completed.returncode == 0, completed.stderr
    results = json.loads(completed.stdout)
    assert results.pop("netns") != os.readlink("/proc/self/ns/net")
    assert results.pop("pid1") == "bwrap"  # a PID namespace of its own
    for name in ("loopback4", "loopback6", "socketpair", "unix_inside", "home_write"):
        assert results.pop(name) == "ok", name
    assert not marker.exists()  # the home inside is a private tmpfs
    assert results.pop("public4") == "ENETUNREACH"
    assert results.pop("public6") == "ENETUNREACH"
    assert results.pop("host_unix_socket") == "ENOENT"  # not in the view
    assert results.pop("run_user") == "ENOENT"
    assert results.pop("io_uring") == "EPERM"
    assert results.pop("127.0.0.1") == "ECONNREFUSED"  # the namespace's own loopback
    assert results.pop("::1") == "ECONNREFUSED"
    assert results and all(outcome == "ENETUNREACH" for outcome in results.values()), results


@pytest.mark.skipif(not _bwrap_usable(), reason="bubblewrap user namespaces unavailable")
def test_loopback_shell_view_holds_no_host_socket(tmp_path: Path) -> None:
    work, outside = tmp_path / "w", tmp_path / "o"  # unix socket paths: <= 107 bytes
    work.mkdir()
    outside.mkdir()
    listeners = [_unix_listener(outside / "h.s")]
    # Controls: a host socket inside a mounted run directory is visible, and reported; a
    # directory that can be entered but not listed (a socket in it could be reached by
    # name) makes the walk inconclusive; one that cannot be entered at all is sealed.
    listeners.append(_unix_listener(work / "c.s"))
    traverse_only, sealed = work / "t", work / "s"
    for directory in (traverse_only, sealed):
        directory.mkdir()
        listeners.append(_unix_listener(directory / "h.s"))
    traverse_only.chmod(0o311)
    sealed.chmod(0o000)
    (work / "walk.py").write_text(_WALK)
    try:
        completed = _in_shell("python3 walk.py", work, str(work))
    finally:
        traverse_only.chmod(0o755)
        sealed.chmod(0o755)
        for listener in listeners:
            listener.close()
    assert completed.returncode == 0, completed.stderr
    walk = json.loads(completed.stdout)
    assert not walk["truncated"] and walk["entries"] > 1000
    assert walk["found"] == [str(work / "c.s")]
    assert walk["unlisted"] == [str(traverse_only)]  # nothing else in the view
    assert "srv" not in walk["top"] and "sys" not in walk["top"]


@pytest.mark.skipif(not _bwrap_usable(), reason="bubblewrap user namespaces unavailable")
def test_loopback_shell_etc_holds_no_worker_credentials_and_tools_work(tmp_path: Path) -> None:
    (tmp_path / "view.py").write_text(
        "import json, os, socket\n"
        "paths = [os.path.join(top, name) for top, dirs, files in os.walk('/etc')\n"
        "         for name in dirs + files]\n"
        "print(json.dumps({'etc': sorted(os.listdir('/etc')), 'paths': paths,\n"
        "                  'localhost': socket.getaddrinfo('localhost', 80)[0][4][0]}))\n"
    )
    command = (
        "python3 view.py && git init -q repo && git -C repo -c user.name=t "
        "-c user.email=t@t commit -q --allow-empty -m sandbox && git -C repo log --format=%s"
    )
    completed = _in_shell(command, tmp_path, str(tmp_path))
    assert completed.returncode == 0, completed.stderr
    first, commit = completed.stdout.splitlines()
    view = json.loads(first)
    assert commit == "sandbox"
    assert view["localhost"] in {"::1", "127.0.0.1"}
    allowed = {name.split("/")[0] for name in ETC_ENTRIES}
    assert set(view["etc"]) <= allowed, set(view["etc"]) - allowed
    assert "engineering-control" not in view["etc"]
    assert not [path for path in view["paths"] if "engineering-control" in path]
    assert not [path for path in view["paths"] if path.startswith("/etc/ssl/private")]
    node = shutil.which("node")
    if node is not None:  # the worker's Node.js (here wherever this host has one)
        resolved = Path(node).resolve()
        script = "require('node:http').createServer().listen(0, '::1', function () { "
        script += "console.log(this.address().port > 0); this.close(); })"
        ran = _in_shell(
            f"{resolved} -e {shlex.quote(script)}",
            tmp_path,
            str(tmp_path),
            readable=str(resolved.parent.parent),
        )
        assert ran.returncode == 0 and ran.stdout.strip() == "true", ran.stderr


@pytest.mark.skipif(not _bwrap_usable(), reason="bubblewrap user namespaces unavailable")
def test_loopback_shell_mounts_the_run_temp_at_tmp(tmp_path: Path) -> None:
    work, temp = tmp_path / "work", tmp_path / "run-temp"
    work.mkdir()
    temp.mkdir()
    command = 'echo "$TMPDIR"; echo x > /tmp/marker; ls "$HOME"'
    completed = _in_shell(command, work, f"{work}:{temp}", temp=temp)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines()[0] == "/tmp"  # noqa: S108
    assert (temp / "marker").read_text() == "x\n"  # on disk, removed with the run
    # A TMPDIR that is not one of the run's directories is not mounted.
    other = _in_shell("echo x > /tmp/marker", work, str(work), temp=tmp_path)
    assert other.returncode == 0, other.stderr
    assert not (tmp_path / "marker").exists()


@pytest.mark.skipif(not _bwrap_usable(), reason="bubblewrap user namespaces unavailable")
def test_loopback_shell_shows_this_changesets_git_metadata_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, _adapter, _source = _handler(tmp_path, monkeypatch, Provider.CLAUDE)
    worktree, metadata, other, _mirror = _changeset(tmp_path)
    readable, writable = handler._git_metadata_mounts(worktree, writable=True)
    command = (
        f"test ! -e {other} && test ! -e {other.parent / 'missing'} && "
        f"ls {metadata.parent} && echo change > file && git add file && "
        "git -c user.name=t -c user.email=t@t commit -q -m inside && git log --format=%s -1"
    )
    completed = _in_shell(
        command, worktree, ":".join((str(worktree), *writable)), readable=":".join(readable)
    )
    assert completed.returncode == 0, completed.stderr
    listed, subject = completed.stdout.split()
    assert listed == metadata.name  # the other ChangeSet's metadata is not in the view
    assert subject == "inside"  # a maker commits through the narrow writable mounts
    assert _git("-C", str(worktree), "log", "--format=%s", "-1").strip() == "inside"


def _shell_module() -> Any:
    loader = importlib.machinery.SourceFileLoader("agent_loopback_shell", str(LOOPBACK_SHELL))
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("agent_loopback_shell", loader)  # type: ignore[arg-type]
    )
    loader.exec_module(module)
    return module


def test_home_and_configuration_sources_are_resolved_and_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shell = _shell_module()
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".gitconfig").write_text("[user]\n")
    (home / ".claude" / "shell-snapshots").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HERMES_LOOPBACK_WRITABLE", "")
    monkeypatch.setenv("HERMES_LOOPBACK_READABLE", "")

    def binds(argv: list[str]) -> list[str]:
        return [argv[index + 1] for index, part in enumerate(argv) if part == "--ro-bind"]

    bound = binds(shell.arguments(str(home), 9, "true"))
    for entry in (".gitconfig", ".config/git", ".claude/shell-snapshots"):
        assert str(home / entry) in bound
    assert not [path for path in bound if path in {str(home), str(home / ".claude")}]

    def refused(message: str) -> None:
        with pytest.raises(SystemExit) as stopped:
            shell.arguments(str(home), 9, "true")
        assert stopped.value.code == 126
        assert message in capsys.readouterr().err

    # A redirect to the host's runtime sockets, or back to the real home, is refused.
    tricks = {
        home / ".config" / "git": "/run",
        home / ".gitconfig": str(home / ".claude" / ".credentials.json"),
        home / ".claude" / "shell-snapshots": str(home),
    }
    for link, target in tricks.items():
        original = link.with_name(link.name + ".orig")
        link.rename(original)
        link.symlink_to(target)
        refused("symlink")
        link.unlink()
        original.rename(link)
    # A redirect higher up (the .config directory itself) too, whether or not its
    # target holds a git directory.
    (home / ".config").rename(home / "config.orig")
    for target in ("/run", str(home / "config.orig")):
        (home / ".config").symlink_to(target)
        refused("symlink")
        (home / ".config").unlink()
    (home / "config.orig").rename(home / ".config")
    # The wrong type (a directory where git expects its configuration file).
    (home / ".gitconfig").unlink()
    (home / ".gitconfig").mkdir()
    refused("not a file")
    (home / ".gitconfig").rmdir()
    # CLAUDE_CONFIG_DIR is resolved the same way.
    elsewhere = tmp_path / "config"
    (elsewhere / "shell-snapshots").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))
    assert str(elsewhere / "shell-snapshots") in binds(shell.arguments(str(home), 9, "true"))
    (tmp_path / "link").symlink_to(elsewhere)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "link"))
    refused("symlink")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative")
    refused("not absolute")


@pytest.mark.skipif(not _bwrap_usable(), reason="bubblewrap user namespaces unavailable")
def test_loopback_shell_refuses_misuse_and_broad_roots(tmp_path: Path) -> None:
    misuse = subprocess.run(  # noqa: S603 - fixed script
        [str(LOOPBACK_SHELL)], capture_output=True, text=True, check=False
    )
    assert misuse.returncode == 126
    broad = ("/", "/run", "/run/user", "/tmp", "/home", str(Path.home()), "relative")  # noqa: S108
    for root in broad:
        refused = _in_shell("true", tmp_path, root)
        assert refused.returncode == 126, root
        assert "agent-loopback-shell" in refused.stderr
    refused = _in_shell("true", tmp_path, str(tmp_path), readable="/proc/1")
    assert refused.returncode == 126
    # A missing root (no mirrors on this host, say) is skipped.
    assert _in_shell("true", tmp_path, f"{tmp_path}:{tmp_path / 'missing'}").returncode == 0
