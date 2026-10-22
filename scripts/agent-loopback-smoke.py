#!/usr/bin/env python3
"""NEW-228: live check of the loopback-only Claude sandbox, before a deploy is trusted.

NEW-221 (2026-10-18): a sandbox change broke every maker run in production because no
live bubblewrap test ran before the deploy. Run this on the worker after deploying, as
claude-runner (no sudo inside; it never needs root):

  shell   the loopback shell (scripts/agent-loopback-shell), invoked exactly as Claude
          Code applies CLAUDE_CODE_SHELL_PREFIX; no model call.
  claude  one real Claude checker run with the loopback shell prefix, in a scratch copy
          of a read-only worktree (the checker_scratch_copy path).

Inside the sandbox the probe must bind and connect on 127.0.0.1 and ::1 and fetch a page
from a loopback HTTP server (with Chromium too, given --chromium). Every egress target
(1.1.1.1, a public IPv6 address, the metadata address, UDP, the configured DNS servers,
this host's own addresses and its loopback, where this script listens) must fail with an
explicit network refusal from inside the namespace: ENETUNREACH, ECONNREFUSED,
EHOSTUNREACH or EAFNOSUPPORT. A connection, an HTTP or TLS answer, a timeout or any other
error is a FAIL (inconclusive is not blocked). Unix sockets this script opens outside
(in a directory the sandbox does not mount, and in the run's TMPDIR parent) must be
absent inside, and a walk of the visible filesystem (not /proc or /sys) must find no
socket outside the sandbox's own tmpfs mounts and no directory it can enter but not list
(inconclusive). Prints one JSON line; exit 0 only on PASS.

`sudo -u claude-runner` runs outside the worker unit's sandbox, so it can pass where the
worker fails (gntik-ui #247, 2026-10-22: the unit's ProtectKernelTunables= mounts made the
kernel refuse bubblewrap's procfs). As root, `worker-unit` runs the same smoke in a
transient unit with every [Service] setting of the worker in this release (the template,
then its drop-ins in systemd's order), lifecycle keys aside, and PASSes only if, besides
the sandbox checks, the loaded worker unit has the same effective settings (systemctl show,
no daemon-reload pending) and the running worker's /proc carries no mounts over it (it was
restarted with them). On the worker:

  sudo /opt/engineering-control/venv/bin/python \
    /opt/engineering-control/source/scripts/agent-loopback-smoke.py worker-unit \
    [--instance claude] [--expectation-only] [--dry-run] -- shell|claude <smoke options>

A third production check reads the running worker process itself (/proc/<MainPID>:
credentials, capabilities, no_new_privs, seccomp, and the mounts the sandbox makes) against
a probe process started with this release's settings: unit settings alone cannot show what
the running invocation started with (a privileged ExecStart restored and reloaded without
a restart). Out of scope: a root operator deliberately forging kernel-level state.

--expectation-only (before installing the release's units) skips the production checks
and reports EXPECTATION_PASS, never PASS: it is not production acceptance.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from engineering_control.adapters.agent_sandbox import (
    AgentSandbox,
    copy_worktree,
    loopback_shell_problem,
    remove_tree,
    verified_loopback_shell,
)
from engineering_control.adapters.claude.client import ClaudeCLIAdapter
from engineering_control.config import Settings
from engineering_control.models.core import AgentRequest
from engineering_control.models.enums import ModelClass, Project, Provider, RunStatus

PROBE_NAME = "hermes_loopback_selftest.py"
RESULT_NAME = "hermes-loopback-selftest.json"
HOME_MARKER = ".hermes-loopback-selftest"
# Run inside the sandbox. Standard library only; every outcome is recorded, none raised.
PROBE = r'''
"""Hermes sandbox acceptance self-test (NEW-228), run by the operator at deploy.

It checks that this sandbox allows loopback networking only: local servers work, and
nothing outside the machine's own loopback is reachable. It sends no data anywhere."""
import errno, json, os, socket, stat, struct, subprocess, sys, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REFUSED = {errno.ENETUNREACH: "ENETUNREACH", errno.ECONNREFUSED: "ECONNREFUSED",
           errno.EHOSTUNREACH: "EHOSTUNREACH", errno.EAFNOSUPPORT: "EAFNOSUPPORT"}

def outcome(function):
    try:
        function()
        return "ok"
    except Exception as error:  # noqa: BLE001
        return f"error: {type(error).__name__}: {error}"[:160]

def refusal(function, accepted=REFUSED):
    """'refused:<errno>' only for an explicit refusal; anything else is not blocked."""
    try:
        function()
        return "reached"
    except OSError as error:
        if error.errno in accepted:
            return f"refused:{accepted[error.errno]}"
        return f"inconclusive: {type(error).__name__}: {error}"[:160]
    except Exception as error:  # noqa: BLE001
        return f"inconclusive: {type(error).__name__}: {error}"[:160]

def loopback(family, host):
    server = socket.socket(family)
    server.bind((host, 0))
    server.listen()
    client = socket.create_connection((host, server.getsockname()[1]), timeout=3)
    peer, _ = server.accept()
    client.sendall(b"x")
    if peer.recv(1) != b"x":
        raise RuntimeError("no echo")

def connect(host, port):
    return lambda: socket.create_connection((host, port), timeout=4).close()

def udp_dns(server):
    def query():
        family = socket.AF_INET6 if ":" in server else socket.AF_INET
        client = socket.socket(family, socket.SOCK_DGRAM)
        client.settimeout(4)
        client.connect((server, 53))
        # A standard query for example.com A; an ICMP error surfaces on recv.
        client.send(struct.pack(">HHHHHH", 0x4E28, 0x0100, 1, 0, 0, 0)
                    + b"\x07example\x03com\x00\x00\x01\x00\x01")
        client.recv(512)
    return query

def unix(path):
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(4)
    client.connect(path)

def nameservers():
    found = []
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    found.append(parts[1].split("%")[0])
    except OSError:
        pass
    return found

