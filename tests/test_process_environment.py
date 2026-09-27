from __future__ import annotations

from engineering_control.adapters import process
from engineering_control.adapters.process import subscription_only_environment


def test_provider_environment_offers_offline_go_and_trivy(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # NEW-79a: makers and checkers could not run go test or trivy (#1125); the scan
    # worker prefills the caches and the sandbox uses them read-only without network.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    env = subscription_only_environment("codex")

    assert "OPENAI_API_KEY" not in env
    assert env["GOPROXY"] == "off" and env["GOTOOLCHAIN"] == "local"
    assert env["GOMODCACHE"] == str(process.TOOLCHAIN_CACHE_ROOT / "go-mod")
    assert env["TRIVY_SKIP_DB_UPDATE"] == "true" and env["TRIVY_OFFLINE_SCAN"] == "true"
    assert env["TRIVY_CACHE_BACKEND"] == "memory"
    assert env["TRIVY_CACHE_DIR"] == str(process.TOOLCHAIN_CACHE_ROOT / "trivy")
    if process.GO_ROOT_BIN.is_dir():
        assert env["PATH"].split(":")[0] == str(process.GO_ROOT_BIN)
