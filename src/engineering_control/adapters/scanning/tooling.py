from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from engineering_control.adapters.command import SafeCommandRunner
from engineering_control.security.redaction import REDACTED, SecretRedactor


class DeterministicScanner:
    def __init__(self, runner: SafeCommandRunner) -> None:
        self.runner = runner
        self.redactor = SecretRedactor()

    def secret_scan(self, worktree: Path, evidence_directory: Path) -> dict[str, Any]:
        existing = evidence_directory / "gitleaks.json"
        if existing.is_file():
            payload = self._sanitize_gitleaks(existing)
            return {"status": "FINDINGS" if payload else "PASS", "findings": payload}
        report = self._report_path(evidence_directory, "gitleaks.json")
        result = self.runner.run(
            "gitleaks",
            [
                "dir",
                "--redact=100",
                "--report-format",
                "json",
                "--report-path",
                str(report),
                "--no-banner",
                "--exit-code",
                "2",
                str(worktree),
            ],
            cwd=worktree,
            check=False,
        )
        if result.return_code not in {0, 2}:
            raise RuntimeError(
                f"gitleaks execution failed ({result.return_code}): {result.stderr[-2000:]}"
            )
        payload = self._sanitize_gitleaks(report)
        return {"status": "FINDINGS" if payload else "PASS", "findings": payload}

    def vulnerability_scan(self, worktree: Path, evidence_directory: Path) -> dict[str, Any]:
        existing = evidence_directory / "trivy.json"
        if existing.is_file():
            return self._sanitize_trivy(existing)
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
            raise RuntimeError(
                f"Trivy execution failed ({result.return_code}): {result.stderr[-2000:]}"
            )
        return self._sanitize_trivy(report)

    def semgrep(self, worktree: Path, evidence_directory: Path) -> dict[str, Any]:
        existing = evidence_directory / "semgrep.json"
        if existing.is_file():
            return self._sanitize_semgrep(existing)
        report = self._report_path(evidence_directory, "semgrep.json")
        result = self.runner.run(
            "semgrep",
            [
                "scan",
                # NEW-19, 2026-09-22: "--config auto" hard-fails on semgrep 1.174.0
                # ("Cannot create auto config when metrics are off") because auto
                # mode's registry lookup requires the metrics ping this platform
                # deliberately disables. p/ci is semgrep's own general-purpose
                # default ruleset and works fine with metrics off.
                "--config",
                "p/ci",
                "--json",
                "--output",
                str(report),
                "--metrics=off",
                str(worktree),
            ],
            cwd=worktree,
            check=False,
            timeout_seconds=1800,
        )
        if result.return_code not in {0, 1}:
            raise RuntimeError(
                f"Semgrep execution failed ({result.return_code}): {result.stderr[-2000:]}"
            )
        return self._sanitize_semgrep(report)

    def sbom(self, worktree: Path, evidence_directory: Path) -> dict[str, Any]:
        existing = evidence_directory / "sbom.cyclonedx.json"
        if existing.is_file():
            return self._sanitize_object(existing)
        report = self._report_path(evidence_directory, "sbom.cyclonedx.json")
        result = self.runner.run(
            "syft",
            [str(worktree), "--output", f"cyclonedx-json={report}", "--quiet"],
            cwd=worktree,
            check=False,
            timeout_seconds=1800,
        )
        if result.return_code:
            raise RuntimeError(
                f"Syft execution failed ({result.return_code}): {result.stderr[-2000:]}"
            )
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

    def _sanitize_gitleaks(self, report: Path) -> list[dict[str, Any]]:
        payload = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Gitleaks report must contain a JSON array")
        safe: list[dict[str, Any]] = []
        allowed = {
            "RuleID",
            "Description",
            "File",
            "StartLine",
            "EndLine",
            "Commit",
            "Entropy",
            "Fingerprint",
        }
        for finding in payload:
            if not isinstance(finding, dict):
                continue
            item = {key: finding[key] for key in allowed if key in finding}
            item["Secret"] = REDACTED
            safe.append(item)
        self._atomic_json(report, safe)
        return safe

    def _sanitize_trivy(self, report: Path) -> dict[str, Any]:
        payload = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Trivy report must contain a JSON object")
        results: list[dict[str, Any]] = []
        for raw_result in payload.get("Results", []):
            if not isinstance(raw_result, dict):
                continue
            result: dict[str, Any] = {
                key: raw_result[key] for key in ("Target", "Class", "Type") if key in raw_result
            }
            selections = {
                "Vulnerabilities": (
                    "VulnerabilityID",
                    "PkgName",
                    "InstalledVersion",
                    "FixedVersion",
                    "Severity",
                    "Title",
                    "PrimaryURL",
                ),
                "Misconfigurations": (
                    "ID",
                    "AVDID",
                    "Type",
                    "Title",
                    "Message",
                    "Resolution",
                    "Severity",
                    "PrimaryURL",
                ),
                "Secrets": (
                    "RuleID",
                    "Category",
                    "Severity",
                    "Title",
                    "StartLine",
                    "EndLine",
                ),
                "Licenses": ("PkgName", "Name", "Category", "Severity", "Status"),
            }
            for category, fields in selections.items():
                findings = raw_result.get(category)
                if not isinstance(findings, list):
                    continue
                result[category] = [
                    {key: finding[key] for key in fields if key in finding}
                    for finding in findings
                    if isinstance(finding, dict)
                ]
            results.append(result)
        safe = {
            "SchemaVersion": payload.get("SchemaVersion"),
            "ArtifactName": payload.get("ArtifactName"),
            "ArtifactType": payload.get("ArtifactType"),
            "Results": results,
        }
        redacted = self.redactor.redact(safe).value
        if not isinstance(redacted, dict):
            raise TypeError("redacted Trivy report must remain an object")
        self._atomic_json(report, redacted)
        return redacted

    def _sanitize_semgrep(self, report: Path) -> dict[str, Any]:
        payload = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Semgrep report must contain a JSON object")
        results: list[dict[str, Any]] = []
        for finding in payload.get("results", []):
            if not isinstance(finding, dict):
                continue
            raw_extra = finding.get("extra")
            extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
            results.append(
                {
                    "check_id": finding.get("check_id"),
                    "path": finding.get("path"),
                    "start": finding.get("start"),
                    "end": finding.get("end"),
                    "extra": {
                        "message": extra.get("message"),
                        "severity": extra.get("severity"),
                    },
                }
            )
        safe = {
            "version": payload.get("version"),
            "errors": self.redactor.redact(payload.get("errors", [])).value,
            "results": results,
        }
        self._atomic_json(report, safe)
        return safe

    @staticmethod
    def _atomic_json(report: Path, value: Any) -> None:
        payload = json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
        with tempfile.NamedTemporaryFile(dir=report.parent, delete=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o640)
        os.replace(temporary, report)
