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
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
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


def main() -> int:
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
