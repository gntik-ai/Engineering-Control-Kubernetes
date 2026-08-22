from pathlib import Path

from engineering_control.adapters.command import CommandResult
from engineering_control.adapters.scanning.tooling import DeterministicScanner


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], Path]] = []

    def run(  # type: ignore[no-untyped-def]
        self,
        executable,
        arguments,
        *,
        cwd,
        timeout_seconds=900,
        environment=None,
        check=True,
    ) -> CommandResult:
        self.calls.append((executable, list(arguments), cwd))
        report = Path(arguments[arguments.index("--report-path") + 1])
        report.write_text("[]\n", encoding="utf-8")
        return CommandResult(0, "", "")


def test_secret_scan_uses_supported_directory_mode(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = RecordingRunner()
    scanner = DeterministicScanner(runner)  # type: ignore[arg-type]

    result = scanner.secret_scan(worktree, tmp_path / "evidence")

    assert result == {"status": "PASS", "findings": []}
    executable, arguments, cwd = runner.calls[0]
    assert executable == "gitleaks"
    assert arguments[0] == "dir"
    assert "detect" not in arguments
    assert arguments[-1] == str(worktree)
    assert cwd == worktree
