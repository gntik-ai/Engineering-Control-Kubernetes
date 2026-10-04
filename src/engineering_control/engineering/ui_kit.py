"""NEW-121 v2: a deterministic, Hermes-side gntik-ui kit client (no model involved).

Models never get an MCP server. Instead Hermes itself runs the digest-pinned gntik-ui MCP
image as a stdio child and speaks MCP JSON-RPC to it directly, calling only an explicit
allowlist of read-only tools. The result is a versioned kit snapshot (stored as evidence
and handed to later flows as data) and a `validate_page` check helper.

Isolation of the child:
- `docker run -i --rm --pull never --network none --read-only --cap-drop ALL
  --security-opt no-new-privileges --name <unique> <image>@<digest> --stdio`;
- the docker CLI is an absolute, root-owned path (never a PATH lookup), the environment is
  explicit and minimal, the working directory is `/`;
- the child runs in its own process group under a hard deadline; on timeout, cancellation
  or any error the whole group is killed and reaped and the named container removed.

Tool output is untrusted data: it is bounded, stored verbatim (after NUL removal) with its
SHA-256, and never interpreted as instructions or parsed for control flow.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import tempfile
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from engineering_control.engineering.models import UiMigrationPolicy
from engineering_control.security.redaction import SecretRedactor

KIT_SNAPSHOT_SCHEMA_VERSION = 1
GENERATOR = "engineering_control.engineering.ui_kit"
UI_MCP_UNAVAILABLE = "UI_MCP_UNAVAILABLE"
MCP_PROTOCOL_VERSION = "2025-06-18"

# Read-only tools only. Never: sync_design_system (git fetch + reset --hard in clone mode),
# scaffold_page / scaffold_preset (generators, not needed for a snapshot), or any tool the
# pinned image may add later.
READ_ONLY_TOOLS = frozenset(
    {
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
)
# The default snapshot: the catalogue per kind plus the rules, tokens, adoption guide and
# the app shell. Templates, blocks and components are added by explicit id.
DEFAULT_SNAPSHOT_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("list_kit", {"kind": "template"}),
    ("list_kit", {"kind": "layout"}),
    ("list_kit", {"kind": "block"}),
    ("list_kit", {"kind": "component"}),
    ("get_rules", {}),
    ("get_tokens", {"theme": "all", "format": "json"}),
    ("get_adoption_guide", {}),
    ("get_app_shell", {"include_html_block": True}),
)
KIT_ITEM_TOOLS = {"template": "get_template", "block": "get_block", "component": "get_component"}
_KIT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

TRUSTED_DOCKER_CANDIDATES = ("/usr/bin/docker", "/usr/local/bin/docker")
KIT_SERVER_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
DEFAULT_DEADLINE_SECONDS = 180.0
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_TOOL_TEXT_BYTES = 512 * 1024
MAX_SNAPSHOT_TEXT_BYTES = 8 * 1024 * 1024
MAX_PAGE_SOURCE_BYTES = 256 * 1024
_STDERR_TAIL_BYTES = 4096
_REAP_SECONDS = 10.0


class KitUnavailable(RuntimeError):  # noqa: N818 - a blocker, not a programming error
    """Docker or the pinned image is missing: the step is BLOCKED (UI_MCP_UNAVAILABLE)."""

    code = UI_MCP_UNAVAILABLE


class KitProtocolError(ValueError):
    """The server answered outside the MCP contract or the bounds."""


class KitToolRefused(ValueError):  # noqa: N818
    """A tool outside the read-only allowlist was requested."""


# --------------------------------------------------------------------------------------
# The pinned launch
# --------------------------------------------------------------------------------------


def _root_controlled(path: Path) -> bool:
    try:
        metadata = path.stat()
    except OSError:
        return False
    return metadata.st_uid == 0 and not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def trusted_docker_executable(
    candidates: Iterable[str] = TRUSTED_DOCKER_CANDIDATES,
) -> str | None:
    """An absolute docker CLI whose file and every parent are root-owned, not g/o-writable."""
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute():
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            continue
        chain = (resolved, *resolved.parents, *path.parent.parents, path.parent)
        if all(_root_controlled(item) for item in chain):
            return str(resolved)
    return None


DEFAULT_DOCKER_ENDPOINT = "unix:///var/run/docker.sock"
_UNIX_ENDPOINT = re.compile(r"unix:///[A-Za-z0-9._/-]{1,200}")


def docker_endpoint(source: Mapping[str, str] | None = None) -> str:
    """The local daemon socket: the worker's unix:// DOCKER_HOST (e.g. a rootless daemon)
    or the system socket. A TCP, SSH or any other endpoint is refused."""
    value = (os.environ if source is None else source).get("DOCKER_HOST", "")
    if not value:
        return DEFAULT_DOCKER_ENDPOINT
    if _UNIX_ENDPOINT.fullmatch(value) and ".." not in value:
        return value
    raise KitUnavailable("refusing a non-local docker endpoint (DOCKER_HOST must be unix://)")


def private_docker_config() -> str:
    """A fresh, empty, 0700 directory owned by this user: Docker's config and HOME, so no
    saved context, credential helper or config.json of the user is ever consulted."""
    directory = tempfile.mkdtemp(prefix="hermes-ui-kit-docker-")
    os.chmod(directory, 0o700)
    metadata = os.lstat(directory)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or os.listdir(directory)
    ):
        shutil.rmtree(directory, ignore_errors=True)
        raise KitUnavailable("could not create a private docker config directory")
    return directory


def kit_environment(endpoint: str, config_dir: str) -> dict[str, str]:
    """The explicit, minimal environment of every docker invocation (nothing inherited)."""
    if not _UNIX_ENDPOINT.fullmatch(endpoint) or ".." in endpoint:
        raise ValueError("the docker endpoint must be a unix:// socket")
    if not os.path.isabs(config_dir):
        raise ValueError("the docker config directory must be absolute")
    return {
        "PATH": KIT_SERVER_PATH,
        "HOME": config_dir,
        "DOCKER_CONFIG": config_dir,
        "DOCKER_CONTEXT": "default",
        "DOCKER_HOST": endpoint,
    }


def docker_global_flags(endpoint: str, config_dir: str) -> list[str]:
    return ["--config", config_dir, "-H", endpoint]


def kit_reference(image: str, digest: str) -> str:
    if not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?(?:/[a-z0-9][a-z0-9._-]*)+", image
    ):
        raise ValueError("kit image must be a tagless repository reference")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("kit image must be pinned by sha256 digest")
    return f"{image}@{digest}"


def kit_command(
    docker: str, image: str, digest: str, *, name: str, endpoint: str, config_dir: str
) -> list[str]:
    if not os.path.isabs(docker):
        raise ValueError("the docker CLI must be an absolute path")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", name):
        raise ValueError("invalid container name")
    kit_environment(endpoint, config_dir)  # validates both
    return [
        docker,
        *docker_global_flags(endpoint, config_dir),
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
        name,
        kit_reference(image, digest),
        "--stdio",
    ]


# --------------------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------------------


def _bounded_text(value: str, limit: int) -> tuple[str, bool]:
    clean = value.replace("\x00", "")
    data = clean.encode("utf-8")
    if len(data) <= limit:
        return clean, False
    return data[:limit].decode("utf-8", errors="ignore"), True


class GntikUiKitClient:
    """One MCP stdio session with a hard deadline; use as an async context manager."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        preflight: Sequence[str] | None = None,
        cleanup: Sequence[str] | None = None,
        cwd: str = "/",
        private_dirs: Sequence[str] = (),
        shutdown_seconds: float = _REAP_SECONDS,
    ) -> None:
        if not argv or not os.path.isabs(argv[0]):
            raise ValueError("the kit server command must be an absolute executable")
        self.argv = list(argv)
        self.environment = dict(environment)
        self.deadline_seconds = deadline_seconds
        self.preflight = list(preflight) if preflight else None
        self.cleanup_argv = list(cleanup) if cleanup else None
        self.cwd = cwd
        self.private_dirs = list(private_dirs)
        self.shutdown_seconds = shutdown_seconds
        self.removed_container = False
        self.process: asyncio.subprocess.Process | None = None
        self.server_info: dict[str, Any] = {}
        self.protocol_version = ""
        self.tools: list[str] = []
        self._next_id = 0
        self._stderr = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        self._deadline = 0.0
        self._escalate = asyncio.Event()
        self._closed = False
        self._launch_attempted = False

    @classmethod
    def pinned(
        cls,
        image: str,
        digest: str,
        *,
        docker_executable: str | None = None,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    ) -> GntikUiKitClient:
        docker = docker_executable or trusted_docker_executable()
        if docker is None:
            raise KitUnavailable(
                "no root-owned docker CLI found (/usr/bin/docker, /usr/local/bin/docker)"
            )
        endpoint = docker_endpoint()
        config_dir = private_docker_config()
        name = f"hermes-ui-kit-{uuid.uuid4().hex[:16]}"
        flags = docker_global_flags(endpoint, config_dir)
        try:
            argv = kit_command(
                docker, image, digest, name=name, endpoint=endpoint, config_dir=config_dir
            )
        except BaseException:
            shutil.rmtree(config_dir, ignore_errors=True)
            raise
        return cls(
            argv,
            environment=kit_environment(endpoint, config_dir),
            deadline_seconds=deadline_seconds,
            preflight=[docker, *flags, "image", "inspect", "--format", "{{.Id}}", argv[-2]],
            cleanup=[docker, *flags, "rm", "-f", name],
            private_dirs=[config_dir],
        )

    @classmethod
    def from_policy(cls, policy: UiMigrationPolicy, **kwargs: Any) -> GntikUiKitClient:
        return cls.pinned(policy.mcp.image, policy.mcp.digest, **kwargs)

    # -- lifecycle -----------------------------------------------------------------------

    async def __aenter__(self) -> GntikUiKitClient:
        loop = asyncio.get_running_loop()
        self._deadline = loop.time() + self.deadline_seconds
        try:
            if self.preflight is not None:
                await self._run_preflight()
            await self._start()
            await self._initialize()
        except BaseException:
            await self.close(force=True)
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close(force=exc_info[0] is not None)

    def _remaining(self) -> float:
        remaining = self._deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("gntik-ui kit session exceeded its deadline")
        return remaining

    async def _run_preflight(self) -> None:
        assert self.preflight is not None
        process = await asyncio.create_subprocess_exec(
            *self.preflight,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=self.environment,
            cwd=self.cwd,
            start_new_session=True,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=min(30.0, self._remaining())
            )
        except BaseException:
            await _kill_group(process)
            raise
        if process.returncode == 0:
            return
        detail = stderr.decode("utf-8", errors="replace").strip().lower()
        if "permission denied" in detail:
            reason = "the worker user has no access to the docker daemon"
        elif "cannot connect" in detail or "is the docker daemon running" in detail:
            reason = "the docker daemon is unreachable"
        elif "no such image" in detail:
            reason = f"image {self.preflight[-1]} is not pre-pulled"
        else:
            reason = "docker image inspect failed"
        raise KitUnavailable(reason)

    async def _start(self) -> None:
        # NEW-120 review 2: a cancellation can land after the Docker CLI was spawned but
        # before `self.process` is assigned; the launch attempt alone makes cleanup remove
        # the named container.
        self._launch_attempted = True
        self.process = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.environment,
            cwd=self.cwd,
            start_new_session=True,  # its own process group, killed as a whole
            limit=MAX_LINE_BYTES,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while chunk := await self.process.stderr.read(4096):
            self._stderr.extend(chunk)
            del self._stderr[:-_STDERR_TAIL_BYTES]

    async def close(self, *, force: bool = False) -> None:
        """Stop the session. Cancellation-safe: every cleanup step completes (shielded) and
        only then is a cancellation propagated; a cancellation escalates to a forced stop."""
        if self._closed:
            return
        if force:
            self._escalate.set()
        shutdown = asyncio.ensure_future(self._shutdown())
        cancelled = False
        while not shutdown.done():
            try:
                await asyncio.shield(shutdown)
            except asyncio.CancelledError:
                cancelled = True
                self._escalate.set()
        self._closed = True
        if cancelled:
            raise asyncio.CancelledError
        shutdown.result()

    async def _shutdown(self) -> None:
        process = self.process
        clean = process is not None
        if process is not None:
            if process.returncode is None and not self._escalate.is_set():
                if process.stdin is not None:
                    with contextlib.suppress(OSError, RuntimeError):
                        process.stdin.close()
                await self._wait_or_escalate(process)
            if process.returncode is None:
                await _kill_group(process)
            clean = process.returncode == 0 and not self._escalate.is_set()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None
        try:
            # `--rm` removes only an exited container: whenever the CLI had to be killed,
            # failed or the session was aborted, remove the named container explicitly.
            launched = process is not None or self._launch_attempted
            if launched and not clean and self.cleanup_argv is not None:
                await _best_effort(self.cleanup_argv, self.environment, self.cwd)
                self.removed_container = True
        finally:
            for directory in self.private_dirs:
                shutil.rmtree(directory, ignore_errors=True)

    async def _wait_or_escalate(self, process: asyncio.subprocess.Process) -> None:
        waiter = asyncio.ensure_future(process.wait())
        escalation = asyncio.ensure_future(self._escalate.wait())
        try:
            await asyncio.wait(
                {waiter, escalation},
                timeout=self.shutdown_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for pending in (waiter, escalation):
                pending.cancel()
            await asyncio.gather(waiter, escalation, return_exceptions=True)

    # -- JSON-RPC ------------------------------------------------------------------------

    async def _send(self, message: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        await asyncio.wait_for(self.process.stdin.drain(), timeout=self._remaining())

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        assert self.process is not None and self.process.stdout is not None
        while True:
            try:
                line = await asyncio.wait_for(
                    self.process.stdout.readline(), timeout=self._remaining()
                )
            except ValueError as error:  # a line over MAX_LINE_BYTES
                raise KitProtocolError("kit server message exceeds the size bound") from error
            if not line:
                raise KitProtocolError("kit server closed the session")
            try:
                message = json.loads(line)
            except json.JSONDecodeError as error:
                raise KitProtocolError("kit server sent a non-JSON message") from error
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue  # notifications, logs, other ids
            if "error" in message:
                raise KitProtocolError(f"kit server refused {method}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise KitProtocolError(f"kit server sent no result for {method}")
            return result

    async def _initialize(self) -> None:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "engineering-control", "version": "1"},
            },
        )
        info = result.get("serverInfo")
        self.server_info = {
            key: str(info.get(key))[:80]
            for key in ("name", "version")
            if isinstance(info, dict) and key in info
        }
        self.protocol_version = str(result.get("protocolVersion", ""))[:40]
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = await self._request("tools/list", {})
        tools = listed.get("tools")
        if not isinstance(tools, list):
            raise KitProtocolError("kit server sent no tool list")
        self.tools = sorted(
            str(tool.get("name"))[:80]
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        )

    async def call(self, tool: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Call one allowlisted read-only tool; the text is bounded, untrusted data."""
        if tool not in READ_ONLY_TOOLS:
            raise KitToolRefused(f"kit tool {tool!r} is not on the read-only allowlist")
        if tool not in self.tools:
            raise KitProtocolError(f"the pinned kit server does not offer {tool!r}")
        payload = dict(arguments or {})
        result = await self._request("tools/call", {"name": tool, "arguments": payload})
        content = result.get("content")
        texts = [
            item["text"]
            for item in (content if isinstance(content, list) else [])
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        bounded, truncated = _bounded_text("\n".join(texts), MAX_TOOL_TEXT_BYTES)
        # Redact first: the stored text is exactly what is hashed and counted.
        text = str(SecretRedactor().redact_text(bounded).value)
        return {
            "tool": tool,
            "arguments": payload,
            "is_error": result.get("isError") is True,
            "text": text,
            "truncated": truncated,
            "bytes": len(text.encode("utf-8")),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }


async def _kill_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the whole process group (the child and anything it spawned), then reap."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(process.wait()), timeout=_REAP_SECONDS)


async def _best_effort(argv: Sequence[str], environment: Mapping[str, str], cwd: str) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=dict(environment),
            cwd=cwd,
            start_new_session=True,
        )
    except OSError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=20)
    except BaseException:
        await _kill_group(process)


# --------------------------------------------------------------------------------------
# Snapshot and checks
# --------------------------------------------------------------------------------------


def snapshot_plan(
    *,
    templates: Iterable[str] = (),
    blocks: Iterable[str] = (),
    components: Iterable[str] = (),
) -> list[tuple[str, dict[str, Any]]]:
    plan = list(DEFAULT_SNAPSHOT_CALLS)
    for kind, ids in (("template", templates), ("block", blocks), ("component", components)):
        for item in sorted(set(ids)):
            if not _KIT_ID.fullmatch(item):
                raise ValueError(f"invalid gntik-ui {kind} id")
            plan.append((KIT_ITEM_TOOLS[kind], {"id": item, "include_source": True}))
    return plan


def snapshot_hash(document: Mapping[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "snapshot_sha256"}
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def collect_kit_snapshot(
    client: GntikUiKitClient,
    *,
    image: str,
    digest: str,
    gntik_ui_version: str,
    plan: Sequence[tuple[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """A versioned kit snapshot: each allowlisted tool's output plus the pins."""
    calls: list[dict[str, Any]] = []
    total = 0
    for tool, arguments in plan if plan is not None else DEFAULT_SNAPSHOT_CALLS:
        call = await client.call(tool, arguments)
        total += call["bytes"]
        if total > MAX_SNAPSHOT_TEXT_BYTES:
            raise KitProtocolError("kit snapshot exceeds its total size bound")
        calls.append(call)
    document: dict[str, Any] = {
        "schema_version": KIT_SNAPSHOT_SCHEMA_VERSION,
        "generator": GENERATOR,
        "image": kit_reference(image, digest),
        "digest": digest,
        "gntik_ui_version": gntik_ui_version,
        "server_info": client.server_info,
        "protocol_version": client.protocol_version,
        "tools_available": client.tools,
        "tools_allowlisted": sorted(READ_ONLY_TOOLS),
        "calls": calls,
        "summary": {
            "calls": len(calls),
            "errors": sum(1 for call in calls if call["is_error"]),
            "truncated": sum(1 for call in calls if call["truncated"]),
            "text_bytes": total,
        },
    }
    return finalize_snapshot(document)


def finalize_snapshot(document: dict[str, Any]) -> dict[str, Any]:
    """Redact the whole snapshot, then hash and count exactly what will be stored."""
    body = {key: value for key, value in document.items() if key != "snapshot_sha256"}
    redacted: dict[str, Any] = SecretRedactor().redact(body).value
    for call in redacted["calls"]:
        data = str(call["text"]).encode("utf-8")
        call["bytes"] = len(data)
        call["sha256"] = hashlib.sha256(data).hexdigest()
    redacted["summary"]["text_bytes"] = sum(call["bytes"] for call in redacted["calls"])
    redacted["snapshot_sha256"] = snapshot_hash(redacted)
    return redacted


async def validate_page(
    client: GntikUiKitClient, source: str, *, filename: str | None = None
) -> dict[str, Any]:
    """Run the kit's `validate_page` rule check on one page source (deterministic).

    Returns the tool call record; its text is untrusted data for the caller to store or
    show, never instructions.
    """
    if len(source.encode("utf-8")) > MAX_PAGE_SOURCE_BYTES:
        raise ValueError("page source exceeds the validate_page size bound")
    arguments: dict[str, Any] = {"source": source}
    if filename is not None:
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", filename) or ".." in filename:
            raise ValueError("invalid page filename")
        arguments["filename"] = filename
    return await client.call("validate_page", arguments)
