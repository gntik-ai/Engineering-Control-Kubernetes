#!/usr/bin/env bash
# NEW-130: remove the per-worktree Codex project trust entries from a runner's config.
#
# Codex writes `[projects."<dir>"] trust_level = "trusted"` into $CODEX_HOME/config.toml
# for directories it runs in; on the worker that grew to one entry per past ChangeSet
# worktree. A trusted project's `.codex/config.toml` (MCP servers, agents, model,
# sandbox, approval policy, hooks) is loaded by Codex. The Codex adapter now forces
# every run's worktree untrusted per invocation; this script removes the stale entries.
#
# Only `[projects."<path>"]` tables (and their sub-tables) whose path is the prefix or
# lies under it are removed. Every other line is kept byte-for-byte; the parsed TOML
# after pruning must equal the parsed TOML before minus exactly those entries, or
# nothing is written.
#
# Usage (as the runner user that owns the config, e.g. codex-runner; never as root):
#   sudo -u codex-runner -H scripts/prune-codex-project-trust.sh --dry-run
#   sudo -u codex-runner -H scripts/prune-codex-project-trust.sh
#   scripts/prune-codex-project-trust.sh --config <path> [--prefix <dir>] [--dry-run]
#
# Defaults: --config "${CODEX_HOME:-$HOME/.codex}/config.toml",
#           --prefix /srv/engineering/worktrees.
# A real run first copies the file to <config>.bak-<UTC timestamp> (mode 0600), then
# replaces it atomically (same directory temp file, fsync, rename) keeping its mode.
set -euo pipefail
umask 077

config="${CODEX_HOME:-${HOME:?}/.codex}/config.toml"
prefix=/srv/engineering/worktrees
dry_run=0

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

while (($#)); do
  case "$1" in
    --config) config="${2:?--config needs a path}"; shift 2 ;;
    --prefix) prefix="${2:?--prefix needs a directory}"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$(id -u)" == 0 ]]; then
  echo "refusing to run as root; run as the runner user that owns $config" >&2
  exit 2
fi
if [[ "$prefix" != /* ]]; then
  echo "--prefix must be an absolute directory" >&2
  exit 2
fi
if [[ ! -f "$config" || -L "$config" ]]; then
  echo "config is not a regular file: $config" >&2
  exit 2
fi
if [[ "$(stat -c %u -- "$config")" != "$(id -u)" ]]; then
  echo "config is not owned by $(id -un); run as its owner: $config" >&2
  exit 2
fi

exec "${PYTHON:-python3}" - "$config" "$prefix" "$dry_run" <<'PY'
import os
import posixpath
import re
import sys
import tempfile
import tomllib
from datetime import UTC, datetime

config, prefix, dry_run = sys.argv[1], posixpath.normpath(sys.argv[2]), sys.argv[3] == "1"

with open(config, "rb") as handle:
    raw = handle.read()
text = raw.decode("utf-8")
try:
    before = tomllib.loads(text)
except tomllib.TOMLDecodeError as error:
    sys.exit(f"config is not valid TOML before pruning: {error}")


def under_prefix(path: str) -> bool:
    if not path.startswith("/"):
        return False
    normalized = posixpath.normpath(path)
    return normalized == prefix or normalized.startswith(prefix.rstrip("/") + "/")


# [projects."<path>"] / [projects.'<path>'] and their sub-tables [projects."<path>".x].
HEADER = re.compile(r"""^\s*\[\s*projects\s*\.\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)')\s*[\].]""")
TABLE = re.compile(r"^\s*\[")

projects = before.get("projects")
pruned = sorted(
    key for key in (projects if isinstance(projects, dict) else {}) if under_prefix(key)
)

kept: list[str] = []
dropping = False
for line in text.splitlines(keepends=True):
    if TABLE.match(line):
        match = HEADER.match(line)
        if match is None:
            dropping = False
        else:
            quoted, literal = match.groups()
            key = literal if literal is not None else tomllib.loads(f'k = "{quoted}"')["k"]
            dropping = under_prefix(key)
    if not dropping:
        kept.append(line)
result = "".join(kept)

try:
    after = tomllib.loads(result)
except tomllib.TOMLDecodeError as error:
    sys.exit(f"pruned config would not be valid TOML; nothing written: {error}")
expected = dict(before)
if isinstance(projects, dict):
    remaining = {key: value for key, value in projects.items() if key not in pruned}
    if remaining or "projects" in after:
        expected["projects"] = remaining
    else:
        expected.pop("projects", None)
if after != expected:
    sys.exit(
        "pruning would change more than the matching [projects] tables "
        "(inline or unusual layout?); nothing written"
    )

for key in pruned:
    print(f"prune: {key}")
print(f"{len(pruned)} of {len(projects) if isinstance(projects, dict) else 0} project entries "
      f"under {prefix} {'would be ' if dry_run else ''}removed from {config}")
if dry_run or not pruned:
    sys.exit(0)

mode = os.stat(config).st_mode & 0o7777
directory = os.path.dirname(os.path.abspath(config))
backup = f"{config}.bak-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
with open(backup, "xb") as handle:
    handle.write(raw)
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(backup, 0o600)
descriptor, temporary = tempfile.mkstemp(prefix=".config.toml.", dir=directory)
try:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(result.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    with open(temporary, "rb") as handle:
        if tomllib.loads(handle.read().decode("utf-8")) != after:
            sys.exit("written temp file does not match the validated result; nothing replaced")
    os.replace(temporary, config)
except BaseException:
    if os.path.exists(temporary):
        os.unlink(temporary)
    raise
directory_fd = os.open(directory, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
with open(config, "rb") as handle:
    tomllib.loads(handle.read().decode("utf-8"))
print(f"backup: {backup}")
PY
