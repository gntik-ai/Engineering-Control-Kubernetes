from __future__ import annotations

import hashlib
import json
import shutil
import stat
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from temporalio.exceptions import ApplicationError

from engineering_control.config import Settings


class FileCleanupCandidate(BaseModel):
    """One controller-derived, identity-bound file retention candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["delivery-run", "browser-output", "evidence-artifact", "provider-temp"]
    worker: Literal["controller", "delivery", "browser", "codex"]
    run_id: UUID
    reason: Literal[
        "TERMINAL_RUN_RETENTION_EXPIRED",
        "BROWSER_OUTPUT_RETENTION_EXPIRED",
        "EPHEMERAL_EVIDENCE_RETENTION_EXPIRED",
        "PROVIDER_TEMP_RETENTION_EXPIRED",
    ]
    not_modified_after: datetime
    deployment_id: UUID | None = None
    artifact_id: UUID | None = None
    relative_path: str | None = None
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    expected_size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def coherent_identity(self) -> FileCleanupCandidate:
        if self.kind == "delivery-run":
            if self.worker != "delivery" or any(
                value is not None
                for value in (
                    self.deployment_id,
                    self.artifact_id,
                    self.relative_path,
                    self.expected_sha256,
                    self.expected_size_bytes,
                )
            ):
                raise ValueError("delivery-run cleanup identity is inconsistent")
        elif self.kind == "browser-output":
            if (
                self.worker != "browser"
                or self.deployment_id is None
                or any(
                    value is not None
                    for value in (
                        self.artifact_id,
                        self.relative_path,
                        self.expected_sha256,
                        self.expected_size_bytes,
                    )
                )
            ):
                raise ValueError("browser-output cleanup identity is inconsistent")
        elif self.kind == "evidence-artifact":
            if (
                self.worker != "controller"
                or self.artifact_id is None
                or self.relative_path is None
                or self.expected_sha256 is None
                or self.expected_size_bytes is None
                or self.deployment_id is not None
            ):
                raise ValueError("evidence-artifact cleanup identity is inconsistent")
            path = PurePosixPath(self.relative_path)
            if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
                raise ValueError("evidence cleanup path must be bounded and relative")
        else:
            if self.worker != "codex" or any(
                value is not None
                for value in (
                    self.deployment_id,
                    self.artifact_id,
                    self.relative_path,
                    self.expected_sha256,
                    self.expected_size_bytes,
                )
            ):
                raise ValueError("provider temp cleanup identity is inconsistent")
        return self


class PreviewNamespaceCleanupCandidate(BaseModel):
    """One controller-derived, release-worker-owned preview namespace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    deployment_id: UUID
    change_set_id: UUID
    project: Literal["falcone", "musematic"]
    namespace: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
    )
    namespace_prefix: str = Field(pattern=r"^(falcone|musematic)-preview-$")
    target_context: str = Field(min_length=1, max_length=160)
    kube_context: str = Field(min_length=1, max_length=160)
    kubeconfig_path: Path
    finished_at: datetime
    reason: Literal["TERMINAL_CHANGESET_PREVIEW_RETENTION_EXPIRED"]

    @model_validator(mode="after")
    def coherent_identity(self) -> PreviewNamespaceCleanupCandidate:
        expected_prefix = f"{self.project}-preview-"
        if self.namespace_prefix != expected_prefix or not self.namespace.startswith(
            expected_prefix
        ):
            raise ValueError("preview cleanup namespace does not match its project prefix")
        if self.namespace == self.namespace_prefix:
            raise ValueError("preview cleanup namespace requires an instance suffix")
        if not self.kubeconfig_path.is_absolute():
            raise ValueError("preview cleanup kubeconfig must be an absolute scoped path")
        return self


