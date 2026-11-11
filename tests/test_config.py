import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from engineering_control.config import Settings


def test_service_token_is_owner_only_or_a_root_file_shared_with_a_service_group(
    tmp_path: Path,
) -> None:
    token = tmp_path / "service.token"
    token.write_text("s" * 48, encoding="utf-8")
    os.chmod(token, 0o600)
    settings = Settings(api_token_file=token, evidence_ingest_token_file=token)

    assert settings.read_api_token() == "s" * 48
    assert settings.read_evidence_ingest_token() == "s" * 48

    os.chmod(token, 0o660)
    with pytest.raises(PermissionError, match="group write"):
        settings.read_api_token()

    # Group read is how a mounted Secret arrives (root's file, a group of this service).
    os.chmod(token, 0o640)
    if os.geteuid() == 0:
        assert settings.read_api_token() == "s" * 48
    else:
        with pytest.raises(PermissionError, match="group-readable"):
            settings.read_api_token()


def test_agent_pack_root_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        Settings(agent_pack_root=Path("relative-agent-pack"))


def test_dashboard_session_lifetime_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(dashboard_session_hours=0)


def test_dashboard_host_and_origin_allowlists_are_bounded_and_production_safe() -> None:
    settings = Settings()
    assert "ade.musematic.ai" in settings.dashboard_allowed_hosts
    assert "https://ade.musematic.ai" in settings.dashboard_allowed_origins
    assert "testserver" not in settings.dashboard_allowed_hosts
    assert "https://testserver" not in settings.dashboard_allowed_origins

    with pytest.raises(ValidationError):
        Settings(dashboard_allowed_hosts=())
    with pytest.raises(ValidationError):
        Settings(dashboard_allowed_hosts=("attacker.invalid@ade.musematic.ai",))
    with pytest.raises(ValidationError):
        Settings(dashboard_allowed_origins=("https://ade.musematic.ai/path",))


def test_default_browser_runtime_uses_the_immutable_release_link() -> None:
    settings = Settings()

    assert settings.pnpm_executable == Path(
        "/opt/engineering-browser/runtime/node_modules/.bin/pnpm"
    )
    assert settings.browser_script == Path("/opt/engineering-browser/runtime/run-journey.mjs")


def test_evidence_ingest_tokens_include_every_worker_identity(tmp_path: Path) -> None:
    identities = {
        "codex-runner",
        "claude-runner",
        "browser-runner",
        "scan-runner",
        "delivery-runner",
        "release-runner",
        "github-runner",
    }
    for identity in identities:
        token = tmp_path / f"{identity}.token"
        token.write_text("s" * 48, encoding="utf-8")
        os.chmod(token, 0o600)

    settings = Settings(evidence_ingest_token_directory=tmp_path)

    assert settings.read_evidence_ingest_tokens().keys() == identities


def test_listeners_are_loopback_only_unless_the_boundary_is_the_cluster() -> None:
    every_interface = "0.0.0.0"  # noqa: S104 - the value under test
    for field, message in (
        ("api_host", "API cannot bind to every interface"),
        ("evidence_ingest_host", "evidence ingest service cannot bind to every interface"),
        ("dashboard_host", "dashboard must bind only to loopback"),
    ):
        with pytest.raises(ValueError, match=message):
            Settings(**{field: every_interface})
    assert Settings().network_boundary == "loopback"
    # In a pod the listener has to be on the pod interface; network policy decides reach.
    settings = Settings(
        network_boundary="cluster",
        api_host=every_interface,
        evidence_ingest_host=every_interface,
        dashboard_host=every_interface,
    )
    assert settings.api_host == settings.dashboard_host == every_interface
    with pytest.raises(ValueError):
        Settings(network_boundary="public")
