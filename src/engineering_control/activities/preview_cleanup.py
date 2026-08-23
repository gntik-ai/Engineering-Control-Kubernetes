from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import SafeCommandRunner
from engineering_control.config import Settings

from .retention import (
    PreviewNamespaceCleanupCandidate,
    cleanup_authorization_sha256,
)


class PreviewCleanupActivityHandler:
    """Delete only controller-authorized, managed, expired preview namespaces."""

    supported_steps = frozenset(
        {"cleanup.inspect_preview_namespaces", "cleanup.execute_preview_namespaces"}
    )
    _required_label = "engineering.gntik.ai/managed-by"
    _deployment_label = "engineering.gntik.ai/deployment-id"
    _change_set_label = "engineering.gntik.ai/change-set-id"

    def __init__(
        self,
        settings: Settings,
        *,
        runner: SafeCommandRunner | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner or SafeCommandRunner({"kubectl": settings.kubectl_executable})

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if step == "cleanup.inspect_preview_namespaces":
                return self._inspect(payload)
            if step == "cleanup.execute_preview_namespaces":
                return self._execute(payload)
        except (OSError, ValueError, ValidationError) as error:
            raise ApplicationError(
                "preview cleanup failed bounded policy validation",
                type="PolicyViolation",
                non_retryable=True,
            ) from error
        raise ApplicationError("unsupported preview cleanup activity", non_retryable=True)

    def _inspect(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_candidates = payload.get("preview_namespace_cleanup_candidates")
        existing = payload.get("inspected_preview_namespace_candidates", [])
        if not isinstance(raw_candidates, list) or not isinstance(existing, list):
            raise ValueError("preview cleanup inspection requires candidate lists")
        inspected = list(existing)
        seen = {
            str(value.get("candidate_id"))
            for value in inspected
            if isinstance(value, dict) and value.get("candidate_id")
        }
        for raw in raw_candidates:
            candidate = PreviewNamespaceCleanupCandidate.model_validate(raw)
            if candidate.candidate_id in seen:
                continue
            record = candidate.model_dump(mode="json")
            observation = self._observe(candidate)
            if observation is None:
                inspected.append({**record, "inspection": "ABSENT"})
            elif observation.pop("managed") is not True:
                inspected.append({**record, "inspection": "PROTECTED"})
            else:
                inspected.append({**record, "inspection": "CLEAN", **observation})
            seen.add(candidate.candidate_id)
        return {
            **payload,
            "status": "PREVIEW_CLEANUP_INSPECTED",
            "inspected_preview_namespace_candidates": inspected,
        }

    def _execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.cleanup_execution_enabled:
            raise ApplicationError(
                "preview cleanup execution is disabled by policy",
                type="SecurityBlocker",
                non_retryable=True,
            )
        authorized = self._objects(payload.get("authorized_preview_namespace_candidates"))
        self._validate_authorization(payload)
        removed = self._string_list(payload.get("removed_preview_namespace_candidate_ids", []))
        absent = self._string_list(payload.get("absent_preview_namespace_candidate_ids", []))
        completed = set(removed) | set(absent)
        candidate_fields = set(PreviewNamespaceCleanupCandidate.model_fields)
        for raw in authorized:
            candidate = PreviewNamespaceCleanupCandidate.model_validate(
                {key: value for key, value in raw.items() if key in candidate_fields}
            )
            if candidate.candidate_id in completed:
                continue
            if raw.get("inspection") not in {"CLEAN", "ABSENT"}:
                raise ValueError("preview namespace was not safely inspected")
            observation = self._observe(candidate)
            if observation is None:
                absent.append(candidate.candidate_id)
                completed.add(candidate.candidate_id)
                continue
            if observation.pop("managed") is not True or any(
                observation.get(key) != raw.get(key)
                for key in ("uid", "resource_version", "labels_sha256")
            ):
                raise ValueError("preview namespace changed after controller authorization")
            self.runner.run(
                "kubectl",
                [
                    "--kubeconfig",
                    str(candidate.kubeconfig_path),
                    "--context",
                    candidate.kube_context,
                    "delete",
                    "namespace",
                    candidate.namespace,
                    "--wait=true",
                    "--timeout=120s",
                    "--ignore-not-found=true",
                ],
                cwd=Path("/"),
                timeout_seconds=180,
            )
            removed.append(candidate.candidate_id)
            completed.add(candidate.candidate_id)
        return {
            **payload,
            "status": "PREVIEW_CLEANUP_EXECUTED",
            "removed_preview_namespace_candidate_ids": sorted(set(removed)),
            "absent_preview_namespace_candidate_ids": sorted(set(absent)),
        }

    def _observe(self, candidate: PreviewNamespaceCleanupCandidate) -> dict[str, str | bool] | None:
        self._scoped_credential(candidate.kubeconfig_path)
        result = self.runner.run(
            "kubectl",
            [
                "--kubeconfig",
                str(candidate.kubeconfig_path),
                "--context",
                candidate.kube_context,
                "get",
                "namespace",
                candidate.namespace,
                "--ignore-not-found=true",
                "-o",
                "json",
            ],
            cwd=Path("/"),
            timeout_seconds=60,
        )
        if not result.stdout.strip():
            return None
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get("kind") != "Namespace":
            raise ValueError("preview cleanup observation was not a Namespace")
        metadata = value.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("name") != candidate.namespace:
            raise ValueError("preview cleanup namespace identity changed")
        labels = metadata.get("labels", {})
        if not isinstance(labels, dict) or any(
            not isinstance(key, str) or not isinstance(item, str) for key, item in labels.items()
        ):
            raise ValueError("preview cleanup namespace labels are malformed")
        uid = metadata.get("uid")
        resource_version = metadata.get("resourceVersion")
        if not isinstance(uid, str) or not uid or not isinstance(resource_version, str):
            raise ValueError("preview cleanup namespace metadata is incomplete")
        managed = (
            labels.get(self._required_label) == "engineering-control"
            and labels.get(self._deployment_label) == str(candidate.deployment_id)
            and labels.get(self._change_set_label) == str(candidate.change_set_id)
        )
        encoded = json.dumps(labels, separators=(",", ":"), sort_keys=True).encode()
        return {
            "managed": managed,
            "uid": uid,
            "resource_version": resource_version,
            "labels_sha256": hashlib.sha256(encoded).hexdigest(),
        }

    def _scoped_credential(self, path: Path) -> None:
        root = self.settings.release_credential_root.resolve()
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(root):
            raise ValueError("preview cleanup credential is outside the release credential root")
        if resolved.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError("preview cleanup credential grants group or other access")

    @staticmethod
    def _objects(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValueError("preview cleanup authorization must contain objects")
        return value

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("preview cleanup completion identifiers must be strings")
        return value

    def _validate_authorization(self, payload: dict[str, Any]) -> None:
        orchestration = payload.get("orchestration")
        if not isinstance(orchestration, dict):
            raise ValueError("preview cleanup orchestration identity is missing")
        workflow_id = orchestration.get("workflow_id")
        supplied = payload.get("cleanup_authorization_sha256")
        if not isinstance(workflow_id, str) or not isinstance(supplied, str):
            raise ValueError("preview cleanup authorization identity is incomplete")
        expected = cleanup_authorization_sha256(
            workflow_id=workflow_id,
            worktrees=self._objects(payload.get("authorized_candidates")),
            files=self._objects(payload.get("authorized_file_candidates")),
            component_leases=self._objects(payload.get("authorized_component_lease_candidates")),
            preview_namespaces=self._objects(
                payload.get("authorized_preview_namespace_candidates")
            ),
        )
        if expected != supplied:
            raise ValueError("preview cleanup authorization changed after validation")