def own_tmpfs(path, mounts):
    """Whether the mount holding ``path`` is one of the sandbox's own tmpfs mounts."""
    point, fstype = "", ""
    for mount, kind in mounts:  # the last, longest match is the visible mount
        if (path == mount or path.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(point):
            point, fstype = mount, kind
    scratch = ("/tmp", "/var/tmp", "/run", "/dev/shm", "/dev", os.path.expanduser("~"))
    return fstype == "tmpfs" and point in scratch

def socket_walk(limit=1_500_000):
    """Socket inodes visible outside the sandbox's own tmpfs mounts (/proc, /sys skipped)."""
    mounts = []
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            fields, kind = line.split(" - ", 1)
            point = fields.split()[4].encode().decode("unicode_escape")
            mounts.append((point, kind.split()[0]))
    found, unlisted, entries, stack = [], [], 0, ["/"]
    while stack:
        top = stack.pop()
        try:
            with os.scandir(top) as listing:
                items = list(listing)
        except OSError as error:
            # A directory that can be entered but not listed may hold a socket whose name
            # a command could know: the walk cannot vouch for it (inconclusive). One that
            # cannot be entered either is sealed: nothing below it is reachable.
            if error.errno in (errno.EACCES, errno.EPERM) and os.access(top, os.X_OK):
                unlisted.append(top)
            continue
        for item in items:
            entries += 1
            if entries > limit:
                return found, unlisted, entries, True
            if item.path in ("/proc", "/sys"):
                continue
            try:
                info = item.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISSOCK(info.st_mode) and not own_tmpfs(item.path, mounts):
                found.append(item.path)
            elif stat.S_ISDIR(info.st_mode):
                stack.append(item.path)
    return found, unlisted, entries, False

class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (b"<html><body><h1>hermes-loopback-ok</h1><script>document.body"
                b".insertAdjacentHTML('beforeend','<p>hermes-js-ran</p>')</script></body></html>")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

class Server6(ThreadingHTTPServer):
    address_family = socket.AF_INET6

settings = json.loads(sys.argv[1])
results = {"netns": os.readlink("/proc/self/ns/net")}
results["loopback4"] = outcome(lambda: loopback(socket.AF_INET, "127.0.0.1"))
results["loopback6"] = outcome(lambda: loopback(socket.AF_INET6, "::1"))
server = Server6(("::1", 0), Page)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://localhost:{server.server_address[1]}/"
def fetch(target):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(target, timeout=6) as response:
        if b"hermes-loopback-ok" not in response.read():
            raise RuntimeError("unexpected page")
results["http_localhost"] = outcome(lambda: fetch(url))
if settings.get("chromium"):
    def chromium():
        page = subprocess.run(
            [settings["chromium"], "--headless", "--no-sandbox", "--disable-gpu",
             "--dump-dom", url], capture_output=True, text=True, timeout=90)
        if "hermes-js-ran" not in page.stdout:
            raise RuntimeError(f"exit {page.returncode}: {page.stderr[-200:]}")
    results["chromium_localhost"] = outcome(chromium)
blocked = {
    "public4": connect("1.1.1.1", 443),
    "public6": connect("2606:4700:4700::1111", 443),
    "metadata": connect("169.254.169.254", 80),
    "udp_public": lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(
        b"x", ("1.1.1.1", 53)),
}
for address in nameservers() or ["127.0.0.53"]:
    blocked[f"dns:{address}"] = udp_dns(address)
for address in settings["host_addresses"]:
    blocked[f"host:{address}"] = connect(address, settings["host_port"])
for name, function in blocked.items():
    results[f"blocked:{name}"] = refusal(function)
# A pathname unix socket crosses namespaces: those this script opened must be absent.
for name, path in settings["unix_listeners"].items():
    results[f"blocked:unix:{name}"] = refusal(
        lambda path=path: unix(path), {errno.ENOENT: "ENOENT"})
sockets, unlisted, entries, truncated = socket_walk()
results["socket_walk"] = {"sockets": sockets[:50], "count": len(sockets),
                          "unlisted": unlisted[:50], "entries": entries,
                          "truncated": truncated}
with open(os.path.join(os.path.expanduser("~"), settings["home_marker"]), "w") as handle:
    handle.write("x")  # must not reach the runner's real home
server.shutdown()
with open(settings["result"], "w", encoding="utf-8") as handle:
    json.dump(results, handle, sort_keys=True)
print("hermes loopback self-test finished")
'''


class HostListener:
    """A dual-stack TCP listener on every host address and unix socket listeners, all
    reachable outside the sandbox."""

    def __init__(self, extra_unix_directories: tuple[Path, ...] = ()) -> None:
        # A directory of its own the sandbox does not mount (a unix path is <= 107 bytes).
        self.unix_directory = tempfile.mkdtemp(prefix="hl-", dir="/tmp")
        self.unix: dict[str, socket.socket] = {}
        self.paths: dict[str, str] = {}
        for name, directory in (("host_tmp", Path(self.unix_directory)),) + tuple(
            (f"near:{index}", path) for index, path in enumerate(extra_unix_directories)
        ):
            path = str(directory / f"hl-{uuid4().hex[:6]}.sock")
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(path)
            listener.listen(4)
            self.unix[name], self.paths[name] = listener, path
        self.socket = socket.socket(socket.AF_INET6)
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        self.socket.bind(("::", 0))
        self.socket.listen(16)
        self.port = int(self.socket.getsockname()[1])
        threading.Thread(target=self._accept, daemon=True).start()
        socket.create_connection(("127.0.0.1", self.port), timeout=3).close()

    def _accept(self) -> None:
        while True:
            try:
                peer, _ = self.socket.accept()
            except OSError:
                return
            peer.close()

    def close(self) -> None:
        self.socket.close()
        for name, listener in self.unix.items():
            listener.close()
            Path(self.paths[name]).unlink(missing_ok=True)
        shutil.rmtree(self.unix_directory, ignore_errors=True)


def host_addresses() -> list[str]:
    hostname = shutil.which("hostname") or "/usr/bin/hostname"
    listed = subprocess.run(  # noqa: S603 - fixed read-only command
        [hostname, "-I"], capture_output=True, text=True, check=False, timeout=10
    ).stdout.split()
    return ["127.0.0.1", "::1", *(item for item in listed if not item.startswith("fe80"))]


def probe_arguments(directory: Path, listener: HostListener, chromium: str | None) -> str:
    settings = {
        "result": str(directory / RESULT_NAME),
        "host_port": listener.port,
        "host_addresses": host_addresses(),
        "unix_listeners": listener.paths,
        "chromium": chromium,
        "home_marker": HOME_MARKER,
    }
    (directory / PROBE_NAME).write_text(PROBE, encoding="utf-8")
    return json.dumps(settings, separators=(",", ":"))


def evaluate(directory: Path, *, require_chromium: bool) -> dict[str, Any]:
    path = directory / RESULT_NAME
    if not path.is_file():
        return {"status": "FAIL", "problems": ["the probe did not run (no result file)"]}
    results = json.loads(path.read_text(encoding="utf-8"))
    problems = [
        f"{name}: {results.get(name)}"
        for name in (
            "loopback4",
            "loopback6",
            "http_localhost",
            *(("chromium_localhost",) if require_chromium else ()),
        )
        if results.get(name) != "ok"
    ]
    if results.get("netns") == os.readlink("/proc/self/ns/net"):
        problems.append("netns: the probe ran in this host's network namespace")
    blocked = {name: value for name, value in results.items() if name.startswith("blocked:")}
    # Only an explicit refusal counts; "reached" and "inconclusive" both fail.
    problems.extend(
        f"{name}: {value}"
        for name, value in blocked.items()
        if not str(value).startswith("refused:")
    )
    for required in ("blocked:host:127.0.0.1", "blocked:unix:host_tmp"):
        if required not in blocked:
            problems.append(f"{required} was not probed")
    walk = results.get("socket_walk") or {}
    if walk.get("truncated") or not walk.get("entries"):
        problems.append(f"socket walk inconclusive: {walk.get('entries')} entries")
    if walk.get("unlisted"):
        problems.append(f"socket walk inconclusive: traverse-only {walk.get('unlisted')}")
    if walk.get("count"):
        problems.append(f"host sockets visible: {walk.get('sockets')}")
    home_marker = Path.home() / HOME_MARKER
    if home_marker.exists():
        home_marker.unlink()
        problems.append("a write to the home inside reached the runner's real home")
    return {
        "status": "FAIL" if problems else "PASS",
        "problems": problems,
        "reachable": [
            name
            for name in ("loopback4", "loopback6", "http_localhost", "chromium_localhost")
            if results.get(name) == "ok"
        ],
        "blocked": len(blocked),
        "socket_walk_entries": walk.get("entries"),
    }


def _request(directory: Path, prompt: str) -> AgentRequest:
    return AgentRequest(
        project=Project.GNTIK_UI,
        role="code-reviewer",
        provider=Provider.CLAUDE,
        model_class=ModelClass.WORKER,
        prompt=prompt,
        working_directory=str(directory),
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "summary"],
            "properties": {
                "status": {"type": "string", "enum": ["DONE", "NOT_RUN"]},
                "summary": {"type": "string", "maxLength": 400},
            },
        },
        timeout_seconds=900,
        effort="low",
        writable=False,
        allowed_tools=("Read", "Bash"),
    )


def _prompt(arguments: str) -> str:
    return (
        "This is the Hermes operator's deploy-time sandbox acceptance check (NEW-228). The "
        f"file {PROBE_NAME} in the current directory is a self-test that verifies this "
        "sandbox allows loopback networking only; it sends no data anywhere. Run exactly "
        "this one shell command, unchanged, once, and nothing else:\n"
        f"python3 {PROBE_NAME} '{arguments}'\n"
        "Do not edit or inspect other files and do not retry. Return status DONE once the "
        "command ran (whatever it printed), or NOT_RUN with the reason."
    )


def _readable(chromium: str | None) -> tuple[str, ...]:
    """The browser's install directory (on the worker it is under /opt/engineering-browser,
    mounted anyway; a development host may keep it elsewhere)."""
    return (str(Path(chromium).resolve().parent),) if chromium else ()


def run_shell(work: Path, shell: Path, chromium: str | None) -> dict[str, Any]:
    probe_dir = work / "probe"
    probe_dir.mkdir()
    # One host socket sits right beside the mounted directory (in its unmounted parent).
    listener = HostListener((work,))
    try:
        arguments = probe_arguments(probe_dir, listener, chromium)
        command = f"python3 {PROBE_NAME} '{arguments}'"
        quoted = "'" + command.replace("'", "'\\''") + "'"
        environment = {
            **os.environ,
            "HERMES_LOOPBACK_WRITABLE": str(probe_dir),
            "HERMES_LOOPBACK_READABLE": ":".join(_readable(chromium)),
        }
        completed = subprocess.run(  # noqa: S603 - fixed shell, script-owned command
            ["/bin/bash", "-c", f"'{shell}' {quoted}"],
            cwd=probe_dir,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    finally:
        listener.close()
    verdict = evaluate(probe_dir, require_chromium=chromium is not None)
    if completed.returncode:
        verdict["status"] = "FAIL"
        verdict["problems"].append(f"shell exit {completed.returncode}: {completed.stderr[-300:]}")
    return verdict


async def run_claude(
    work: Path, executable: str, shell: Path, chromium: str | None, *, allow_unowned_shell: bool
) -> dict[str, Any]:
    problem = loopback_shell_problem(shell)
    if problem is not None and not allow_unowned_shell:
        return {"status": "FAIL", "problems": [f"AGENT_SANDBOX_LOOPBACK_UNAVAILABLE: {problem}"]}
    resolved = verified_loopback_shell(shell)[0] or os.path.realpath(shell)  # noqa: ASYNC240
    source, copy = work / "maker-worktree", work / "copy" / "maker-worktree"
    source.mkdir()
    (source / "README.md").write_text("read-only maker worktree\n", encoding="utf-8")
    source.chmod(0o555)  # the checker cannot write the maker's worktree
    copy.parent.mkdir()
    copy_worktree(source, copy)
    temp_root = work / "claude-temp"
    listener = HostListener((work,))
    try:
        arguments = probe_arguments(copy, listener, chromium)
        adapter = ClaudeCLIAdapter(executable=executable, temp_root=str(temp_root))
        result = await adapter.execute(
            _request(copy, _prompt(arguments)),
            sandbox=AgentSandbox(
                loopback_network=True,
                checker_scratch_copy=True,
                loopback_shell=str(resolved),
                readable_roots=(str(source), *_readable(chromium)),
            ),
        )
    finally:
        listener.close()
        source.chmod(0o755)
    verdict = evaluate(copy, require_chromium=chromium is not None)
    if result.status is not RunStatus.SUCCESS:
        verdict["status"] = "FAIL"
        verdict.setdefault("problems", []).append(
            f"claude {result.status.value}: {result.summary[-300:]}"
        )
    verdict["usage"] = result.usage.model_dump(mode="json")
    return verdict


# The release's unit files (the worker installs them unchanged, bootstrap-worker.sh).
UNIT_ROOT = Path(__file__).resolve().parents[1] / "systemd"
WORKER_TEMPLATE = "engineering-worker@.service"
# The worker process's own start, notification and restart keys; the smoke is one command.
# Every other [Service] assignment reaches the transient unit unchanged, in order.
LIFECYCLE_KEYS = frozenset(
    {
        "Type",
        "NotifyAccess",
        "WatchdogSec",
        "WatchdogSignal",
        "TimeoutSec",
        "TimeoutStartSec",
        "TimeoutStopSec",
        "Restart",
        "RestartSec",
        "RemainAfterExit",
        "PIDFile",
        "ExecCondition",
        "ExecStartPre",
        "ExecStart",
        "ExecStartPost",
        "ExecReload",
        "ExecStop",
        "ExecStopPost",
    }
)


def worker_unit_files(instance: str, unit_root: Path = UNIT_ROOT) -> list[Path]:
    """The template, then the drop-ins (template's and instance's) sorted by file name; an
    instance drop-in replaces the template's of the same name, as in systemd."""
    drop_ins: dict[str, Path] = {}
    for directory in ("engineering-worker@.service.d", f"engineering-worker@{instance}.service.d"):
        if (unit_root / directory).is_dir():
            drop_ins.update({path.name: path for path in (unit_root / directory).glob("*.conf")})
    return [unit_root / WORKER_TEMPLATE, *(drop_ins[name] for name in sorted(drop_ins))]


def _expand(value: str, instance: str, origin: Path) -> str:
    expanded, index = [], 0
    while index < len(value):
        if value[index] != "%":
            expanded.append(value[index])
            index += 1
            continue
        specifier = value[index + 1 : index + 2]
        if not specifier.isalpha() and specifier != "%":
            expanded.append("%")  # a literal percentage (CPUQuota=250%)
            index += 1
            continue
        if specifier not in ("i", "I", "%"):
            raise SystemExit(f"{origin}: specifier %{specifier} is not supported: {value}")
        expanded.append(instance if specifier in "iI" else "%")
        index += 2
    return "".join(expanded)


def worker_unit_properties(instance: str, unit_root: Path = UNIT_ROOT) -> list[str]:
    """Every [Service] assignment of the worker unit, lifecycle keys aside, in the order
    systemd applies them (a later scalar wins, an empty one resets a list), %i expanded."""
    if not instance.isidentifier():
        raise SystemExit(f"invalid worker instance {instance!r}")
    properties: list[str] = []
    for path in worker_unit_files(instance, unit_root):
        section = ""
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.endswith("\\"):
                raise SystemExit(f"{path}: continuation lines are not supported")
            if line.startswith("["):
                section = line
            elif section == "[Service]" and "=" in line:
                key, value = (part.strip() for part in line.split("=", 1))
                if key not in LIFECYCLE_KEYS:
                    properties.append(f"{key}={_expand(value, instance, path)}")
    return properties


def worker_unit_command(
    instance: str, smoke: list[str], unit_root: Path = UNIT_ROOT, unit: str | None = None
) -> list[str]:
    """systemd-run of this smoke inside a transient unit sandboxed like the worker."""
    if not smoke or smoke[0] not in ("shell", "claude"):
        raise SystemExit("worker-unit: give the smoke after --: shell|claude <options>")
    command = [
        "systemd-run",
        "--quiet",
        "--pipe",
        "--wait",
        "--collect",
        "--service-type=exec",
        f"--unit={unit or f'engineering-loopback-smoke-{instance}-{uuid4().hex[:8]}'}",
        "--property=RuntimeMaxSec=1800",
    ]
    command += [f"--property={item}" for item in worker_unit_properties(instance, unit_root)]
    return [*command, "--", sys.executable, str(Path(__file__).resolve()), *smoke]


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
WORKER_STATE = (
    "LoadState",
    "ActiveState",
    "NeedDaemonReload",
    "MainPID",
    "FragmentPath",
    "DropInPaths",
    "ExecMainStartTimestamp",
    "ExecMainStartTimestampMonotonic",
)
# Bus properties (Unit and Service interfaces) not compared between the loaded worker and
# the probe unit (a oneshot /bin/true made from this release's settings). Each group says
# why it cannot change the execution context of the worker's processes; every other one (all of
# systemd.exec and systemd.resource-control: users, credentials, capabilities, namespaces
# and the units whose namespaces are joined, mounts, root, filters, Slice, Memory*/Tasks*/
# CPU* limits, Delegate) must be equal, whether or not the release's unit files set it.
# The commands themselves (Exec*) are checked apart: exec_problems().
NOT_COMPARED = frozenset(
    {
        # Names the unit, its files or this activation; nothing the process runs with.
        "Id",
        "Names",
        "Following",
        "Description",
        "Documentation",
        "Transient",  # yes for the probe by construction
        "Perpetual",
        "FragmentPath",  # reported; the settings they produce are what is compared
        "SourcePath",
        "DropInPaths",
        "UnitFileState",
        "UnitFilePreset",
        "Markers",
        "InvocationID",
        # Load and run state of this activation (checked apart for the worker:
        # LoadState, ActiveState, NeedDaemonReload, MainPID's /proc).
        "LoadState",
        "LoadError",
        "NeedDaemonReload",
        "ActiveState",
        "SubState",
        "FreezerState",
        "Result",
        "ReloadResult",
        "CleanResult",
        "LiveMountResult",
        "MainPID",
        "ControlPID",
        "UID",  # the running process's IDs; User=/Group= and DynamicUser= are compared
        "GID",
        "ControlGroup",  # this unit's cgroup path; Slice= and Delegate* are compared
        "ControlGroupId",
        "NRestarts",
        "CPUUsageNSec",
        "MemoryAvailable",
        "NFileDescriptorStore",
        "ActivationDetails",  # what triggered this activation (a timer, a path...)
        # Who may start or stop the unit and when the manager collects it.
        "RefuseManualStart",
        "RefuseManualStop",
        "AllowIsolate",
        "IgnoreOnIsolate",
        "StopWhenUnneeded",
        "CollectMode",
        "SurviveFinalKillSignal",
        "DebugInvocation",  # the manager's log level for this unit
        # Activation and ordering dependencies: when the unit starts or stops, never what
        # its processes see. (JoinsNamespaceOf= shares namespaces: compared.)
        "DefaultDependencies",
        "Requires",
        "Requisite",
        "Wants",
        "BindsTo",
        "PartOf",
        "Upholds",
        "RequiredBy",
        "RequisiteOf",
        "WantedBy",
        "BoundBy",
        "UpheldBy",
        "ConsistsOf",
        "Conflicts",
        "ConflictedBy",
        "Before",
        "After",
        "OnSuccess",
        "OnSuccessOf",
        "OnFailure",
        "OnFailureOf",
        "Triggers",
        "TriggeredBy",
        "PropagatesReloadTo",
        "ReloadPropagatedFrom",
        "PropagatesStopTo",
        "StopPropagatedFrom",
        "RequiresMountsFor",  # ordering after mount units only
        "WantsMountsFor",
        "References",
        "ReferencedBy",
        "OnSuccessJobMode",
        "OnFailureJobMode",
        "SliceOf",  # slice units only
        # The worker process's start, readiness and end (the probe is a oneshot by design).
        "Type",
        "ExitType",
        "NotifyAccess",
        "RemainAfterExit",
        "GuessMainPID",
        "PIDFile",
        "BusName",  # Type=dbus readiness
        "RuntimeMaxUSec",
        "RuntimeRandomizedExtraUSec",
        "OOMPolicy",  # what the manager does after an OOM kill
        "FinalKillSignal",
        "RebootArgument",
        "FailureActionExitStatus",
        "SuccessActionExitStatus",
    }
)
NOT_COMPARED_PREFIXES = (
    # Exec* lines carry this activation's PIDs and times; exec_problems() checks them.
    "ExecStart",
    "ExecStop",
    "ExecReload",
    "ExecCondition",
    "ExecMain",  # the main process's PID, code and times
    # Start, stop, restart and watchdog timing; job handling; rate limits; actions the
    # manager takes on success or failure; signals used to stop the unit.
    "Timeout",
    "Restart",
    "Watchdog",
    "Job",
    "StartLimit",
    "FailureAction",
    "SuccessAction",
    "Kill",
    "SendSIG",
    # Whether the unit starts at all (Condition*/Assert* and their results), not how.
    "Condition",
    "Assert",
    # Runtime status text, the fd store, the manager's Can* capabilities of the unit,
    # and limits computed for the running cgroup (the configured ones are compared).
    "Status",
    "FileDescriptorStore",
    "Can",
    "Effective",
)
# Accounting of this activation: times, current and peak use, traffic counters.
NOT_COMPARED_SUFFIXES = (
    "Timestamp",
    "TimestampMonotonic",
    "Current",
    "Peak",
    "Bytes",
    "Packets",
    "Operations",
)
# Command-line prefixes that lift the sandbox or the user switch for that command:
# "+" (privileged: no sandboxing, root), "!" (no-setuid: the user switch is skipped) and
# "!!" (ambient: capabilities kept). ":", "-", "@" and "|" change expansion, failure
# handling, argv[0] or the shell only.
PRIVILEGED_EXEC_FLAGS = frozenset({"privileged", "no-setuid", "ambient"})
SHELL = Path(__file__).resolve().parent / "agent-loopback-shell"


def compared(name: str) -> bool:
    return not (
        name in NOT_COMPARED
        or name.startswith(NOT_COMPARED_PREFIXES)
        or name.endswith(NOT_COMPARED_SUFFIXES)
    )


# The unit-file command settings; on the bus each is <name>Ex, a(sasasttttuii): path, argv,
# flags, times, PID, code, status.
COMMAND_KEYS = (
    "ExecCondition",
    "ExecStartPre",
    "ExecStart",
    "ExecStartPost",
    "ExecReload",
    "ExecReloadPost",
    "ExecStop",
    "ExecStopPost",
)
COMMAND_TYPE = "a(sasasttttuii)"


def release_commands(instance: str, unit_root: Path = UNIT_ROOT) -> dict[str, list[list[Any]]]:
    """[path, argv] of every command the release's unit files define, by bus property
    (ExecStartEx...); an empty assignment resets the list, as in systemd."""
    commands: dict[str, list[list[Any]]] = {}
    for path in worker_unit_files(instance, unit_root):
        section = ""
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("["):
                section = line
                continue
            key = line.split("=", 1)[0].strip()
            if section != "[Service]" or key not in COMMAND_KEYS or "=" not in line:
                continue
            value = _expand(line.split("=", 1)[1].strip(), instance, path)
            if not value:
                commands.pop(f"{key}Ex", None)
                continue
            argv = shlex.split(value)
            if argv[0][:1] in "+!:-@|":
                raise SystemExit(f"{path}: {key}= with a prefix is not supported here")
            commands.setdefault(f"{key}Ex", []).append([argv[0], argv])
    return commands


def exec_problems(worker: dict[str, Any], release: dict[str, list[list[Any]]]) -> dict[str, Any]:
    """Every command of the loaded worker, read as typed bus data (never text a command's
    arguments could forge): none may carry a flag that lifts the sandbox or the user
    switch, and each command list is exactly the release's (path and argv)."""
    loaded: dict[str, list[dict[str, Any]]] = {}
    problems: list[str] = []
    names = {name for name, variant in worker.items() if variant.get("type") == COMMAND_TYPE}
    for name in sorted(names | set(release)):
        entries = []
        for entry in (worker.get(name) or {}).get("data") or []:
            path, argv, flags = entry[0], list(entry[1]), list(entry[2])
            entries.append({"path": path, "argv": argv, "flags": flags})
            lifted = sorted(PRIVILEGED_EXEC_FLAGS.intersection(flags))
            if lifted:
                problems.append(f"{name} {path}: {', '.join(lifted)}")
        if [[entry["path"], entry["argv"]] for entry in entries] != release.get(name, []):
            problems.append(f"{name} differs from the release: {entries}"[:300])
        if entries:
            loaded[name] = entries
    if "ExecStartEx" not in release:
        problems.append("the release defines no ExecStart")
    return {"ok": not problems, "problems": problems, "commands": loaded}


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed busctl/systemctl/systemd-run arguments
        command, capture_output=True, text=True, timeout=2400, check=False
    )


BUS = ("busctl", "--json=short", "call", "org.freedesktop.systemd1")
INTERFACES = ("org.freedesktop.systemd1.Unit", "org.freedesktop.systemd1.Service")


def _bus_properties(run: Runner, unit: str) -> dict[str, Any]:
    """Every property of the unit's Unit and Service interfaces as typed bus variants
    ({"type", "data"}), so systemd's own values are compared, not a rendering of them.
    Empty when the unit is not loaded."""
    found = run(
        [*BUS, "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "GetUnit"]
        + ["s", unit]
    )
    if found.returncode:
        return {}
    path = json.loads(found.stdout)["data"][0]
    properties: dict[str, Any] = {}
    for interface in INTERFACES:
        reply = run([*BUS, path, "org.freedesktop.DBus.Properties", "GetAll", "s", interface])
        if reply.returncode:
            raise SystemExit(f"{unit}: GetAll {interface} failed: {reply.stderr[-200:]}")
        for name, variant in json.loads(reply.stdout)["data"][0].items():
            if name in properties and properties[name] != variant:
                raise SystemExit(f"{unit}: {name} differs between its interfaces")
            properties[name] = variant
    return properties


def _data(properties: dict[str, Any], name: str) -> Any:
    variant = properties.get(name)
    return variant.get("data") if isinstance(variant, dict) else None


def unit_loaded_matches(run: Runner, instance: str, properties: list[str]) -> dict[str, Any]:
    """The loaded worker unit against a probe unit made from this release's settings, both
    read in full from systemd's bus API, so systemd normalises both sides alike and a
    setting only the host has (an installed drop-in or override) differs too."""
    worker = _bus_properties(run, f"engineering-worker@{instance}.service")
    probe = f"engineering-loopback-probe-{instance}-{uuid4().hex[:8]}"
    started = run(
        [
            "systemd-run",
            "--quiet",
            f"--unit={probe}",
            "--service-type=oneshot",
            "--property=RemainAfterExit=yes",
            *(f"--property={item}" for item in properties),
            "--",
            "/bin/true",
        ]
    )
    try:
        expected = _bus_properties(run, f"{probe}.service")
    finally:
        run(["systemctl", "stop", probe])
        run(["systemctl", "reset-failed", probe])
    names = sorted(name for name in {*worker, *expected} if compared(name))
    mismatches = {
        name: {"loaded": _data(worker, name), "release": _data(expected, name)}
        for name in names
        if worker.get(name) != expected.get(name)
    }
    problems = [f"{name} differs" for name in mismatches]
    commands = exec_problems(worker, release_commands(instance))
    problems += commands["problems"]
    if started.returncode:
        problems.append(f"probe unit failed: {started.stderr[-200:]}")
    if len(names) < 50:
        problems.append(f"only {len(names)} properties compared: is the unit loaded?")
    for name, wanted in (
        ("LoadState", "loaded"),
        ("ActiveState", "active"),
        ("NeedDaemonReload", False),
    ):
        if _data(worker, name) != wanted:
            problems.append(f"{name}={_data(worker, name)} (expected {wanted})")
    main_pid = _data(worker, "MainPID")
    return {
        "ok": not problems,
        "problems": problems,
        "mismatches": mismatches,
        "compared": len(names),
        "commands": commands["commands"],
        **{name: _data(worker, name) for name in WORKER_STATE},
        "MainPID": str(main_pid) if main_pid is not None else None,
    }


def _allowed_proc_submounts() -> tuple[str, ...]:
    for node in ast.parse(SHELL.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == (
            "PROC_SUBMOUNTS_ALLOWED"
        ):
            return tuple(ast.literal_eval(node.value))
    raise SystemExit(f"{SHELL}: PROC_SUBMOUNTS_ALLOWED not found")


def worker_proc_unlocked(main_pid: str | None, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """The running worker's own mount namespace: a mount over /proc there (a worker not
    restarted since its unit changed) is locked in bubblewrap and refuses its procfs."""
    if not main_pid or not main_pid.isdigit() or main_pid == "0":
        return {"ok": False, "main_pid": main_pid, "problems": ["the worker is not running"]}
    try:
        lines = (proc_root / main_pid / "mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return {"ok": False, "main_pid": main_pid, "problems": [f"mountinfo: {error}"]}
    allowed = _allowed_proc_submounts()
    covered = sorted(
        {
            fields[4]
            for fields in (line.split() for line in lines)
            if len(fields) > 4 and fields[4].startswith("/proc/") and fields[4] not in allowed
        }
    )
    return {
        "ok": not covered,
        "main_pid": main_pid,
        "overmounts": covered,
        "problems": [f"mounts over the worker's /proc: {covered}"] if covered else [],
    }


def _smoke_verdict(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The smoke's JSON verdict; PASS only with systemd-run (the smoke) exiting 0 too."""
    verdict: dict[str, Any] = {"status": "FAIL", "problems": ["no verdict"]}
    for line in reversed(completed.stdout.splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            verdict = parsed
            break
    if completed.returncode != 0 or verdict.get("status") != "PASS":
        verdict = {**verdict, "status": "FAIL", "exit_status": completed.returncode}
        verdict["problems"] = [
            *verdict.get("problems", []),
            f"systemd-run exit {completed.returncode}: {completed.stderr[-300:]}",
        ]
    return verdict


# What a process runs with, from /proc/<pid>/status (review 8: every field reviewed).
# Compared: Umask (UMask=), Uid/Gid/Groups (User=, Group=, SupplementaryGroups=), TracerPid
# (a tracer can change the process), the capability sets, NoNewPrivs, Seccomp and the filter
# count, the speculation controls the seccomp filters set, and NSpid's depth (PrivatePIDs=).
# Not compared, being this process's own runtime or program: Name, State, the PIDs
# themselves (Tgid, Ngid, Pid, PPid, NStgid, NSpgid, NSsid), Kthread, FDSize, memory use
# (Vm*, Rss*, HugetlbPages, THP_enabled, CoreDumping), untag_mask, Threads, signals (SigQ,
# SigPnd, ShdPnd, SigBlk, SigIgn, SigCgt: the program's own handlers), CPU and memory
# placement (Cpus_allowed*, Mems_allowed*: scheduling, not isolation), context switches,
# and the CPU's per-program x86_Thread_features.
PROCESS_FIELDS = (
    "Umask",
    "Uid",
    "Gid",
    "Groups",
    "TracerPid",
    "CapInh",
    "CapPrm",
    "CapEff",
    "CapBnd",
    "CapAmb",
    "NoNewPrivs",
    "Seccomp",
    "Seccomp_filters",
    "Speculation_Store_Bypass",
    "SpeculationIndirectBranch",
)
# Namespaces whose sharing with the host (PID 1's) is compared: a JoinsNamespaceOf=,
# PrivateNetwork=, PrivateIPC=, PrivateUsers=, PrivatePIDs= or ProtectHostname= the
# invocation started with shows here.
NAMESPACES = ("cgroup", "ipc", "mnt", "net", "pid", "time", "user", "uts")
NAMESPACES_SEPARATOR = "--- hermes-loopback-namespaces ---"
NO_CAPABILITIES = "0000000000000000"
# Paths whose visible mount (point, type, read-only) the sandbox decides; the unit's
# ReadWritePaths= and ReadOnlyPaths= are added.
MOUNT_PATHS = (
    "/",
    "/usr",
    "/etc",
    "/opt",
    "/var",
    "/srv",
    "/home",
    "/root",
    "/run",
    "/run/user",
    "/tmp",  # noqa: S108 - a mount checked, not a file made
    "/var/tmp",  # noqa: S108
    "/proc",
    "/proc/sys",
    "/sys",
    "/sys/fs/cgroup",
    "/dev",
)
FACTS_SEPARATOR = "--- hermes-loopback-mountinfo ---"


def process_facts(status: str, mountinfo: str, paths: list[str], unit: str) -> dict[str, Any]:
    """Credentials, capabilities, no_new_privs and seccomp of a process, and the mount
    visible at each path (the longest mount point above it, the last one mounted)."""
    fields = {}
    for line in status.splitlines():
        name, _, value = line.partition(":")
        if name in PROCESS_FIELDS:
            fields[name] = " ".join(value.split())
        elif name == "NSpid":
            fields["NSpid_depth"] = str(len(value.split()))
    mounts = []
    for line in mountinfo.splitlines():
        parts = line.split()
        if " - " in line and len(parts) > 5:
            fstype = line.split(" - ", 1)[1].split()[0]
            mounts.append((parts[4], "ro" in parts[5].split(","), fstype))
    visible: dict[str, dict[str, Any] | None] = {}
    for path in paths:
        best: tuple[str, bool, str] | None = None
        for point, read_only, fstype in mounts:
            if (path == point or path.startswith(point.rstrip("/") + "/")) and (
                best is None or len(point) >= len(best[0])
            ):
                best = (point, read_only, fstype)
        visible[path] = (
            None if best is None else {"point": best[0], "read_only": best[1], "fstype": best[2]}
        )
    table, foreign = mount_table(mountinfo, unit)
    return {"status": fields, "mounts": visible, "table": table, "foreign": foreign}


# PrivateTmp='s directories: /tmp/systemd-private-<boot id>-<unit>-<random>/tmp. The unit
# must be the process's own (review 7: another unit's means its /tmp is shared, as with a
# JoinsNamespaceOf= the unit no longer shows); only then is the name generalised, the
# random suffix being per instance by construction.
_PRIVATE_TMP = re.compile(r"systemd-private-(?P<boot>[0-9a-f]{32})-(?P<unit>[^/]+)-[A-Za-z0-9]+")
_PEER_GROUP = re.compile(r"^(shared|master|propagate_from):\d+$")


def mount_table(mountinfo: str, unit: str) -> tuple[list[str], list[str]]:
    """Every mount of a process's mount namespace, normalised to what the sandbox decides:
    the filesystem root bound (PrivateTmp's per-unit directory name generalised), the mount
    point, the per-mount options (ro/rw, nosuid, nodev, noexec, atime), the propagation
    kinds and the filesystem type. Not kept, being per instance: the mount and parent IDs
    and device numbers (numbering of this namespace and boot), the peer group numbers
    (numbering of this namespace's propagation groups), and the source and superblock
    options (one superblock is shared by every namespace that mounts it: its device name,
    tmpfs size and inode limits are the host's, not this unit's). A sorted list: equal
    tables have the same mounts, each as many times (stacked mounts count). Also returns
    the PrivateTmp roots owned by another unit than ``unit``."""
    table, foreign = [], []
    for line in mountinfo.splitlines():
        if " - " not in line:
            continue
        before, after = line.split(" - ", 1)
        parts = before.split()
        if len(parts) < 6 or not after.split():
            raise SystemExit(f"unparsed mountinfo line: {line[:120]}")
        root, point, options = parts[3], parts[4], parts[5]
        for match in _PRIVATE_TMP.finditer(root):
            if match["unit"] != unit:
                foreign.append(f"{point}: {root}")
        propagation = sorted(_PEER_GROUP.sub(r"\1", tag) for tag in parts[6:])
        table.append(
            " ".join(
                [
                    _PRIVATE_TMP.sub(r"systemd-private-\g<boot>-<this unit>-*", root),
                    point,
                    ",".join(sorted(options.split(","))),
                    "[" + ",".join(propagation) + "]",
                    after.split()[0],
                ]
            )
        )
    return sorted(table), foreign


def _namespaces(proc_root: Path, pid: str) -> dict[str, str]:
    links = {}
    for name in NAMESPACES:
        try:
            links[name] = os.readlink(proc_root / pid / "ns" / name)
        except OSError:
            links[name] = ""
    return links


def _sharing(links: dict[str, str], host: dict[str, str]) -> dict[str, str]:
    """Each namespace as "host" (PID 1's), "own" or "unknown"."""
    return {
        name: "unknown"
        if not links.get(name) or not host.get(name)
        else "host"
        if links[name] == host[name]
        else "own"
        for name in NAMESPACES
    }


def _mount_paths(properties: list[str]) -> list[str]:
    paths = list(MOUNT_PATHS)
    for item in properties:
        key, _, value = item.partition("=")
        if key in ("ReadWritePaths", "ReadOnlyPaths"):
            paths += [path.lstrip("-+") for path in value.split() if path.lstrip("-+")]
    return sorted(set(paths))


def worker_process_matches(
    run: Runner,
    instance: str,
    properties: list[str],
    main_pid: str | None,
    proc_root: Path = Path("/proc"),
    lookup: Callable[[str], Any] = pwd.getpwnam,
) -> dict[str, Any]:
    """The running worker process against a probe process started with this release's
    settings: what the running invocation really runs with, whatever the unit says now."""
    paths = _mount_paths(properties)
    probe_unit = f"engineering-loopback-facts-{instance}-{uuid4().hex[:8]}"
    probe = run(
        [
            "systemd-run",
            "--quiet",
            "--pipe",
            "--wait",
            "--collect",
            "--service-type=exec",
            f"--unit={probe_unit}",
            *(f"--property={item}" for item in properties),
            "--",
            "/bin/sh",
            "-c",
            f"cat /proc/self/status; echo '{FACTS_SEPARATOR}'; cat /proc/self/mountinfo; "
            f"echo '{NAMESPACES_SEPARATOR}'; for n in {' '.join(NAMESPACES)}; do "
            'echo "$n $(readlink /proc/self/ns/$n)"; done',
        ]
    )
    problems: list[str] = []
    host = _namespaces(proc_root, "1")
    if (
        probe.returncode
        or FACTS_SEPARATOR not in probe.stdout
        or (NAMESPACES_SEPARATOR not in probe.stdout)
    ):
        problems.append(f"probe process failed: exit {probe.returncode} {probe.stderr[-200:]}")
        expected: dict[str, Any] = {
            "status": {},
            "mounts": {},
            "table": [],
            "foreign": [],
            "namespaces": {},
        }
    else:
        status, rest = probe.stdout.split(FACTS_SEPARATOR, 1)
        mountinfo, links = rest.split(NAMESPACES_SEPARATOR, 1)
        expected = process_facts(status, mountinfo, paths, f"{probe_unit}.service")
        probe_links = dict(line.split(" ", 1) for line in links.strip().splitlines() if " " in line)
        expected["namespaces"] = _sharing(probe_links, host)
    if not main_pid or not main_pid.isdigit() or main_pid == "0":
        return {"ok": False, "problems": [*problems, "the worker is not running"]}
    try:
        status = (proc_root / main_pid / "status").read_text(encoding="utf-8")
        mountinfo = (proc_root / main_pid / "mountinfo").read_text(encoding="utf-8")
    except OSError as error:
        return {"ok": False, "problems": [*problems, f"worker /proc: {error}"]}
    actual = process_facts(status, mountinfo, paths, f"engineering-worker@{instance}.service")
    actual["namespaces"] = _sharing(_namespaces(proc_root, main_pid), host)
    problems += [f"another unit's PrivateTmp: {item}" for item in actual["foreign"]]
    problems += [f"probe: another unit's PrivateTmp: {item}" for item in expected["foreign"]]
    differences = {
        f"{kind}:{name}": {"worker": actual[kind].get(name), "release": expected[kind].get(name)}
        for kind in ("status", "mounts", "namespaces")
        for name in sorted({*actual[kind], *expected[kind]})
        if actual[kind].get(name) != expected[kind].get(name)
    }
    # The whole mount table (review 6): a mount the worker has and a process started with
    # the release's settings has not (or the other way round) refuses the PASS.
    only_worker = list((Counter(actual["table"]) - Counter(expected.get("table", []))).elements())
    only_release = list((Counter(expected.get("table", [])) - Counter(actual["table"])).elements())
    if only_worker or only_release:
        differences["mounts:table"] = {"worker": only_worker, "release": only_release}
    problems += [f"{name} differs" for name in differences]
    user = next(
        (item.split("=", 1)[1] for item in reversed(properties) if item.startswith("User=")), None
    )
    try:
        account = lookup(user) if user else None
    except KeyError:
        account = None
    if account is None:
        problems.append(f"no account for User={user}")
    else:
        for name, wanted in (("Uid", account.pw_uid), ("Gid", account.pw_gid)):
            if actual["status"].get(name, "").split() != [str(wanted)] * 4:
                problems.append(f"{name} {actual['status'].get(name)} (expected {wanted} x4)")
    for name in ("CapPrm", "CapEff", "CapAmb"):
        if actual["status"].get(name) != NO_CAPABILITIES:
            problems.append(f"{name}={actual['status'].get(name)} (expected none)")
    if actual["status"].get("NoNewPrivs") != "1":
        problems.append("no_new_privs is not set")
    for path, read_only in (("/usr", True), ("/etc", True), ("/sys", True)):
        mount = actual["mounts"].get(path) or {}
        if mount.get("read_only") is not read_only:
            problems.append(f"{path} is not read-only")
    for path in ("/tmp", "/var/tmp", "/home"):  # noqa: S108 - mounts checked
        if (actual["mounts"].get(path) or {}).get("point") != path:
            problems.append(f"{path} is not a mount of its own (PrivateTmp/ProtectHome)")
    return {
        "ok": not problems,
        "problems": problems,
        "differences": differences,
        "main_pid": main_pid,
        "status": actual["status"],
    }


def worker_started_after_last_reload(run: Runner, worker: dict[str, Any]) -> dict[str, Any]:
    """The running invocation started after systemd last loaded the unit files (review 7):
    a setting changed and reloaded without a restart (a JoinsNamespaceOf=, a privileged
    command, a mount, a drop-in removed) leaves the running process as it started, so a
    check of the loaded settings alone would vouch for an invocation they did not start.
    Fails closed without the manager's UnitsLoadTimestampMonotonic (review 8: file times
    cannot show a drop-in that was deleted); the worker host's systemd 259 has it."""
    started = worker.get("ExecMainStartTimestampMonotonic")
    reply = run(
        [*BUS, "/org/freedesktop/systemd1", "org.freedesktop.DBus.Properties", "Get", "ss"]
        + ["org.freedesktop.systemd1.Manager", "UnitsLoadTimestampMonotonic"]
    )
    loaded: Any = None
    if reply.returncode == 0:
        with suppress(ValueError, KeyError, IndexError, TypeError):
            loaded = json.loads(reply.stdout)["data"][0]["data"]
    problems = []
    if not isinstance(started, int) or started <= 0:
        problems.append("the worker's start time is unknown")
    if not isinstance(loaded, int) or loaded <= 0:
        problems.append("systemd's last unit load time is unavailable (UnitsLoadTimestamp)")
    elif isinstance(started, int) and started <= loaded:
        problems.append("the worker was not restarted after the last daemon-reload")
    return {
        "ok": not problems,
        "problems": problems,
        "started_monotonic_usec": started,
        "units_loaded_monotonic_usec": loaded,
    }


def worker_unit_check(
    instance: str,
    smoke: list[str],
    *,
    expectation_only: bool,
    run: Runner = _run,
    proc_root: Path = Path("/proc"),
    lookup: Callable[[str], Any] = pwd.getpwnam,
) -> dict[str, Any]:
    properties = worker_unit_properties(instance)
    checks: dict[str, Any] = {}
    if not expectation_only:
        checks["unit_loaded_matches"] = unit_loaded_matches(run, instance, properties)
        main_pid = checks["unit_loaded_matches"].get("MainPID")
        checks["worker_proc_unlocked"] = worker_proc_unlocked(main_pid, proc_root)
        checks["worker_process_matches"] = worker_process_matches(
            run, instance, properties, main_pid, proc_root, lookup
        )
        checks["worker_started_after_last_reload"] = worker_started_after_last_reload(
            run, checks["unit_loaded_matches"]
        )
    checks["sandbox"] = _smoke_verdict(run(worker_unit_command(instance, smoke)))
    passed = all(check.get("ok", check.get("status") == "PASS") for check in checks.values())
    if expectation_only:
        status = "EXPECTATION_PASS" if passed else "FAIL"
        note = "this release's settings in a transient unit only: not production acceptance"
    else:
        status = "PASS" if passed else "FAIL"
        note = "loaded unit, running worker process and the sandbox checked"
    return {
        "mode": "worker-unit",
        "instance": instance,
        "status": status,
        "note": note,
        "checks": checks,
    }


def worker_unit_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent-loopback-smoke.py worker-unit")
    parser.add_argument("--instance", default="claude")
    parser.add_argument(
        "--expectation-only",
        action="store_true",
        help="before installing this release's units: sandbox checks only, never PASS",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the smoke command only")
    parser.add_argument("smoke", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    smoke = arguments.smoke[1:] if arguments.smoke[:1] == ["--"] else arguments.smoke
    if arguments.dry_run:
        print(shlex.join(worker_unit_command(arguments.instance, smoke)))
        return 0
    if os.geteuid() != 0:
        raise SystemExit("worker-unit starts system units: run it with sudo (or --dry-run)")
    verdict = worker_unit_check(
        arguments.instance, smoke, expectation_only=arguments.expectation_only
    )
    print(json.dumps(verdict, sort_keys=True))
    return 0 if verdict["status"] in ("PASS", "EXPECTATION_PASS") else 1


def main() -> int:
    if sys.argv[1:2] == ["worker-unit"]:
        return worker_unit_main(sys.argv[2:])
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("mode", choices=("shell", "claude"))
    parser.add_argument(
        "--work-root", type=Path, required=True, help="an existing directory this user may write"
    )
    parser.add_argument("--shell", type=Path, default=Settings().agent_loopback_shell)
    parser.add_argument("--executable", help="the Claude CLI (default: the worker's)")
    parser.add_argument("--chromium", help="also load the loopback page with this Chromium")
    parser.add_argument(
        "--allow-unowned-shell",
        action="store_true",
        help="development hosts only: accept a loopback shell this user could modify",
    )
    arguments = parser.parse_args()
    work = Path(
        tempfile.mkdtemp(prefix=f"agent-loopback-smoke-{uuid4().hex[:8]}-", dir=arguments.work_root)
    )
    try:
        if arguments.mode == "shell":
            verdict = run_shell(work, arguments.shell, arguments.chromium)
        else:
            verdict = asyncio.run(
                run_claude(
                    work,
                    arguments.executable or "/usr/local/bin/claude",
                    arguments.shell,
                    arguments.chromium,
                    allow_unowned_shell=arguments.allow_unowned_shell,
                )
            )
    finally:
        remove_tree(work)
    print(json.dumps({"mode": arguments.mode, **verdict}, sort_keys=True))
    return 0 if verdict.get("status") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
