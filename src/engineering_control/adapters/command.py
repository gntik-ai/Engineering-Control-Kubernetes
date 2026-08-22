from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from engineering_control.security.redaction import SecretRedactor


@dataclass(frozen=True)
class CommandResult:
    return_code: int
    stdout: str
    stderr: str


class CommandFailure(RuntimeError):
    pass


class SafeCommandRunner:
    """Run fixed argument vectors, never a shell, and redact all captured output."""

    def __init__(self, allowed_executables: Mapping[str, Path]) -> None:
        self.allowed_executables = dict(allowed_executables)
        self.redactor = SecretRedactor()

    def run(
        self,
        executable: str,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int = 900,
        environment: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        resolved = self.allowed_executables.get(executable)
        if resolved is None or not resolved.is_absolute():
            raise ValueError(f"executable is not allowed: {executable}")
        if not cwd.is_dir():
            raise ValueError(f"working directory does not exist: {cwd}")
        if any("\x00" in value for value in arguments):
            raise ValueError("arguments cannot contain NUL bytes")
        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
        }
        if environment:
            env.update(environment)
        process = subprocess.run(  # noqa: S603 - fixed executable allowlist; shell is never used
            [str(resolved), *arguments],
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            start_new_session=True,
        )
        stdout = str(self.redactor.redact_text(process.stdout).value)
        stderr = str(self.redactor.redact_text(process.stderr).value)
        result = CommandResult(process.returncode, stdout, stderr)
        if check and result.return_code:
            raise CommandFailure(
                f"{executable} failed with {result.return_code}: {result.stderr[-4000:]}"
            )
        return result


def minimal_git_environment() -> dict[str, str]:
    env: dict[str, str] = {}
    for name in ("SSH_AUTH_SOCK", "GIT_SSH_COMMAND"):
        if value := os.environ.get(name):
            env[name] = value
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
    return env
