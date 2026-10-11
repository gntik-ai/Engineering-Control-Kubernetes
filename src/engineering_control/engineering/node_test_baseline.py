"""NEW-161: pre-existing failures of a maker-chosen ``node --test`` suite.

NEW-49 only reruns a project's toolchain contract test commands at the base commit. A
project without a contract (falcone) therefore blocked forever on a maker FAIL in any
suite that already fails on main (#978: tests/integration/098-plan-base-limits fails 9
of 16 on main with "Unhandled fake SQL").

This module is pure: a strict parser for one allowlisted invocation shape, a bounded
non-following glob expansion against a checkout, the reporter the scan worker controls,
the parser of its output and the bubblewrap argument vector the scan worker runs it in.
Review 1: only the BASE (trusted main code) is ever executed; maker-authored head code
never runs in the scan worker. Anything not accepted returns None (MAKER_TEST_FAILURE).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# Bounds (the scan worker also bounds the number of commands and the run time).
MAX_PATH_ARGUMENTS = 32
MAX_TEST_FILES = 400
MAX_GLOB_VISITS = 20_000
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_FAILING_TESTS = 5000
EVIDENCE_TEST_NAMES = 50
EVIDENCE_NAME_CHARS = 200
# The sandbox's only writable mount: a size-capped tmpfs (no host filesystem is writable).
SANDBOX_TMPFS_BYTES = 256 * 1024 * 1024

# Inside the sandbox the base checkout is read-only at /work, so reported file paths
# do not depend on where the scan worker keeps its checkout.
SANDBOX_WORKTREE = "/work"
SANDBOX_REPORTER = "/run/hermes-node-test/reporter.mjs"
SANDBOX_TMP = "/tmp"  # noqa: S108 - a private size-capped tmpfs inside the sandbox
NONCE_VARIABLE = "HERMES_NODE_TEST_NONCE"

REPORTER_VERSION = 4
# Buffers one JSON line per failed test and prints them, between nonce markers, to
# stdout when the run ends (so the block survives a bounded output tail). It keeps what
# separates a test failure from a file-level one (import, syntax, process exit, hooks).
REPORTER_SOURCE = """\
// Hermes NEW-161 node:test reporter v4 (scan worker, base-only pre-existing baseline).
// Review 2: error codes along the cause chain let sandbox-induced failures be excluded.
const ERRNO = /\\b(E[A-Z]{2,15})\\b/g;
function errorCodes(error) {
  const codes = new Set();
  let current = error;
  for (let depth = 0; current && depth < 6 && codes.size < 8; depth += 1) {
    if (typeof current.code === 'string') codes.add(current.code.slice(0, 40));
    const text = typeof current.message === 'string' ? current.message.slice(0, 2000) : '';
    if (/\\/proc(\\/|\\b)/.test(text)) codes.add('EHERMESPROC');
    for (const match of text.matchAll(ERRNO)) { if (codes.size < 8) codes.add(match[1]); }
    current = current.cause;
  }
  return [...codes];
}
export default async function* hermesReporter(source) {
  const nonce = process.env.HERMES_NODE_TEST_NONCE ?? '';
  const lines = [];
  let passed = 0;
  let truncated = false;
  for await (const event of source) {
    if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
    const data = event.data ?? {};
    if (data.skip || data.todo) continue;
    if (event.type === 'test:pass') { passed += 1; continue; }
    if (lines.length >= 5000) { truncated = true; continue; }
    const error = data.details?.error;
    lines.push(JSON.stringify({
      name: String(data.name ?? '').slice(0, 500),
      nesting: Number.isInteger(data.nesting) ? data.nesting : 0,
      file: typeof data.file === 'string' ? data.file.slice(0, 1000) : null,
      type: data.details?.type === 'suite' ? 'suite' : 'test',
      failure: typeof error?.failureType === 'string' ? error.failureType : null,
      error_cause: error?.cause instanceof Error ? 'error' : typeof error?.cause,
      error_codes: errorCodes(error),
    }));
  }
  yield `${nonce} BEGIN ${JSON.stringify({ hermes: 4, passed, truncated })}\\n`;
  for (const line of lines) yield `${nonce} FAIL ${line}\\n`;
  yield `${nonce} END ${lines.length}\\n`;
}
"""
# node:test failure types that a test body (not its file, process or hooks) produced.
_TEST_LEVEL_FAILURES = frozenset({"testCodeFailure", "testTimeoutFailure"})
# Review 2: errors the sandbox itself causes (read-only checkout, capped /tmp, no network,
# dropped privileges). A base failure carrying one is never evidence of a real failure.
SANDBOX_ERROR_CODES = frozenset(
    {
        # NEW-186: the sandbox has no /proc; the v4 reporter tags errors naming it, and
        # node's native APIs that read it fail with ENOENT without naming it
        # (process.memoryUsage(): "ENOENT ... uv_resident_set_memory"). Fail closed: an
        # ENOENT base failure is never pre-existing evidence.
        "EHERMESPROC",
        "ENOENT",
        "EROFS",
        "ENOSPC",
        "EDQUOT",
        "EACCES",
        "EPERM",
        "EMFILE",
        "ENFILE",
        "ENETUNREACH",
        "EAI_AGAIN",
        "ENOTFOUND",
        "ECONNREFUSED",
        "EHOSTUNREACH",
    }
)

# Characters that never appear in an accepted command, quoted or not: we never use a
# shell, but a command that needs one is not the plain invocation this exemption covers.
_FORBIDDEN_CHARACTERS = frozenset(";|&<>$`\\\n\r\x00{}!~#")
_CD_PREFIX = re.compile(r"^cd\s+(?P<cwd>\S+)\s+&&\s+(?P<command>.+)$", re.DOTALL)
_SEGMENT = r"[A-Za-z0-9_@+][A-Za-z0-9._@+-]*"
_GLOB_SEGMENT = r"[A-Za-z0-9_@+*?][A-Za-z0-9._@+*?-]*"
_CWD = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT})*")
_PATH = re.compile(rf"{_GLOB_SEGMENT}(?:/{_GLOB_SEGMENT})*")
_FILE = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT})*")
_PREFIX = re.compile(rf"(?:{_SEGMENT}/)+")
_DURATION = re.compile(r"[0-9]{1,5}(?:\.[0-9]{1,3})?[smhd]?")
_KILL_AFTER = re.compile(r"--kill-after=[0-9]{1,5}[smhd]?")
# The only node:test flags kept for the rerun (the scan worker adds its own reporter).
_KEPT_FLAGS = (
    re.compile(r"--(?:experimental-)?test-isolation=(?:none|process)"),
    re.compile(r"--test-concurrency=[1-9][0-9]?"),
    re.compile(r"--test-name-pattern=[A-Za-z0-9 _.:/,+*?()\[\]^-]{1,200}"),
    re.compile(r"--test-timeout=[1-9][0-9]{0,7}"),
)
# Output-format flags the maker may have used; they are dropped, never forwarded.
_DROPPED_FLAGS = (
    re.compile(r"--test-reporter=(?:spec|tap|dot)"),
    re.compile(r"--test-reporter-destination=stdout"),
)


def valid_path_prefix(prefix: str) -> bool:
    """A configured test path prefix: plain relative directory segments ending in '/'."""
    return bool(_PREFIX.fullmatch(prefix)) and not _has_dot_segment(prefix)


def _has_dot_segment(path: str) -> bool:
    return any(segment in {"", ".", ".."} for segment in path.rstrip("/").split("/"))


@dataclass(frozen=True)
class NodeTestCommand:
    """A strictly parsed ``node --test`` invocation (cwd relative to the repository)."""

    cwd: str
    flags: tuple[str, ...]
    paths: tuple[str, ...]

    @property
    def argv(self) -> list[str]:
        """The normalized command (maker reporter flags and timeout prefix removed)."""
        return ["node", "--test", *self.flags, *self.paths]

    @property
    def display(self) -> str:
        return f"cd {self.cwd} && {shlex.join(self.argv)}"


def _relative_cwd(value: str, worktree: Path | None) -> str | None:
    if value in {".", "./"}:
        return "."
    if value.startswith("/"):
        if worktree is None or _FORBIDDEN_CHARACTERS.intersection(value):
            return None
        path = Path(value)
        if path == worktree:
            return "."
        if not path.is_relative_to(worktree):
            return None
        value = path.relative_to(worktree).as_posix()
    value = value.removeprefix("./").rstrip("/")
    if not _CWD.fullmatch(value) or _has_dot_segment(value):
        return None
    return value


def parse_node_test_command(
    reported: str, *, prefixes: tuple[str, ...], worktree: Path | None = None
) -> NodeTestCommand | None:
    """Accept only ``[cd <dir> &&] [timeout [--kill-after=D] D] node --test [flags] paths``.

    Every flag must be on the allowlist, every path relative, below a configured prefix
    (relative to the cwd), without '.'/'..' segments, and only '*'/'?' as glob syntax.
    """
    text = reported.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    cwd = "."
    prefix = _CD_PREFIX.match(text)
    if prefix is not None:
        relative = _relative_cwd(prefix["cwd"], worktree)
        if relative is None:
            return None
        cwd = relative
        text = prefix["command"]
    if not text or _FORBIDDEN_CHARACTERS.intersection(text):
        return None
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    if tokens[:1] == ["timeout"]:
        tokens = tokens[1:]
        if tokens and _KILL_AFTER.fullmatch(tokens[0]):
            tokens = tokens[1:]
        if not tokens or not _DURATION.fullmatch(tokens[0]):
            return None
        tokens = tokens[1:]
    if tokens[:2] != ["node", "--test"]:
        return None
    flags: list[str] = []
    paths: list[str] = []
    for token in tokens[2:]:
        if token.startswith("-"):
            if paths:
                return None  # flags first: nothing after a path is reinterpreted
            if any(pattern.fullmatch(token) for pattern in _DROPPED_FLAGS):
                continue
            if not any(pattern.fullmatch(token) for pattern in _KEPT_FLAGS):
                return None
            if token in flags:
                return None
            flags.append(token)
            continue
        candidate = token.removeprefix("./")
        if (
            not _PATH.fullmatch(candidate)
            or _has_dot_segment(candidate)
            or "**" in candidate
            or not any(candidate.startswith(item) for item in prefixes)
        ):
            return None
        if candidate not in paths:
            paths.append(candidate)
    if not paths or len(paths) > MAX_PATH_ARGUMENTS:
        return None
    return NodeTestCommand(cwd, tuple(flags), tuple(paths))


def _walk(
    directory: Path, segments: list[str], prefix: str, found: list[str], visits: list[int]
) -> bool:
    """Match one segment per level; never follows a symlink. False aborts the expansion."""
    if not segments:
        return True
    segment, rest = segments[0], segments[1:]
    try:
        with os.scandir(directory) as entries:
            listed = sorted(entries, key=lambda entry: entry.name)
    except OSError:
        return False
    for entry in listed:
        visits[0] += 1
        if visits[0] > MAX_GLOB_VISITS:
            return False
        if entry.name.startswith(".") or not fnmatch.fnmatchcase(entry.name, segment):
            continue
        if entry.is_symlink():
            return False  # a symlinked file or directory is never expanded
        relative = f"{prefix}{entry.name}"
        if rest:
            if entry.is_dir(follow_symlinks=False) and not _walk(
                Path(entry.path), rest, f"{relative}/", found, visits
            ):
                return False
        elif entry.is_file(follow_symlinks=False) and _FILE.fullmatch(relative):
            if relative not in found:
                found.append(relative)
                if len(found) > MAX_TEST_FILES:
                    return False
    return True


def expand_test_paths(checkout: Path, command: NodeTestCommand) -> list[str] | None:
    """Expand the paths in Python against one checkout (never a shell, never a symlink).

    A bounded, non-following walk: returns the matched files relative to the cwd, or None
    when a path matches nothing, meets a symlink, or a visit/file bound is exceeded.
    """
    base = checkout
    if command.cwd != ".":
        for part in command.cwd.split("/"):
            base = base / part
            if base.is_symlink() or not base.is_dir():
                return None
    if checkout.is_symlink() or not checkout.is_dir():
        return None
    files: list[str] = []
    visits = [0]
    for pattern in command.paths:
        matched: list[str] = []
        if not _walk(base, pattern.split("/"), "", matched, visits) or not matched:
            return None
        files.extend(item for item in matched if item not in files)
        if len(files) > MAX_TEST_FILES:
            return None
    return files


@dataclass(frozen=True)
class NodeTestReport:
    """Failures at one run: test-level (in a targeted file) vs everything else."""

    test_failures: Counter[str]
    other_failures: Counter[str]
    passed: int


def parse_reporter_output(
    text: str, nonce: str, files: list[str], cwd: str
) -> NodeTestReport | None:
    """The reporter block for this run's nonce -> classified failures; None when malformed.

    A test identity is ``<file relative to the checkout>#<nesting>: <name>``. A failure is
    test-level only if a test (not a suite) body failed (testCodeFailure or
    testTimeoutFailure) in one of the files the command targets; file-level failures
    (import, syntax, process exit: reported under the file's own path), hook failures,
    suites and failures outside the targeted files are "other".
    """
    lines = text.splitlines()
    begin = f"{nonce} BEGIN "
    starts = [index for index, line in enumerate(lines) if line.startswith(begin)]
    if len(starts) != 1 or not nonce:
        return None
    try:
        summary = json.loads(lines[starts[0]][len(begin) :])
    except ValueError:
        return None
    if (
        not isinstance(summary, dict)
        or summary.get("hermes") != REPORTER_VERSION
        or not isinstance(summary.get("passed"), int)
        or summary.get("truncated") is not False
    ):
        return None
    root = f"{SANDBOX_WORKTREE}/"
    targeted = {f"{root}{item}" if cwd == "." else f"{root}{cwd}/{item}" for item in files}
    file_names = set(files) | {path[len(root) :] for path in targeted} | targeted
    test_failures: Counter[str] = Counter()
    other_failures: Counter[str] = Counter()
    count = 0
    for line in lines[starts[0] + 1 :]:
        if line == f"{nonce} END {count}":
            return NodeTestReport(test_failures, other_failures, summary["passed"])
        if not line.startswith(f"{nonce} FAIL "):
            continue  # test output interleaved with the block (isolation=none)
        try:
            record = json.loads(line[len(nonce) + 6 :])
        except ValueError:
            return None
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("name"), str)
            or not isinstance(record.get("nesting"), int)
            or not (record.get("file") is None or isinstance(record.get("file"), str))
            or record.get("type") not in {"test", "suite"}
            or not (record.get("failure") is None or isinstance(record.get("failure"), str))
            or not isinstance(record.get("error_codes"), list)
            or not all(isinstance(code, str) for code in record["error_codes"])
        ):
            return None
        count += 1
        if count > MAX_FAILING_TESTS:
            return None
        file = record["file"]
        relative = file[len(root) :] if isinstance(file, str) and file.startswith(root) else ""
        identity = f"{relative}#{record['nesting']}: {record['name']}"
        if (
            file in targeted
            and record["type"] == "test"
            and record["failure"] in _TEST_LEVEL_FAILURES
            and not (record["nesting"] == 0 and record["name"] in file_names)
            and record.get("error_cause") != "string"
            and not SANDBOX_ERROR_CODES.intersection(record["error_codes"])
        ):
            test_failures[identity] += 1
        else:
            other_failures[identity] += 1
    return None  # no END marker: truncated or interrupted


def bounded_names(failing: Counter[str]) -> list[str]:
    names = sorted(failing.elements())[:EVIDENCE_TEST_NAMES]
    return [name[:EVIDENCE_NAME_CHARS] for name in names]


def _system_binds() -> list[str]:
    arguments = ["--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib64", "lib32"):
        path = Path("/") / name
        if path.is_symlink():
            arguments += ["--symlink", str(path.readlink()), str(path)]
        elif path.is_dir():
            arguments += ["--ro-bind", str(path), str(path)]
    for name in (
        "ld.so.cache",
        "ld.so.conf",
        "ld.so.conf.d",
        "localtime",
        "passwd",
        "group",
        "nsswitch.conf",
        "hosts",
    ):
        arguments += ["--ro-bind-try", f"/etc/{name}", f"/etc/{name}"]
    return arguments


def sandbox_argv(
    *,
    checkout: Path,
    command: NodeTestCommand,
    files: list[str],
    node: Path,
    reporter: Path,
    nonce: str,
    variables: dict[str, str],
    overlay: tuple[Path, Path] | None = None,
) -> list[str]:
    """bubblewrap arguments: no network, a read-only system and a size-capped /tmp.

    The report goes to stdout between nonce markers. ``node`` must be the resolved
    executable; its installation is bound read-only. Review 3: with ``overlay`` (upper,
    work), the trusted BASE checkout is a writable copy-on-write view whose writes land
    in that per-run upper directory (removed by the caller), so base code that writes
    to its checkout behaves as on a real machine instead of failing on a read-only one
    in a way it may itself swallow. Without it the checkout is bound read-only.
    """
    for path in (checkout, node, reporter, *(overlay or ())):
        if not path.is_absolute() or "\n" in str(path):
            raise ValueError(f"node test sandbox path {path} is not a plain absolute path")
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise ValueError("node test report nonce must be 32 hex characters")
    installation = node.parent.parent
    binds: list[str] = []
    if not node.is_relative_to("/usr"):
        binds = ["--ro-bind", str(installation), str(installation)]
    environment: dict[str, str] = {
        "PATH": f"{node.parent}:/usr/bin:/bin",
        "HOME": SANDBOX_TMP,
        "TMPDIR": SANDBOX_TMP,
        "CI": "true",
        "NO_COLOR": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        **variables,
        NONCE_VARIABLE: nonce,
    }
    setenv: list[str] = []
    for key, value in sorted(environment.items()):
        setenv += ["--setenv", key, value]
    cwd = SANDBOX_WORKTREE if command.cwd == "." else f"{SANDBOX_WORKTREE}/{command.cwd}"
    return [
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        *_system_binds(),
        *binds,
        # NEW-186: no /proc. The scan worker unit's ProtectKernelTunables leaves
        # read-only mounts over /proc subtrees, so the kernel refuses a fresh procfs in
        # bwrap's unprivileged namespace ("Can't mount proc on /newroot/proc"), and
        # binding the worker's /proc would expose other processes' command lines to the
        # base code. node runs without it; failures naming /proc (EHERMESPROC) or
        # carrying ENOENT are sandbox-caused, never pre-existing evidence. Residual: APIs
        # that degrade silently without /proc (os.cpus() returns []) can still fail a
        # base test; the checker and the pull request's CI still gate the merge.
        "--dev",
        "/dev",
        "--size",
        str(SANDBOX_TMPFS_BYTES),
        "--tmpfs",
        SANDBOX_TMP,
        "--ro-bind",
        str(reporter),
        SANDBOX_REPORTER,
        *(
            ["--overlay-src", str(checkout), "--overlay", str(overlay[0]), str(overlay[1])]
            if overlay is not None
            else ["--ro-bind", str(checkout)]
        ),
        SANDBOX_WORKTREE,
        "--remount-ro",
        "/dev",
        "--remount-ro",
        "/",
        "--chdir",
        cwd,
        "--clearenv",
        *setenv,
        "--",
        str(node),
        "--test",
        *command.flags,
        f"--test-reporter={SANDBOX_REPORTER}",
        "--test-reporter-destination=stdout",
        *files,
    ]
