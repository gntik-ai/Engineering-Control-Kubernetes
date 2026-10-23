from __future__ import annotations

import os
import re
import stat
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from engineering_control.policy.github_write import GITHUB_WRITE_POLICY_PATH
from engineering_control.workflows.worker_registry import (
    CONTROL_TASK_QUEUE,
    ROLE_WORKER_REGISTRY,
    WorkerKind,
)

# NEW-163: how many projects may each hold one backlog campaign's GitHub write policy
# ownership at the same time. 1 is the pre-NEW-163 single global owner rule. The API
# (approval) and the root reconciler (scripts/reconcile-backlog-github-policy.py, same
# control.env) both read Settings.backlog_parallel_project_limit.
DEFAULT_BACKLOG_PARALLEL_PROJECT_LIMIT = 2


def _default_toolchain_node_roots() -> dict[Literal["22", "24"], Path]:
    return {"22": Path("/usr/local"), "24": Path("/opt/engineering-toolchain/node-24")}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENGINEERING_", env_file=None, extra="forbid", case_sensitive=False
    )

    environment: str = "development"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8740, ge=1024, le=65535)
    api_admission_mode: Literal["normal", "synthetic-only", "diagnostics-only"] = "normal"
    projection_reconciliation_enabled: bool = False
    projection_reconciliation_interval_seconds: int = Field(default=30, ge=5, le=300)
    projection_reconciliation_batch_size: int = Field(default=25, ge=1, le=100)
    projection_reconciliation_cycle_seconds: int = Field(default=30, ge=10, le=120)
    projection_reconciliation_max_sweep_age_seconds: int = Field(default=300, ge=30, le=3600)
    api_token_file: Path = Path("/etc/engineering-control/env/api.token")
    temporal_target: str = "127.0.0.1:7233"
    temporal_namespace: str = "engineering-control"
    temporal_task_queue: str = CONTROL_TASK_QUEUE
    codex_task_queue: str = ROLE_WORKER_REGISTRY["codex"].task_queue
    claude_task_queue: str = ROLE_WORKER_REGISTRY["claude"].task_queue
    browser_task_queue: str = ROLE_WORKER_REGISTRY["browser"].task_queue
    scan_task_queue: str = ROLE_WORKER_REGISTRY["scan"].task_queue
    release_task_queue: str = ROLE_WORKER_REGISTRY["release"].task_queue
    delivery_task_queue: str = ROLE_WORKER_REGISTRY["delivery"].task_queue
    github_task_queue: str = ROLE_WORKER_REGISTRY["github"].task_queue
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
    # NEW-226: provider runs get their own TMPDIR under temporary_root/<provider>; this
    # sweep is the backstop for the role worker's private /tmp (PrivateTmp, a tmpfs held
    # in RAM). On start and every interval it removes entries nobody wrote for the idle
    # time and none a running step wrote; node-compile-cache stays.
    private_tmp_sweep_idle_minutes: int = Field(default=90, ge=15, le=1440)
    private_tmp_sweep_interval_seconds: int = Field(default=900, ge=60, le=86400)
    # NEW-226: one provider run's cgroup memory.max (MiB). Unset: the worker's MemoryHigh
    # less 1 GiB (codex 6G, claude 5G), so a runaway command is OOM-killed in its own run.
    agent_run_memory_max_mib: int | None = Field(default=None, ge=512, le=65536)
    codex_executable: Path = Path("/usr/local/bin/codex")
    # NEW-36: pin the Codex model instead of inheriting the CLI default, which changed
    # under us (codex-cli 0.149.0 defaulted to gpt-5.4, refused for the ChatGPT account).
    # Operator choice 2026-09-23 (gpt-5.6-terra); 2026-09-29: gpt-6-sol; 2026-10-01:
    # gpt-6.1-sol, the "latest workhorse model for coding and everyday work" (gpt-6-sol
    # is now "previous generation"; codex-cli >= 0.159 lists it). Override with
    # ENGINEERING_CODEX_MODEL.
    codex_model: str | None = "gpt-6.1-sol"
    claude_executable: Path = Path("/usr/local/bin/claude")
    # NEW-53: optional Claude model pins for the delivery spec architect and the
    # independent checker (passed as --model). None keeps the CLI default.
    # NEW-53 (operator 2026-09-25): Opus 5.5 for the Claude spec and checker roles. Needs
    # Claude Code >= 2.1.280 on the worker (upgraded to 2.1.282 on 2026-09-25).
    claude_spec_model: str | None = "claude-opus-5-5"
    claude_checker_model: str | None = "claude-opus-5-5"
    # NEW-123: at most this many UI blueprint items become change-request drafts per fan-out
    # run; an approval replay resumes with the next items (idempotent, deterministic ids).
    ui_blueprint_fanout_max_items: int = Field(default=80, ge=1, le=600)
    # NEW-234 PR6: at most this many backlog decomposition tasks become change requests per
    # fan-out run (design section 4: 130-200 expected, default 250, hard limit 400).
    backlog_decomposition_fanout_max_items: int = Field(default=250, ge=1, le=400)
    # NEW-131: pages per drafted blueprint batch (the frame is drafted first, alone).
    ui_plan_batch_pages: int = Field(default=8, ge=1, le=25)
    helm_executable: Path = Path("/usr/local/bin/helm")
    kubectl_executable: Path = Path("/usr/local/bin/kubectl")
    staging_kubeconfig_path: Path = Path("/etc/engineering-control/credentials/staging/kubeconfig")
    argocd_executable: Path = Path("/usr/local/bin/argocd")
    release_credential_root: Path = Path("/etc/engineering-control/env/release")
    observer_credential_root: Path = Path("/etc/engineering-control/credentials")
    node_executable: Path = Path("/usr/local/bin/node")
    # 2b678d9 (31ee08e): the browser runtime is versioned per release behind runtime/.
    pnpm_executable: Path = Path("/opt/engineering-browser/runtime/node_modules/.bin/pnpm")
    # Delivery toolchain preparation (scan worker, delivery.prepare_toolchain). The
    # browser worker's pnpm above is a different, pinned consumer-suite install.
    uv_executable: Path = Path("/usr/local/bin/uv")
    toolchain_pnpm_executable: Path = Path("/usr/local/bin/pnpm")
    # NEW-159: side-by-side Node.js installs a project manifest selects by major
    # (toolchain.node, default "22" = today's /usr/local/bin/node, never changed), and
    # standalone pnpm executables by pinned version (toolchain.pnpm, default None = the
    # executable above). scripts/bootstrap-worker.sh --node-toolchain installs both.
    toolchain_node_roots: dict[Literal["22", "24"], Path] = Field(
        default_factory=_default_toolchain_node_roots
    )
    toolchain_pnpm_executables: dict[str, Path] = Field(
        default_factory=lambda: {
            "11.28.3": Path("/opt/engineering-toolchain/pnpm-11.28.3/pnpm"),
        }
    )
    # Side-by-side pinned Rust releases (toolchain.rust: <root>/bin/{cargo,rustc,rustfmt,
    # cargo-clippy}) and Go release lines (toolchain.go: <root>/bin/{go,gofmt}). No
    # project selects either by default; scripts/bootstrap-worker.sh
    # --project-toolchains[-only] installs them, pinned and checksum-verified.
    toolchain_rust_roots: dict[str, Path] = Field(
        default_factory=lambda: {"1.99.1": Path("/opt/engineering-toolchain/rust-1.99.1")}
    )
    toolchain_go_roots: dict[str, Path] = Field(
        default_factory=lambda: {"1.26": Path("/opt/engineering-toolchain/go-1.26")}
    )
    # NEW-75: makers are offline; delivery.resolve_dependencies (scan worker, network)
    # regenerates go.sum / pnpm-lock.yaml after the maker edited only the manifests.
    go_executable: Path = Path("/usr/local/go/bin/go")
    python312_executable: Path = Path("/usr/local/bin/python3.12")
    # NEW-49: contract test commands are wrapped in coreutils timeout (NEW-46).
    timeout_executable: Path = Path("/usr/bin/timeout")
    # NEW-161: a maker-chosen `node --test` suite is rerun at the BASE only, in a
    # bubblewrap sandbox (no network, read-only system and checkout, size-capped /tmp);
    # its paths must start with one of these prefixes (relative to the command's cwd).
    bwrap_executable: Path = Path("/usr/bin/bwrap")
    # NEW-228: the root-owned CLAUDE_CODE_SHELL_PREFIX of loopback-enabled Claude runs.
    agent_loopback_shell: Path = Path(
        "/opt/engineering-control/source/scripts/agent-loopback-shell"
    )
    node_test_path_prefixes: tuple[str, ...] = ("tests/", "test/")
    # Must sit under the scan worker unit's ReadWritePaths (/srv/engineering/cache/%i).
    toolchain_cache_root: Path = Path("/srv/engineering/cache/scan/toolchain")
    # NEW-52: a retry/correction of a ChangeSet keeps the previous attempt's commits
    # (when they descend from the base) instead of re-implementing from the base.
    delivery_resume_prior_commits: bool = True
    # NEW-53: a retry/correction of a ChangeSet reuses the OpenSpec created by an earlier
    # attempt when the issue content it was generated from is unchanged, instead of
    # re-running the spec architect (whose structured output fails ~13% of runs).
    delivery_reuse_spec: bool = True
    browser_script: Path = Path("/opt/engineering-browser/runtime/run-journey.mjs")
    journey_schema: Path = Path("/opt/engineering-control/source/schemas/journey-plan.schema.json")
    journey_plan_root: Path = Path("/etc/engineering-control/journeys")
    project_config_root: Path = Path("/etc/engineering-control/projects.d")
    persona_config_root: Path = Path("/etc/engineering-control/personas.d")
    flow_config_root: Path = Path("/etc/engineering-control/flows.d")
    credential_root: Path = Path("/etc/engineering-control/credentials")
    credential_attestation_root: Path = Path("/var/lib/engineering-control/attestations")
    browser_worker_identity: str = "browser-runner@hermes-worker"
    scan_worker_identity: str = "scan-runner@hermes-worker"
    engineering_profile_manifest: Path = Path(
        "/opt/engineering-agent-pack/source/profiles/engineering.yaml"
    )
    agent_pack_root: Path = Path("/opt/engineering-agent-pack/source")
    browser_credential_root: Path = Path("/etc/engineering-control/credentials")
    browser_evidence_root: Path = Path("/srv/engineering/evidence/browser")
    github_executable: Path = Path("/usr/bin/gh")
    github_authenticated_home: Path | None = None
    github_writes_enabled: bool = False
    llmwiki_staging_bootstrap_enabled: bool = False
    application_data_mutation_enabled: bool = False
    github_issue_write_validation_file: Path = GITHUB_WRITE_POLICY_PATH
    backlog_parallel_project_limit: int = Field(
        default=DEFAULT_BACKLOG_PARALLEL_PROJECT_LIMIT, ge=1, le=16
    )
    github_registry_auth_file: Path | None = None
    # NEW-61: ChangeSet worktrees live on the worker host while every GitHub write stays
    # on the controller. delivery.publish fetches the exact validated branch from the
    # worker's ChangeSet Git metadata over SSH (host alias below, resolved by the
    # transport identity's ~/.ssh/config) into a controller-local scratch repository and
    # pushes from there. The worker's worktree manager grants this identity read access
    # to that metadata repository only. Disable to publish from a local worktree.
    publication_fetch_from_worker: bool = True
    publication_transport_host: str = Field(
        default="worker", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$"
    )
    publication_transport_identity: str = Field(
        default="hermes", pattern=r"^[a-z_][a-z0-9_-]{0,31}$"
    )
    # NEW-71: the worker's canonical mirrors (worktree bases) are never fetched there
    # (no GitHub credentials on the worker); github.refresh_mirrors pushes the refreshed
    # controller mirror to the same path on the worker over the transport above.
    mirror_sync_to_worker: bool = True
    git_executable: Path = Path("/usr/bin/git")
    delivery_credential_root: Path = Path("/etc/engineering-control/env/delivery")
    gitleaks_executable: Path = Path("/usr/local/bin/gitleaks")
    trivy_executable: Path = Path("/usr/local/bin/trivy")
    # NEW-226: the scan worker's own Trivy cache (vulnerability DB; scan results stay in
    # memory). Without HOME, trivy fell back to os.TempDir()/trivy: 1.6 GB in the unit's
    # PrivateTmp tmpfs, i.e. RAM. Under the scan unit's ReadWritePaths
    # (/srv/engineering/cache/%i); never the makers' read-only toolchain/trivy.
    trivy_cache_root: Path = Path("/srv/engineering/cache/scan/trivy")
    semgrep_executable: Path = Path("/usr/local/bin/semgrep")
    syft_executable: Path = Path("/usr/local/bin/syft")
    kubeconform_executable: Path = Path("/usr/local/bin/kubeconform")
    acceptance_root: Path = Path("/srv/engineering/tmp/acceptance")
    acceptance_enabled: bool = False
    lease_root: Path = Path("/var/lib/engineering-control/leases")
    log_level: str = "INFO"
    synthetic_mode: bool = False
    worker_kind: WorkerKind = "controller"
    build_id: str | None = None
    notification_sink_url: str | None = None
    notification_token_file: Path | None = None
    # NEW-112a: the Hermes gateway webhook (loopback :8644) authenticates HMAC-SHA256 V2
    # signatures, not bearer tokens. Restored from the 2026-09-16 release (2b678d9).
    notification_auth_mode: Literal["bearer", "hmac-sha256-v2"] = "hmac-sha256-v2"
    notification_poll_seconds: int = Field(default=10, ge=2, le=300)
    watchdog_state_file: Path = Path("/var/lib/engineering-watchdog/state.json")
    watchdog_backup_status_file: Path = Path("/var/lib/engineering-backup/status.json")
    watchdog_probe_seconds: int = Field(default=60, ge=30, le=120)
    watchdog_repeat_seconds: int = Field(default=3900, ge=300, le=86400)
    watchdog_backup_max_age_seconds: int = Field(default=3600, ge=3600, le=86400)
    watchdog_restore_max_age_seconds: int = Field(default=691200, ge=86400, le=2678400)
    watchdog_cleanup_max_age_seconds: int = Field(default=93600, ge=3600, le=172800)
    watchdog_cleanup_running_max_age_seconds: int = Field(default=3600, ge=300, le=86400)
    watchdog_heartbeat_max_age_seconds: int = Field(default=180, ge=60, le=900)
    watchdog_notification_max_age_seconds: int = Field(default=900, ge=60, le=86400)
    watchdog_gateway_required: bool = False
    cleanup_execution_enabled: bool = False
    cleanup_retention_days: int = Field(default=14, ge=1, le=3650)
    cleanup_expiry_grace_hours: int = Field(default=24, ge=1, le=720)
    # NEW-214 Stage 2 (operator 2026-10-18): implementation leases one project may hold at
    # once. One is always left for the BACK slot of a pipelined loop.
    implementation_leases_per_project: int = Field(default=3, ge=1, le=4)
    # NEW-226: the activities (makers, checkers, specs) each provider worker runs at once,
    # per deployment: the NEW-214 Stage 2 FRONT capacity is bounded by the smaller one.
    # The agent workers' memory drop-ins are sized for 2, so it may only be lowered (1
    # while the worker host is short of RAM). Set it where both read it: the worker's env
    # (the worker runs that many) and control.env (FRONT capacity).
    codex_worker_slots: int = Field(
        default=ROLE_WORKER_REGISTRY["codex"].max_concurrent_activities, ge=1, le=2
    )
    claude_worker_slots: int = Field(
        default=ROLE_WORKER_REGISTRY["claude"].max_concurrent_activities, ge=1, le=2
    )
    worker_heartbeat_seconds: int = Field(default=30, ge=10, le=300)
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(default=8750, ge=1024, le=65535)
    dashboard_allowed_hosts: tuple[str, ...] = (
        "ade.musematic.ai",
        "127.0.0.1",
        "localhost",
        "::1",
    )
    dashboard_allowed_origins: tuple[str, ...] = (
        "https://ade.musematic.ai",
        "http://127.0.0.1:8750",
        "http://localhost:8750",
    )
    dashboard_session_key_file: Path = Path("/etc/engineering-control/dashboard/session.key")
    dashboard_session_hours: int = Field(default=12, ge=1, le=24)
    dashboard_event_retention_days: int = Field(default=30, ge=1, le=365)
    dashboard_agent_log_retention_days: int = Field(default=7, ge=1, le=90)
    dashboard_agent_execution_retention_days: int = Field(default=90, ge=7, le=730)
    dashboard_operation_export_retention_days: int = Field(default=7, ge=1, le=30)
    dashboard_operation_export_max_bytes: int = Field(
        default=1024 * 1024, ge=65_536, le=5 * 1024 * 1024
    )
    dashboard_transcript_max_bytes: int = Field(default=5 * 1024 * 1024, ge=65_536, le=20_971_520)
    dashboard_transcript_line_max_bytes: int = Field(default=16 * 1024, ge=1024, le=65_536)
    dashboard_transcript_chunk_bytes: int = Field(default=64 * 1024, ge=4096, le=262_144)

    @field_validator(
        "codex_task_queue",
        "claude_task_queue",
        "browser_task_queue",
        "scan_task_queue",
        "release_task_queue",
        "delivery_task_queue",
        "github_task_queue",
    )
    @classmethod
    def canonical_role_task_queue(cls, value: str, info: ValidationInfo) -> str:
        field_name = info.field_name
        if field_name is None:
            raise ValueError("canonical task queue validation requires a field name")
        kind = field_name.removesuffix("_task_queue")
        expected = ROLE_WORKER_REGISTRY[kind].task_queue  # type: ignore[index]
        if value != expected:
            raise ValueError(f"{field_name} must remain the canonical queue {expected}")
        return value

    @field_validator("api_host")
    @classmethod
    def private_api_only(cls, value: str) -> str:
        if value in {"0.0.0.0", "::"}:  # noqa: S104 - these values are explicitly rejected
            raise ValueError("the Engineering Control API cannot bind to every interface")
        return value

    @field_validator("dashboard_host")
    @classmethod
    def private_dashboard_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("the dashboard must bind only to loopback")
        return value

    @field_validator("dashboard_allowed_hosts")
    @classmethod
    def bounded_dashboard_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.strip().lower() for item in value if item.strip()))
        if not normalized or len(normalized) > 16:
            raise ValueError("dashboard allowed hosts must contain between 1 and 16 entries")
        if any("/" in item or "@" in item or len(item) > 253 for item in normalized):
            raise ValueError("dashboard allowed host is invalid")
        return normalized

    @field_validator("dashboard_allowed_origins")
    @classmethod
    def bounded_dashboard_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            dict.fromkeys(item.strip().lower().rstrip("/") for item in value if item.strip())
        )
        if not normalized or len(normalized) > 16:
            raise ValueError("dashboard allowed origins must contain between 1 and 16 entries")
        if any(
            not item.startswith(("https://", "http://"))
            or "/" in item.split("://", 1)[1]
            or "@" in item
            or len(item) > 300
            for item in normalized
        ):
            raise ValueError("dashboard allowed origin is invalid")
        return normalized

    @field_validator("evidence_ingest_host")
    @classmethod
    def private_evidence_ingest_only(cls, value: str) -> str:
        if value in {"0.0.0.0", "::"}:  # noqa: S104 - these values are explicitly rejected
            raise ValueError("the evidence ingest service cannot bind to every interface")
        return value

    @field_validator("toolchain_node_roots")
    @classmethod
    def absolute_node_roots(
        cls, value: dict[Literal["22", "24"], Path]
    ) -> dict[Literal["22", "24"], Path]:
        if any(not path.is_absolute() for path in value.values()):
            raise ValueError("toolchain node roots must be absolute paths")
        return value

    @field_validator("toolchain_rust_roots", "toolchain_go_roots")
    @classmethod
    def pinned_language_roots(cls, value: dict[str, Path], info: ValidationInfo) -> dict[str, Path]:
        pattern = (
            r"1\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})"
            if info.field_name == "toolchain_rust_roots"
            else r"1\.(?:0|[1-9][0-9]{0,3})"
        )
        for version, path in value.items():
            if not re.fullmatch(pattern, version):
                raise ValueError(f"{info.field_name} keys must be pinned versions")
            if not path.is_absolute():
                raise ValueError(f"{info.field_name} must be absolute paths")
        return value

    @field_validator("node_test_path_prefixes")
    @classmethod
    def plain_node_test_path_prefixes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        segment = r"[A-Za-z0-9_@+][A-Za-z0-9._@+-]*"
        if not value or any(
            not re.fullmatch(rf"(?:{segment}/)+", prefix)
            or any(part in {".", ".."} for part in prefix.split("/"))
            for prefix in value
        ):
            raise ValueError("node test path prefixes must be relative directories ending in /")
        return value

    @field_validator("toolchain_pnpm_executables")
    @classmethod
    def pinned_pnpm_executables(cls, value: dict[str, Path]) -> dict[str, Path]:
        for version, path in value.items():
            if not re.fullmatch(r"(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){2}", version):
                raise ValueError("toolchain pnpm versions must be exact MAJOR.MINOR.PATCH")
            if not path.is_absolute() or path.name != "pnpm":
                raise ValueError("toolchain pnpm executables must be absolute .../pnpm paths")
        return value

    @field_validator("agent_pack_root")
    @classmethod
    def absolute_agent_pack_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("agent pack root must be an absolute path")
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
            "github-runner",
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
