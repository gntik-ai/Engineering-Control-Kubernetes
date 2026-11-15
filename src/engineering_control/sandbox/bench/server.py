"""The workbench: a sandbox that stays for a run and carries out one small job after
another.

This file runs inside the sandbox, with nothing but Python's own library. A step in a
sandbox of its own takes seconds to start; the tasks that look at code or change it are
many and small, so they share one sandbox that is already there. The launcher hands it a
job, the same kind of job a sandbox of its own is started with, and reads back what the
job printed. Each job is carried out by the image's own runner (``sandbox-step``), so a
job means here exactly what it means in a sandbox of its own.

What keeps a kept sandbox honest:

- It has no network but this one door, and the cluster's rules let only the launcher
  through it. The door asks for a token all the same.
- One job runs at a time. A job is known by a key; asked for again under the same key,
  the workbench answers with what it already did instead of doing it twice.
- When a job ends, every process it left behind is ended with it: nothing a job started
  is still there when the next one runs.
"""

from __future__ import annotations

import hmac
import json
import os
import signal
import subprocess
import sys
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("BENCH_PORT", "8791"))
TOKEN = os.environ.get("BENCH_TOKEN", "")
# The image's runner; a test may name another way to start it.
RUNNER = os.environ.get("BENCH_RUNNER", "sandbox-step").split()
# Whether what a job left running is ended after it. On in a sandbox, where every other
# process is the job's; off wherever other processes share the machine.
SWEEP = os.environ.get("BENCH_SWEEP", "") == "1"
KEPT_JOBS = 40
KEPT_OUTPUT = 6 * 1024 * 1024
MAX_BODY = 8 * 1024 * 1024
# Where a job may have files written before it runs.
WRITABLE = tuple(
    os.environ.get("BENCH_WRITABLE", "/tmp/:/workspace/.run/:/workspace/.cache/").split(":")  # noqa: S108
)


class Job:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.log = ""


JOBS: OrderedDict[str, Job] = OrderedDict()
LOCK = threading.Lock()
BUSY = threading.Lock()


def _ancestors() -> set[int]:
    """This process and the ones it descends from: what a sweep leaves alone."""
    kept, pid = set(), os.getpid()
    while pid > 0 and pid not in kept:
        kept.add(pid)
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
                # The name may hold spaces and brackets; what follows its last one is plain.
                pid = int(handle.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return kept


def sweep() -> int:
    """End every process that is not the workbench itself. Returns how many there were."""
    kept, ended = _ancestors(), 0
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) in kept:
            continue
        try:
            os.kill(int(name), signal.SIGKILL)
            ended += 1
        except OSError:
            continue
    return ended


def _write(files: Any) -> None:
    for entry in files if isinstance(files, list) else []:
        path = os.path.normpath(str(entry["path"]))
        if not path.startswith(WRITABLE):
            raise ValueError(f"a job may not write {path}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(entry["content"]))


def carry_out(job: Job, description: dict[str, Any]) -> None:
    try:
        # Files are written here and not by the runner: a job reaches the runner through
        # the environment, which holds less than a file may.
        _write(description.get("files"))
        rest = {key: value for key, value in description.items() if key != "files"}
        environment = {key: value for key, value in os.environ.items() if key != "BENCH_TOKEN"}
        environment["SANDBOX_JOB"] = json.dumps(rest, separators=(",", ":"))
        ran = subprocess.run(  # noqa: S603 - the image's own runner
            RUNNER,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        job.log = ran.stdout[-KEPT_OUTPUT:].decode("utf-8", errors="replace")
    except Exception as error:  # noqa: BLE001 - whatever went wrong is the job's answer
        job.log = f"workbench: the job could not be carried out: {error}\n"
    finally:
        if SWEEP:
            sweep()
        job.done.set()
        BUSY.release()


class Door(BaseHTTPRequestHandler):
    server_version = "workbench"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base's name
        return

    def _answer(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _allowed(self) -> bool:
        said = self.headers.get("Authorization", "")
        if TOKEN and hmac.compare_digest(said.encode(), f"Bearer {TOKEN}".encode()):
            return True
        self._answer(401, {"error": "not allowed"})
        return False

    def do_GET(self) -> None:  # noqa: N802 - the base's name
        url = urlparse(self.path)
        if url.path == "/health":
            self._answer(200, {"ok": True})
            return
        if not self._allowed():
            return
        if not url.path.startswith("/job/"):
            self._answer(404, {"error": "no such thing"})
            return
        with LOCK:
            job = JOBS.get(url.path.removeprefix("/job/"))
        if job is None:
            self._answer(404, {"error": "no such job"})
            return
        try:
            wait = min(30.0, float(parse_qs(url.query).get("wait", ["0"])[0]))
        except ValueError:
            wait = 0.0
        if job.done.wait(timeout=max(0.0, wait)):
            self._answer(200, {"done": True, "log": job.log})
        else:
            self._answer(200, {"done": False})

    def do_POST(self) -> None:  # noqa: N802 - the base's name
        if not self._allowed():
            return
        if urlparse(self.path).path != "/job":
            self._answer(404, {"error": "no such thing"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if not 0 < length <= MAX_BODY:
                raise ValueError("size")
            asked = json.loads(self.rfile.read(length))
            key, description = str(asked["key"]), asked["job"]
            if not key or not isinstance(description.get("commands"), list):
                raise ValueError("job")
        except (ValueError, KeyError, TypeError, AttributeError):
            self._answer(400, {"error": "not a job"})
            return
        with LOCK:
            if key in JOBS:
                # Asked for again: what was done stands.
                self._answer(200, {"key": key, "known": True})
                return
            if not BUSY.acquire(blocking=False):
                self._answer(409, {"error": "another job is running"})
                return
            job = JOBS[key] = Job()
            while len(JOBS) > KEPT_JOBS:
                JOBS.popitem(last=False)
        threading.Thread(target=carry_out, args=(job, description), daemon=True).start()
        self._answer(202, {"key": key, "known": False})


def main() -> None:
    if not TOKEN:
        print("workbench: no token to open the door with", file=sys.stderr, flush=True)
        raise SystemExit(2)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Door)  # noqa: S104 - the pod's own address
    server.daemon_threads = True
    print(f"workbench: listening on {PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
