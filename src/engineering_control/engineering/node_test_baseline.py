"""NEW-161: pre-existing failures of a maker-chosen ``node --test`` suite.

NEW-49 only reruns a project's toolchain contract test commands at the base commit. A
project without a contract (falcone) therefore blocked forever on a maker FAIL in any
suite that already fails on main (#978: tests/integration/098-plan-base-limits fails 9
of 16 on main with "Unhandled fake SQL").

This module is pure: a strict parser for one allowlisted invocation shape, a Python
glob expansion against a checkout, the reporter the scan worker controls, the parser of
its output, the bubblewrap argument vector the scan worker runs it in, and the subset
rule. Anything it does not accept returns None, which keeps MAKER_TEST_FAILURE.
"""

from __future__ import annotations

import glob
import json
import re
import shlex
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# Bounds (the scan worker also bounds the number of commands and the run time).
MAX_PATH_ARGUMENTS = 32
MAX_TEST_FILES = 400
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_FAILING_TESTS = 5000
EVIDENCE_TEST_NAMES = 50
EVIDENCE_NAME_CHARS = 200

# Inside the sandbox: the checkout is an overlay at /work (writes are discarded), so
# reported file paths are identical for the base and the head run.
SANDBOX_WORKTREE = "/work"
SANDBOX_CONTROL = "/run/hermes-node-test"
SANDBOX_REPORTER = f"{SANDBOX_CONTROL}/reporter.mjs"
SANDBOX_OUTPUT = f"{SANDBOX_CONTROL}/out"
SANDBOX_REPORT = f"{SANDBOX_OUTPUT}/report.jsonl"
SANDBOX_TMP = "/tmp"  # noqa: S108 - a private tmpfs inside the sandbox, never the host's

REPORTER_VERSION = 1
# One JSON line per finished test, written to a file the test code never prints to.
# skip/todo are not failures (node:test does not fail the run for them either).
REPORTER_SOURCE = """\
// Hermes NEW-161 node:test reporter v1 (scan worker, pre-existing failure baseline).
export default async function* hermesReporter(source) {
  for await (const event of source) {
    if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
    const data = event.data ?? {};
    if (data.skip || data.todo) continue;
    yield JSON.stringify({
      hermes: 1,
      event: event.type === 'test:fail' ? 'fail' : 'pass',
      name: String(data.name ?? ''),
      nesting: Number.isInteger(data.nesting) ? data.nesting : 0,
      file: typeof data.file === 'string' ? data.file : null,
    }) + '\\n';
  }
}
"""

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


def expand_test_paths(checkout: Path, command: NodeTestCommand) -> list[str] | None:
    """Expand the paths in Python against one checkout (never a shell).

    Returns the sorted, de-duplicated files relative to the cwd, or None when a path
    matches nothing, leaves the checkout, or the bound is exceeded.
    """
    root = checkout.resolve()
    base = (root / command.cwd).resolve()
    if not base.is_relative_to(root) or not base.is_dir():
        return None
    files: list[str] = []
    for pattern in command.paths:
        matches = sorted(glob.glob(pattern, root_dir=base))  # hidden entries excluded
        if not any(character in pattern for character in "*?"):
            matches = [pattern] if (base / pattern).is_file() else []
        selected = [
            match
            for match in matches
            if _FILE.fullmatch(match)
            and not _has_dot_segment(match)
            and (base / match).is_file()
            and (base / match).resolve().is_relative_to(root)
        ]
        if not selected:
            return None
        files.extend(item for item in selected if item not in files)
        if len(files) > MAX_TEST_FILES:
            return None
    return files


@dataclass(frozen=True)
class NodeTestReport:
    failing: Counter[str]
    passed: int


def parse_reporter_output(text: str) -> NodeTestReport | None:
    """The reporter's JSON lines -> failing test identities; None when malformed.

    A test identity is ``<file relative to the checkout>#<nesting>: <name>`` so equal
    names in different files (or nesting levels) never mask each other; the multiset
    comparison also keeps duplicate identities apart.
    """
    failing: Counter[str] = Counter()
    passed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            return None
        if (
            not isinstance(record, dict)
            or record.get("hermes") != REPORTER_VERSION
            or record.get("event") not in {"pass", "fail"}
            or not isinstance(record.get("name"), str)
            or not isinstance(record.get("nesting"), int)
            or not (record.get("file") is None or isinstance(record.get("file"), str))
        ):
            return None
        if record["event"] == "pass":
            passed += 1
            continue
        file = record["file"] or ""
        if file == SANDBOX_WORKTREE or file.startswith(f"{SANDBOX_WORKTREE}/"):
            file = file[len(SANDBOX_WORKTREE) + 1 :]
        failing[f"{file}#{record['nesting']}: {record['name']}"] += 1
        if sum(failing.values()) > MAX_FAILING_TESTS:
            return None
    return NodeTestReport(failing, passed)


def regressions(base: Counter[str], head: Counter[str]) -> list[str]:
    """Head failures not covered by base failures (multiset difference)."""
    return sorted((head - base).elements())


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
    output: Path,
    variables: dict[str, str],
) -> list[str]:
    """bubblewrap arguments: no network, read-only system, checkout as a discarded overlay.

    ``node`` must be the resolved executable; its installation is bound read-only.
    """
    for path in (checkout, node, reporter, output):
        if not path.is_absolute() or any(character in str(path) for character in ":,\n"):
            raise ValueError(f"node test sandbox path {path} is not a plain absolute path")
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
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        SANDBOX_TMP,
        "--ro-bind",
        str(reporter),
        SANDBOX_REPORTER,
        "--bind",
        str(output),
        SANDBOX_OUTPUT,
        "--overlay-src",
        str(checkout),
        "--tmp-overlay",
        SANDBOX_WORKTREE,
        "--chdir",
        cwd,
        "--clearenv",
        *setenv,
        "--",
        str(node),
        "--test",
        *command.flags,
        f"--test-reporter={SANDBOX_REPORTER}",
        f"--test-reporter-destination={SANDBOX_REPORT}",
        *files,
    ]
