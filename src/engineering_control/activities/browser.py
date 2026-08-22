from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import SafeCommandRunner
from engineering_control.config import Settings
from engineering_control.evidence.transfer import EvidencePublisher
from engineering_control.models.enums import Project, RedactionStatus
from engineering_control.security.redaction import SecretRedactor


class BrowserActivityHandler:
    """Run an allowlisted browser journey without placing credentials in workflow history."""

    _credential_name = re.compile(r"^JOURNEY_[A-Z0-9_]+$")

    def __init__(
        self,
        settings: Settings,
        runner: SafeCommandRunner | None = None,
        publisher: EvidencePublisher | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner or SafeCommandRunner({"node": settings.node_executable})
        self.redactor = SecretRedactor()
        self.publisher = publisher or EvidencePublisher(settings, settings.browser_evidence_root)

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        if step != "runtime.browser_checks":
            raise ApplicationError("unsupported browser activity", non_retryable=True)
        plan = self._bounded_file(payload.get("journey_plan_path"), self.settings.journey_plan_root)
        self._validate_plan(plan)
        output = self._bounded_output(payload.get("output_directory"))
        self._clear_previous_attempt(output)
        credentials, credential_path = self._credentials(payload.get("credential_file"))
        environment = {
            "ENGINEERING_JOURNEY_PLAN": str(plan),
            "ENGINEERING_JOURNEY_OUTPUT": str(output),
            **credentials,
        }
        result = self.runner.run(
            "node",
            [str(self.settings.browser_script)],
            cwd=self.settings.browser_script.parent,
            timeout_seconds=3600,
            environment=environment,
            check=False,
        )
        result_path = output / "result.json"
        if not result_path.is_file():
            raise ApplicationError(
                f"browser runner failed without a result ({result.return_code})",
                type="BrowserRunnerFailure",
            )
        report = json.loads(result_path.read_text(encoding="utf-8"))
        safe = self._redact_known(self.redactor.redact(report).value, set(credentials.values()))
        self._atomic_json(result_path, safe)
        artifacts = []
        for artifact in sorted(output.iterdir()):
            if not artifact.is_file():
                continue
            os.chmod(artifact, 0o600)
            artifacts.append(
                {
                    "name": artifact.name,
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "size_bytes": artifact.stat().st_size,
                    "redaction_status": (
                        "REDACTED" if artifact == result_path else "REVIEW_REQUIRED"
                    ),
                }
            )
        if credential_path is not None and any(
            item["name"] in {"trace.zip", "final.png", "failure.png"} for item in artifacts
        ):
            raise ApplicationError(
                "credentialed journey unexpectedly produced visual evidence",
                type="SecurityBlocker",
                non_retryable=True,
            )
        receipts = self._publish_result(payload, result_path)
        return {
            **payload,
            "status": safe.get("status", "FAILED") if isinstance(safe, dict) else "FAILED",
            "result": safe,
            "artifacts": artifacts,
            "evidence_receipts": receipts,
        }

    def _publish_result(self, payload: dict[str, Any], result_path: Path) -> list[dict[str, Any]]:
        orchestration = payload.get("orchestration")
        project = payload.get("project")
        change_set_id = payload.get("change_set_id")
        if (
            not isinstance(orchestration, dict)
            or not isinstance(orchestration.get("workflow_id"), str)
            or not isinstance(project, str)
            or not isinstance(change_set_id, str)
        ):
            if self.settings.environment == "production":
                raise ApplicationError(
                    "production browser evidence requires authoritative workflow metadata",
                    type="SecurityBlocker",
                    non_retryable=True,
                )
            return []
        receipt = self.publisher.publish(
            result_path,
            project=Project(project),
            workflow_id=str(orchestration["workflow_id"]),
            change_set_id=UUID(change_set_id),
            category="changes",
            filename=f"runtime-browser-{payload['deployment_id']}.json",
            producer="browser-runner",
            content_type="application/json",
            redaction_status=RedactionStatus.REDACTED,
        )
        return [receipt]

    def _validate_plan(self, path: Path) -> None:
        try:
            schema = json.loads(self.settings.journey_schema.read_text(encoding="utf-8"))
            plan = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(plan)
        except (OSError, json.JSONDecodeError, SchemaError, ValidationError) as error:
            raise ApplicationError(
                "browser journey failed strict schema validation",
                type="PolicyViolation",
                non_retryable=True,
            ) from error

    @staticmethod
    def _bounded_file(value: Any, root: Path) -> Path:
        if not isinstance(value, str):
            raise ApplicationError("browser plan path is required", non_retryable=True)
        resolved_root = root.resolve()
        path = Path(value).resolve()
        if not path.is_file() or not path.is_relative_to(resolved_root):
            raise ApplicationError(
                "browser plan is outside its configured root",
                type="PolicyViolation",
                non_retryable=True,
            )
        return path

    def _bounded_output(self, value: Any) -> Path:
        if not isinstance(value, str):
            raise ApplicationError("browser output directory is required", non_retryable=True)
        root = self.settings.browser_evidence_root.resolve()
        path = Path(value).resolve()
        if path == root or not path.is_relative_to(root):
            raise ApplicationError(
                "browser output is outside its evidence root",
                type="PolicyViolation",
                non_retryable=True,
            )
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
        return path

    @staticmethod
    def _clear_previous_attempt(output: Path) -> None:
        for filename in ("result.json", "trace.zip", "final.png", "failure.png"):
            candidate = output / filename
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink()

    def _credentials(self, value: Any) -> tuple[dict[str, str], Path | None]:
        if value is None:
            return {}, None
        path = self._bounded_file(value, self.settings.browser_credential_root)
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ApplicationError(
                "browser credential file grants group or other access",
                type="PolicyViolation",
                non_retryable=True,
            )
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#"):
                continue
            name, separator, raw = line.partition("=")
            if not separator or not self._credential_name.fullmatch(name):
                raise ApplicationError(
                    "browser credential file contains an invalid entry",
                    type="PolicyViolation",
                    non_retryable=True,
                )
            values[name] = raw
        if not values or any(not item for item in values.values()):
            raise ApplicationError(
                "browser credential file is empty or incomplete",
                type="PolicyViolation",
                non_retryable=True,
            )
        return values, path

    @classmethod
    def _redact_known(cls, value: Any, secrets: set[str]) -> Any:
        if isinstance(value, str):
            result = value
            for secret in sorted((item for item in secrets if item), key=len, reverse=True):
                result = result.replace(secret, "[REDACTED:journey-credential]")
            return result
        if isinstance(value, dict):
            return {key: cls._redact_known(item, secrets) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact_known(item, secrets) for item in value]
        return value

    @staticmethod
    def _atomic_json(path: Path, value: Any) -> None:
        content = json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
