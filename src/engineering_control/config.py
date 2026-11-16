"""What every process of the platform is told from outside: environment variables
(``ENGINEERING_...``) for what may be shown, mounted files for what may not."""

from __future__ import annotations

import ipaddress
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENGINEERING_", env_file=None, extra="forbid", case_sensitive=False
    )

    environment: str = "development"
    # How the listeners are kept private. "loopback": this machine's own interfaces, as on
    # a developer's machine. "cluster": a Kubernetes pod, where a listener has to be on the
    # pod interface to be reachable by its Service and probes, and who may reach it is
    # decided by network policy, not by the bind address.
    network_boundary: Literal["loopback", "cluster"] = "loopback"
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://engineering_control@127.0.0.1/engineering_control"
    )
    # The GitHub App the platform acts through. These three identify it and are not
    # secret; its private key and client secret are mounted secrets, never settings.
    github_app_id: int | None = Field(default=None, ge=1)
    github_app_client_id: str | None = Field(default=None, pattern=r"^Iv[0-9A-Za-z]{10,40}$")
    github_app_installation_id: int | None = Field(default=None, ge=1)
    github_app_private_key_file: Path | None = None
    # How often the console brings every project's backlog in step with GitHub by itself.
    # Zero leaves it to the administrator's "Sync" button.
    backlog_sync_minutes: int = Field(default=15, ge=0, le=1440)
    # The one Telegram account the chat channel answers: the administrator's numeric user
    # id. The bot's token is a mounted secret, never a setting.
    telegram_administrator_id: int | None = Field(default=None, ge=1)
    telegram_token_file: Path | None = None
    telegram_api_url: str = Field(
        default="https://api.telegram.org", pattern=r"^https://[a-z0-9.-]{3,200}$"
    )
    # Where the console is reached from outside, for the links in chat messages; and the
    # hour of the day (UTC) after which the chat channel sends the daily digest.
    console_public_url: str | None = Field(default=None, pattern=r"^https://[a-z0-9.-]{3,200}$")
    chat_digest_hour: int = Field(default=7, ge=0, le=23)
    # The namespace the sandboxes run in. Without it the platform acts on no cluster:
    # accounts can be listed but not signed in, and nothing can be run.
    sandbox_namespace: str | None = Field(
        default=None, pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
    )
    # The image a sandbox runs, pinned to a commit, and the Secret it is pulled with.
    sandbox_image: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9./-]{2,200}(:[A-Za-z0-9._-]{1,128}|@sha256:[0-9a-f]{64})$",
    )
    # The way out for sandboxes: the proxy's listener, where sandboxes find it, and the
    # hosts it opens tunnels to. A leading "*." names every host under a domain.
    sandbox_egress_host: str = "127.0.0.1"
    sandbox_egress_port: int = Field(default=8888, ge=1024, le=65535)
    sandbox_egress_proxy: str | None = Field(
        default=None, pattern=r"^http://[a-z0-9]([a-z0-9.-]{0,200}[a-z0-9])?:\d{2,5}$"
    )
    # The key the launcher signs, and the proxy checks, a step's grant to reach a project's
    # own application with. Without the file no such grant is given or honoured.
    sandbox_egress_grant_key_file: Path | None = Path(
        "/etc/engineering-control/env/egress-grant.key"
    )
    # The key API keys are sealed with in the database: the console seals, the launcher
    # opens. Without the file no API key can be stored and no model asked over an API.
    model_keys_key_file: Path | None = Path("/etc/engineering-control/env/model-keys.key")
    sandbox_egress_hosts: tuple[str, ...] = (
        # The subscriptions' clients.
        "api.anthropic.com",
        "chatgpt.com",
        "api.openai.com",
        "auth.openai.com",
        # Checking a repository out, and what its dependencies are fetched from.
        "github.com",
        "codeload.github.com",
        "objects.githubusercontent.com",
        "raw.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "registry.npmjs.org",
        "pypi.org",
        "files.pythonhosted.org",
        "crates.io",
        "static.crates.io",
        "index.crates.io",
        "proxy.golang.org",
        "sum.golang.org",
        # Where Go's module proxy hands out the modules it does not serve itself (it
        # answers a request for a large module with a redirect there).
        "storage.googleapis.com",
    )
    # The supervisor's tools: the listener of the service that serves them, and where the
    # launcher tells a turn of the supervisor to find it.
    supervisor_tools_host: str = "127.0.0.1"
    supervisor_tools_port: int = Field(default=8790, ge=1024, le=65535)
    supervisor_tools_url: str | None = Field(
        default=None, pattern=r"^http://[a-z0-9]([a-z0-9.-]{0,200}[a-z0-9])?:\d{2,5}$"
    )
    # The gateway (deploy/base/gateway.yaml): where it listens, and where the launcher
    # tells a client that is paid by an API key to find it, by address.
    gateway_host: str = "127.0.0.1"
    gateway_port: int = Field(default=8794, ge=1024, le=65535)
    gateway_url: str | None = Field(
        default=None, pattern=r"^http://[a-z0-9]([a-z0-9.-]{0,200}[a-z0-9])?:\d{2,5}$"
    )
    # How many steps may run in sandboxes at the same time, over all runs.
    sandbox_parallel_steps: int = Field(default=4, ge=1, le=32)
    sandbox_pull_secret: str = Field(
        default="engineering-control-registry", pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
    )
    # The cluster's own API, and the identity and certificate authority the kubelet mounts.
    cluster_api_server: str = Field(
        default="https://kubernetes.default.svc", pattern=r"^https://[A-Za-z0-9.-]+(:\d{1,5})?$"
    )
    cluster_token_file: Path = Path("/var/run/secrets/engineering-control-cluster/token")
    cluster_ca_file: Path = Path("/var/run/secrets/engineering-control-cluster/ca.crt")
    log_level: str = "INFO"
    # Where a process says how it is doing, to the cluster alone (health.py): whether its
    # loops still turn, and its numbers. In no Service and behind no route.
    health_host: str = "127.0.0.1"
    health_port: int = Field(default=8799, ge=1024, le=65535)
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
    # In a cluster, the networks the ingress controller connects from. Only connections
    # from them (and from loopback) may say which client address they forward.
    dashboard_trusted_proxy_networks: tuple[str, ...] = ()
    dashboard_session_key_file: Path = Path("/etc/engineering-control/dashboard/session.key")
    dashboard_session_hours: int = Field(default=12, ge=1, le=24)
    # How long the record of what was done in the console is kept.
    dashboard_event_retention_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="after")
    def whole_github_app(self) -> Settings:
        """The app is named completely or not at all."""
        named = (self.github_app_id, self.github_app_client_id, self.github_app_installation_id)
        if any(value is not None for value in named) and None in named:
            raise ValueError(
                "the GitHub App needs its app id, client id and installation id together"
            )
        return self

    @model_validator(mode="after")
    def private_listeners(self) -> Settings:
        """Outside a cluster, no listener may be reachable from every interface."""
        if self.network_boundary == "cluster":
            return self
        if self.dashboard_trusted_proxy_networks:
            raise ValueError("trusted proxy networks are only meaningful inside a cluster")
        every_interface = {"0.0.0.0", "::"}  # noqa: S104 - these values are explicitly rejected
        if self.dashboard_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("the dashboard must bind only to loopback")
        if self.sandbox_egress_host in every_interface:
            raise ValueError("the sandboxes' proxy cannot bind to every interface")
        if self.supervisor_tools_host in every_interface:
            raise ValueError("the supervisor's tool service cannot bind to every interface")
        if self.gateway_host in every_interface:
            raise ValueError("the gateway cannot bind to every interface")
        if self.health_host in every_interface:
            raise ValueError("the health listener cannot bind to every interface")
        return self

    @field_validator("dashboard_allowed_hosts")
    @classmethod
    def bounded_dashboard_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.strip().lower() for item in value if item.strip()))
        if not normalized or len(normalized) > 16:
            raise ValueError("dashboard allowed hosts must contain between 1 and 16 entries")
        if any("/" in item or "@" in item or len(item) > 253 for item in normalized):
            raise ValueError("dashboard allowed host is invalid")
        return normalized

    @field_validator("sandbox_egress_hosts")
    @classmethod
    def named_egress_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        hosts = tuple(dict.fromkeys(item.strip().lower() for item in value if item.strip()))
        if len(hosts) > 100:
            raise ValueError("at most 100 hosts may be named for the sandboxes' way out")
        for host in hosts:
            if not re.fullmatch(r"(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host):
                raise ValueError(f"not a host name or a *.domain: {host}")
            if host.startswith("*.") and host.count(".") < 2:
                raise ValueError(f"too wide: {host} would name a whole top-level domain")
        return hosts

    @field_validator("dashboard_trusted_proxy_networks")
    @classmethod
    def bounded_proxy_networks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 8:
            raise ValueError("at most 8 trusted proxy networks may be named")
        for item in value:
            network = ipaddress.ip_network(item)  # raises ValueError for anything else
            if network.prefixlen == 0:
                raise ValueError("a trusted proxy network cannot be every address")
        return value

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


def service_environment() -> dict[str, str]:
    """Return only non-secret process metadata suitable for structured logging."""
    return {
        "environment": os.getenv("ENGINEERING_ENVIRONMENT", "development"),
        "hostname": os.uname().nodename,
    }
