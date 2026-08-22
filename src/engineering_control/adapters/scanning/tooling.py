from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from engineering_control.adapters.command import SafeCommandRunner
from engineering_control.security.redaction import SecretRedactor


class DeterministicScanner:
    def __init__(self, runner: SafeCommandRunner) -> None:
        self.runner = runner
        self.redactor = SecretRedactor()

    def secret_scan(self, worktree: Path, evidence_directory: Path) -> dict[str, Any]:
        report = self._report_path(evidence_directory, "gitleaks.json")
        result = self.runner.run(
            "gitleaks",
            [
                "detect",
                "--source",
                str(worktree),
                "--redact=100",
                "--report-format",
                "json",
                "--report-path",
                str(report),
                "--no-banner",
                "--exit-code",
                "2",
            ],
            cwd=worktree,
            check=False,
        )
        if result.return_code not in {0, 2}:
            raise RuntimeError("gitleaks execution failed")
        payload = self._sanitize_json(report)
        return {"status": "FINDINGS" if payload else "PASS", "findings": payload}

    def vulnerability_scan(self, worktree: Path, evidence_directory: Path) -> dict[str, Any]:
        report = self._report_path(evidence_directory, "trivy.json")
        result = self.runner.run(
            "trivy",
            [
                "filesystem",
                "--format",
                "json",
                "--output",
                str(report),
                "--scanners",
                "vuln,misconfig,secret,license",
                "--exit-code",
                "0",
                str(worktree),
            ],
            cwd=worktree,
            check=False,
            timeout_seconds=1800,
        )
        if result.return_code:
            raise RuntimeError("Trivy execution failed")
        return self._sanitize_object(report)

    def semgrep(self, worktree: Path, evidence_directory: Path) -> dict[str, Any]:
        report = self._report_path(evidence_directory, "semgrep.json")
        result = self.runner.run(
            "semgrep",
            [
                "scan",
                "--config",
                "auto",
                "--json",
                "--output",
                str(report),
                "--metrics=off",
                "--no-suppress-errors",
                str(worktree),
            ],
            cwd=worktree,
            check=False,
            timeout_seconds=1800,
        )
        if result.return_code not in {0, 1}:
            raise RuntimeError("Semgrep execution failed")
        return self._sanitize_object(report)

    def sbom(self, worktree: Path, evidence_directory: Path) -> dict[str, Any]:
        report = self._report_path(evidence_directory, "sbom.cyclonedx.json")
        result = self.runner.run(
            "syft",
            [str(worktree), "--output", f"cyclonedx-json={report}", "--quiet"],
            cwd=worktree,
            check=False,
            timeout_seconds=1800,
        )
        if result.return_code:
            raise RuntimeError("Syft execution failed")
        return self._sanitize_object(report)

    def validate_manifests(self, manifest: Path, worktree: Path) -> None:
        if not manifest.is_file() or not manifest.resolve().is_relative_to(worktree.resolve()):
            raise ValueError("manifest must be inside the bounded worktree")
        self.runner.run(
            "kubeconform",
            [
                "-strict",
                "-summary",
                "-ignore-missing-schemas",
                "-schema-location",
                "default",
                str(manifest),
            ],
            cwd=worktree,
        )

    @staticmethod
    def _report_path(directory: Path, name: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        report = directory / name
        if report.exists():
            raise FileExistsError(f"scanner report already exists: {report}")
        return report

    def _sanitize_json(self, report: Path) -> Any:
        value = json.loads(report.read_text(encoding="utf-8"))
        safe = self.redactor.redact(value).value
        payload = json.dumps(safe, indent=2, sort_keys=True).encode() + b"\n"
        with tempfile.NamedTemporaryFile(dir=report.parent, delete=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o640)
        os.replace(temporary, report)
        return safe

    def _sanitize_object(self, report: Path) -> dict[str, Any]:
        payload = self._sanitize_json(report)
        if not isinstance(payload, dict):
            raise ValueError(f"scanner report must contain a JSON object: {report.name}")
        return payload
