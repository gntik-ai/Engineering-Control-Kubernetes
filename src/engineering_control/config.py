from __future__ import annotations

import os
import stat
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENGINEERING_", env_file=None, extra="forbid", case_sensitive=False
    )

    environment: str = "development"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8740, ge=1024, le=65535)
    api_token_file: Path = Path("/etc/engineering-control/env/api.token")
    temporal_target: str = "127.0.0.1:7233"
    temporal_namespace: str = "engineering-control"
    temporal_task_queue: str = "engineering-control"
    codex_task_queue: str = "engineering-codex"
    claude_task_queue: str = "engineering-claude"
    browser_task_queue: str = "engineering-browser"
    scan_task_queue: str = "engineering-scan"
    release_task_queue: str = "engineering-release"
    delivery_task_queue: str = "engineering-delivery"
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://engineering_control@127.0.0.1/engineering_control"
    )
    evidence_root: Path = Path("/srv/engineering/evidence")
    evidence_ingest_host: str = "10.10.0.2"
    evidence_ingest_port: int = Field(default=8750, ge=1024, le=65535)
    evidence_ingest_url: str | None = None
    evidence_ingest_token_file: Path = Path("/etc/engineering-control/env/evidence-ingest.token")
    evidence_ingest_token_directory: Path = Path("/etc/engineering-control/env/evidence-uploaders")
    evidence_max_bytes: int = Field(default=33_554_432, ge=1024, le=134_217_728)
    mirror_root: Path = Path("/srv/engineering/mirrors")
    worktree_root: Path = Path("/srv/engineering/worktrees")
    worktree_metadata_root: Path = Path("/srv/engineering/cache/scan/worktree-git")
    run_root: Path = Path("/srv/engineering/runs")
    state_root: Path = Path("/var/lib/engineering-control")
    deployment_target_root: Path = Path("/etc/engineering-control/targets")
    runtime_target_root: Path = Path("/etc/engineering-control/runtime")
    temporary_root: Path = Path("/srv/engineering/tmp")
    codex_executable: Path = Path("/usr/local/bin/codex")
    claude_executable: Path = Path("/usr/local/bin/claude")
    helm_executable: Path = Path("/usr/local/bin/helm")
    kubectl_executable: Path = Path("/usr/local/bin/kubectl")
    argocd_executable: Path = Path("/usr/local/bin/argocd")
    release_credential_root: Path = Path("/etc/engineering-control/env/release")
    observer_credential_root: Path = Path("/etc/engineering-control/env/observer")
    node_executable: Path = Path("/usr/local/bin/node")
    browser_script: Path = Path("/opt/engineering-browser/run-journey.mjs")
    journey_schema: Path = Path("/opt/engineering-control/source/schemas/journey-plan.schema.json")
    journey_plan_root: Path = Path("/etc/engineering-control/journeys")
    project_config_root: Path = Path("/etc/engineering-control/projects.d")
    browser_credential_root: Path = Path("/etc/engineering-control/env/browser")
    browser_evidence_root: Path = Path("/srv/engineering/evidence/browser")
    github_executable: Path = Path("/usr/bin/gh")
    github_token_file: Path | None = None
    github_registry_auth_file: Path | None = None
    git_read_token_file: Path | None = None
    git_executable: Path = Path("/usr/bin/git")
    git_read_credential_root: Path = Path("/etc/engineering-control/env/scan")
    delivery_credential_root: Path = Path("/etc/engineering-control/env/delivery")
    git_askpass_executable: Path = Path("/opt/engineering-control/source/scripts/git-askpass.sh")
    gitleaks_executable: Path = Path("/usr/local/bin/gitleaks")
    trivy_executable: Path = Path("/usr/local/bin/trivy")
    semgrep_executable: Path = Path("/usr/local/bin/semgrep")
    syft_executable: Path = Path("/usr/local/bin/syft")
    kubeconform_executable: Path = Path("/usr/local/bin/kubeconform")
    acceptance_root: Path = Path("/srv/engineering/tmp/acceptance")
    acceptance_enabled: bool = False
    lease_root: Path = Path("/var/lib/engineering-control/leases")
    log_level: str = "INFO"
    synthetic_mode: bool = False
    worker_kind: str = "controller"
    notification_sink_url: str | None = None
    notification_token_file: Path | None = None
    notification_poll_seconds: int = Field(default=10, ge=2, le=300)
    cleanup_execution_enabled: bool = False
    cleanup_retention_days: int = Field(default=14, ge=1, le=3650)
    cleanup_expiry_grace_hours: int = Field(default=24, ge=1, le=720)
    worker_heartbeat_seconds: int = Field(default=30, ge=10, le=300)

    @field_validator("api_host")
    @classmethod
    def private_api_only(cls, value: str) -> str:
        if value in {"0.0.0.0", "::"}:  # noqa: S104 - these values are explicitly rejected
            raise ValueError("the Engineering Control API cannot bind to every interface")
        return value

    @field_validator("evidence_ingest_host")
    @classmethod
    def private_evidence_ingest_only(cls, value: str) -> str:
        if value in {"0.0.0.0", "::"}:  # noqa: S104 - these values are explicitly rejected
            raise ValueError("the evidence ingest service cannot bind to every interface")
        return value

    def read_api_token(self) -> str:
        return self._read_service_token(self.api_token_file, "API")

    def read_evidence_ingest_token(self) -> str:
        return self._read_service_token(self.evidence_ingest_token_file, "evidence ingest")

    def read_evidence_ingest_tokens(self) -> dict[str, str]:
        identities = (
            "codex-runner",
            "claude-runner",
            "browser-runner",
            "scan-runner",
            "delivery-runner",
            "release-runner",
        )
        return {
            identity: self._read_service_token(
                self.evidence_ingest_token_directory / f"{identity}.token",
                f"{identity} evidence ingest",
            )
            for identity in identities
        }

    @staticmethod
    def _read_service_token(path: Path, label: str) -> str:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} token path must be a regular file")
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IXGRP | stat.S_IRWXO):
            raise PermissionError(
                f"{label} token file may grant group read but not group write/execute "
                "or other access"
            )
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ValueError(f"{label} token must contain at least 32 characters")
        return token


@lru_cache
def get_settings() -> Settings:
    return Settings()


def service_environment() -> dict[str, str]:
    """Return only non-secret process metadata suitable for structured logging."""
    return {
        "environment": os.getenv("ENGINEERING_ENVIRONMENT", "development"),
        "hostname": os.uname().nodename,
    }
