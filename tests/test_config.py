from pathlib import Path

import pytest
from pydantic import ValidationError

from engineering_control.config import Settings


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


def test_listeners_are_loopback_only_unless_the_boundary_is_the_cluster() -> None:
    every_interface = "0.0.0.0"  # noqa: S104 - the value under test
    for field, message in (
        ("sandbox_egress_host", "proxy cannot bind to every interface"),
        ("supervisor_tools_host", "tool service cannot bind to every interface"),
        ("dashboard_host", "dashboard must bind only to loopback"),
    ):
        with pytest.raises(ValueError, match=message):
            Settings(**{field: every_interface})
    assert Settings().network_boundary == "loopback"
    # In a pod the listener has to be on the pod interface; network policy decides reach.
    settings = Settings(
        network_boundary="cluster",
        sandbox_egress_host=every_interface,
        supervisor_tools_host=every_interface,
        dashboard_host=every_interface,
    )
    assert settings.sandbox_egress_host == settings.dashboard_host == every_interface
    with pytest.raises(ValueError):
        Settings(network_boundary="public")


def test_github_app_is_named_completely_or_not_at_all() -> None:
    assert Settings().github_app_id is None
    whole = {
        "github_app_id": 5724836,
        "github_app_client_id": "Iv23ctEXJNig6ZErLZsS",
        "github_app_installation_id": 181163547,
    }
    settings = Settings(**whole)
    assert settings.github_app_installation_id == 181163547
    for missing in whole:
        with pytest.raises(ValueError, match="app id, client id and installation id together"):
            Settings(**{key: value for key, value in whole.items() if key != missing})
    for field, value in (
        ("github_app_id", 0),
        ("github_app_installation_id", -1),
        ("github_app_client_id", "ghp_notaclientid"),
        ("github_app_client_id", "Iv1"),
    ):
        with pytest.raises(ValueError):
            Settings(**{**whole, field: value})


def test_the_deployed_configuration_names_the_github_app_and_the_chat_administrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    manifest = Path(__file__).parents[1] / "deploy" / "base" / "configuration.yaml"
    data = yaml.safe_load(manifest.read_text())["data"]
    for name, value in data.items():
        if name.startswith(
            ("ENGINEERING_GITHUB_APP_", "ENGINEERING_TELEGRAM_", "ENGINEERING_NETWORK_BOUNDARY")
        ):
            monkeypatch.setenv(name, value)
    settings = Settings()
    assert settings.telegram_administrator_id and settings.telegram_administrator_id > 0
    assert settings.github_app_id and settings.github_app_installation_id
    assert settings.github_app_client_id
    # Identifiers only: nothing that looks like a key or a secret is in the manifest.
    assert not any("PRIVATE" in value or "secret" in name.lower() for name, value in data.items())


def test_the_chat_administrator_is_one_positive_account_id() -> None:
    assert Settings().telegram_administrator_id is None
    assert Settings(telegram_administrator_id=685699815).telegram_administrator_id == 685699815
    for value in (0, -5, "someone"):
        with pytest.raises(ValueError):
            Settings(telegram_administrator_id=value)