def cleanup_authorization_sha256(
    *,
    workflow_id: str,
    worktrees: list[dict[str, Any]],
    files: list[dict[str, Any]],
    component_leases: list[dict[str, Any]],
    preview_namespaces: list[dict[str, Any]] | None = None,
) -> str:
    canonical = json.dumps(
        {
            "workflow_id": workflow_id,
            "worktrees": worktrees,
            "files": files,
            "component_leases": component_leases,
            "preview_namespaces": preview_namespaces or [],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


class RetentionActivityHandler:
    """Inspect and remove only controller-authorized, role-owned retention paths."""

    _steps = {
        "controller": frozenset(
            {"cleanup.inspect_evidence_files", "cleanup.execute_evidence_files"}
        ),
        "delivery": frozenset({"cleanup.inspect_delivery_files", "cleanup.execute_delivery_files"}),
        "browser": frozenset({"cleanup.inspect_browser_files", "cleanup.execute_browser_files"}),
        "codex": frozenset({"cleanup.inspect_codex_files", "cleanup.execute_codex_files"}),
    }

    def __init__(self, settings: Settings, worker_kind: str) -> None:
        if worker_kind not in self._steps:
            raise ValueError(
                "retention handler requires controller, delivery, browser, or codex identity"
            )
        self.settings = settings
        self.worker_kind = worker_kind
        self.supported_steps = self._steps[worker_kind]

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if step.startswith("cleanup.inspect_"):
                return self._inspect(payload)
            if step.startswith("cleanup.execute_"):
                return self._execute(payload)
        except (OSError, ValueError, ValidationError) as error:
            raise ApplicationError(
                "retention candidate failed bounded policy validation",
                type="PolicyViolation",
                non_retryable=True,
            ) from error
        raise ApplicationError("unsupported retention activity", non_retryable=True)

    def _inspect(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_candidates = payload.get("file_cleanup_candidates")
        if not isinstance(raw_candidates, list):
            raise ValueError("file cleanup inspection requires candidate records")
        existing = payload.get("inspected_file_candidates", [])
        if not isinstance(existing, list):
            raise ValueError("inspected cleanup candidates must be a list")
        inspected = list(existing)
        seen = {
            str(item.get("candidate_id"))
            for item in inspected
            if isinstance(item, dict) and item.get("candidate_id")
        }
        for raw in raw_candidates:
            candidate = FileCleanupCandidate.model_validate(raw)
            if candidate.worker != self.worker_kind or candidate.candidate_id in seen:
                continue
            path = self._path(candidate)
            record = candidate.model_dump(mode="json")
            if not path.exists() and not path.is_symlink():
                inspected.append({**record, "inspection": "ABSENT"})
                seen.add(candidate.candidate_id)
                continue
            manifest = self._manifest(path, candidate)
            if int(manifest["newest_mtime_ns"]) > int(
                candidate.not_modified_after.timestamp() * 1_000_000_000
            ):
                inspected.append({**record, "inspection": "MODIFIED_AFTER_CUTOFF"})
            else:
                inspected.append({**record, "inspection": "CLEAN", **manifest})
            seen.add(candidate.candidate_id)
        return {
            **payload,
            "status": "FILE_CLEANUP_INSPECTED",
            "inspected_file_candidates": inspected,
        }

    def _execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.cleanup_execution_enabled:
            raise ApplicationError(
                "retention cleanup execution is disabled by policy",
                type="SecurityBlocker",
                non_retryable=True,
            )
        authorized = payload.get("authorized_file_candidates")
        if not isinstance(authorized, list):
            raise ValueError("file cleanup execution requires controller authorization")
        self._validate_authorization(payload)
        removed = list(self._string_list(payload.get("removed_file_candidate_ids", [])))
        absent = list(self._string_list(payload.get("absent_file_candidate_ids", [])))
        completed = set(removed) | set(absent)
        for raw in authorized:
            if not isinstance(raw, dict):
                raise ValueError("authorized file cleanup candidate must be an object")
            candidate_fields = set(FileCleanupCandidate.model_fields)
            candidate = FileCleanupCandidate.model_validate(
                {key: value for key, value in raw.items() if key in candidate_fields}
            )
            if candidate.worker != self.worker_kind or candidate.candidate_id in completed:
                continue
            if raw.get("inspection") not in {"CLEAN", "ABSENT"}:
                raise ValueError("file cleanup candidate was not safely inspected")
            path = self._path(candidate)
            if not path.exists() and not path.is_symlink():
                absent.append(candidate.candidate_id)
                completed.add(candidate.candidate_id)
                continue
            manifest = self._manifest(path, candidate)
            if raw.get("inspection") != "CLEAN" or any(
                manifest.get(key) != raw.get(key)
                for key in ("manifest_sha256", "entry_count", "newest_mtime_ns")
            ):
                raise ValueError("file cleanup candidate changed after controller authorization")
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(candidate.candidate_id)
            completed.add(candidate.candidate_id)
        return {
            **payload,
            "status": "FILE_CLEANUP_EXECUTED",
            "removed_file_candidate_ids": sorted(set(removed)),
            "absent_file_candidate_ids": sorted(set(absent)),
        }

    def _validate_authorization(self, payload: dict[str, Any]) -> None:
        workflow_id = str(payload.get("orchestration", {}).get("workflow_id", ""))
        supplied = payload.get("cleanup_authorization_sha256")
        if not workflow_id or not isinstance(supplied, str):
            raise ValueError("file cleanup authorization identity is incomplete")
        expected = cleanup_authorization_sha256(
            workflow_id=workflow_id,
            worktrees=self._objects(payload.get("authorized_candidates", [])),
            files=self._objects(payload.get("authorized_file_candidates", [])),
            component_leases=self._objects(
                payload.get("authorized_component_lease_candidates", [])
            ),
            preview_namespaces=self._objects(
                payload.get("authorized_preview_namespace_candidates", [])
            ),
        )
        if supplied != expected:
            raise ValueError("file cleanup authorization changed after controller validation")

    def _path(self, candidate: FileCleanupCandidate) -> Path:
        if candidate.kind == "delivery-run":
            configured_root = self.settings.run_root / "delivery"
            root = configured_root.resolve()
            expected = root / str(candidate.run_id)
        elif candidate.kind == "browser-output":
            if candidate.deployment_id is None:
                raise ValueError("browser cleanup candidate is missing deployment identity")
            configured_root = self.settings.browser_evidence_root
            root = configured_root.resolve()
            expected = root / str(candidate.deployment_id) / str(candidate.run_id)
        elif candidate.kind == "provider-temp":
            configured_root = self.settings.temporary_root / "codex"
            root = configured_root.resolve()
            expected = root / str(candidate.run_id)
        else:
            if candidate.relative_path is None:
                raise ValueError("evidence cleanup candidate is missing relative path")
            configured_root = self.settings.evidence_root
            root = configured_root.resolve()
            expected = root.joinpath(*PurePosixPath(candidate.relative_path).parts)
        if configured_root.is_symlink() or expected == root or not expected.is_relative_to(root):
            raise ValueError("retention cleanup path escaped its exact bounded root")
        cursor = root
        for part in expected.relative_to(root).parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError("retention cleanup path contains a symbolic link")
        return expected

    @staticmethod
    def _manifest(path: Path, candidate: FileCleanupCandidate) -> dict[str, int | str]:
        root_metadata = path.lstat()
        if stat.S_ISLNK(root_metadata.st_mode):
            raise ValueError("retention cleanup root cannot be a symbolic link")
        records: list[tuple[str, str, int, int, int]] = []
        newest = root_metadata.st_mtime_ns

        def append(candidate_path: Path, relative: str) -> None:
            nonlocal newest
            metadata = candidate_path.lstat()
            newest = max(newest, metadata.st_mtime_ns)
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("retention cleanup tree cannot contain symbolic links")
            if stat.S_ISDIR(metadata.st_mode):
                kind = "directory"
                size = 0
            elif stat.S_ISREG(metadata.st_mode):
                kind = "file"
                size = metadata.st_size
            else:
                raise ValueError("retention cleanup tree contains a special file")
            records.append(
                (relative, kind, size, metadata.st_mtime_ns, stat.S_IMODE(metadata.st_mode))
            )
            if len(records) > 100_000:
                raise ValueError("retention cleanup tree exceeds its bounded entry count")

        append(path, ".")
        if path.is_dir():
            for child in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
                append(child, child.relative_to(path).as_posix())
        elif candidate.kind != "evidence-artifact":
            raise ValueError("run, browser, and provider cleanup candidates must be directories")
        if candidate.kind == "evidence-artifact":
            if not path.is_file():
                raise ValueError("evidence cleanup candidate must be a regular file")
            if path.stat().st_size != candidate.expected_size_bytes:
                raise ValueError("evidence cleanup candidate size changed")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != candidate.expected_sha256:
                raise ValueError("evidence cleanup candidate content changed")
        encoded = json.dumps(records, separators=(",", ":"), sort_keys=False).encode()
        return {
            "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
            "entry_count": len(records),
            "newest_mtime_ns": newest,
        }

    @staticmethod
    def _objects(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValueError("cleanup authorization lists must contain objects")
        return value

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("cleanup result identifiers must be strings")
        return value
