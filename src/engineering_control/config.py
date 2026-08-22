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
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://engineering_control@127.0.0.1/engineering_control"
    )
    evidence_root: Path = Path("/srv/engineering/evidence")
    mirror_root: Path = Path("/srv/engineering/mirrors")
    worktree_root: Path = Path("/srv/engineering/worktrees")
    run_root: Path = Path("/srv/engineering/runs")
    temporary_root: Path = Path("/srv/engineering/tmp")
    codex_executable: Path = Path("/usr/local/bin/codex")
    claude_executable: Path = Path("/usr/local/bin/claude")
    helm_executable: Path = Path("/usr/local/bin/helm")
    argocd_executable: Path = Path("/usr/local/bin/argocd")
    release_credential_root: Path = Path("/etc/engineering-control/env/release")
    node_executable: Path = Path("/usr/local/bin/node")
    browser_script: Path = Path("/opt/engineering-browser/run-journey.mjs")
    journey_schema: Path = Path("/opt/engineering-control/source/schemas/journey-plan.schema.json")
    journey_plan_root: Path = Path("/etc/engineering-control/journeys")
    browser_credential_root: Path = Path("/etc/engineering-control/env/browser")
    browser_evidence_root: Path = Path("/srv/engineering/evidence/browser")
    github_executable: Path = Path("/usr/bin/gh")
    github_token_file: Path | None = None
    acceptance_root: Path = Path("/srv/engineering/tmp/acceptance")
    lease_root: Path = Path("/var/lib/engineering-control/leases")
    log_level: str = "INFO"
    synthetic_mode: bool = False
    worker_kind: str = "controller"
    notification_sink_url: str | None = None
    notification_token_file: Path | None = None
    notification_poll_seconds: int = Field(default=10, ge=2, le=300)

    @field_validator("api_host")
    @classmethod
    def private_api_only(cls, value: str) -> str:
        if value in {"0.0.0.0", "::"}:  # noqa: S104 - these values are explicitly rejected
            raise ValueError("the Engineering Control API cannot bind to every interface")
        return value

    def read_api_token(self) -> str:
        path = self.api_token_file
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("API token path must be a regular file")
        if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError("API token file must not grant group or other access")
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ValueError("API token must contain at least 32 characters")
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
