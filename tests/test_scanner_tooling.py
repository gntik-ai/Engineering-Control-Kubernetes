import json
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


class SemgrepRecordingRunner:
    def __init__(self, return_code: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[str, list[str], Path]] = []
        self.return_code = return_code
        self.stderr = stderr

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
        report = Path(arguments[arguments.index("--output") + 1])
        if self.return_code in {0, 1}:
            report.write_text(json.dumps({"results": [], "errors": []}), encoding="utf-8")
        return CommandResult(self.return_code, "", self.stderr)


def test_semgrep_scan_uses_a_fixed_config_compatible_with_installed_semgrep(tmp_path: Path) -> None:
    # NEW-19, 2026-09-22: semgrep 1.174.0 (the version pinned by
    # scripts/install-scanners.sh) hard-fails "--config auto" whenever metrics
    # are off ("Cannot create auto config when metrics are off"), and rejects
    # "--no-suppress-errors" outright as an unknown flag -- so every delivery
    # precheck crashed on this call, unconditionally, before this fix.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = SemgrepRecordingRunner()
    scanner = DeterministicScanner(runner)  # type: ignore[arg-type]

    scanner.semgrep(worktree, tmp_path / "evidence")

    executable, arguments, cwd = runner.calls[0]
    assert executable == "semgrep"
    assert "auto" not in arguments
    assert "--no-suppress-errors" not in arguments
    assert arguments[arguments.index("--config") + 1] == "p/ci"
    assert cwd == worktree


def test_semgrep_failure_message_includes_stderr(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = SemgrepRecordingRunner(return_code=2, stderr="fatal error: bad config")
    scanner = DeterministicScanner(runner)  # type: ignore[arg-type]

    try:
        scanner.semgrep(worktree, tmp_path / "evidence")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as error:
        assert "bad config" in str(error)


def test_existing_gitleaks_report_discards_secret_and_match_values(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    fake_secret = "".join(("gh", "p_", "B" * 36))
    report = evidence / "gitleaks.json"
    report.write_text(
        json.dumps(
            [
                {
                    "RuleID": "github-pat",
                    "File": "example.env",
                    "StartLine": 4,
                    "Secret": fake_secret,
                    "Match": f"token={fake_secret}",
                }
            ]
        ),
        encoding="utf-8",
    )
    scanner = DeterministicScanner(RecordingRunner())  # type: ignore[arg-type]

    result = scanner.secret_scan(worktree, evidence)

    serialized = report.read_text(encoding="utf-8")
    assert result["status"] == "FINDINGS"
    assert fake_secret not in serialized
    assert "Match" not in serialized
    assert "[REDACTED]" in serialized
