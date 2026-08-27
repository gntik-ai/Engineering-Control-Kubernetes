from __future__ import annotations

import importlib.util
import json
import os
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _module() -> ModuleType:
    path = Path("scripts/hetzner-dashboard-dns.py")
    spec = importlib.util.spec_from_file_location("hetzner_dashboard_dns", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(
        self,
        *,
        a: dict[str, Any] | None = None,
        aaaa: dict[str, Any] | None = None,
        cname: dict[str, Any] | None = None,
    ) -> None:
        self.zone = {
            "id": 42,
            "name": "musematic.ai",
            "mode": "primary",
            "status": "verified",
            "protection": {"delete": False},
            "authoritative_nameservers": {
                "assigned": ["hydrogen.ns.hetzner.com", "oxygen.ns.hetzner.com"],
                "delegated": ["hydrogen.ns.hetzner.com", "oxygen.ns.hetzner.com"],
                "delegation_status": "valid",
            },
        }
        self.records = {"A": a, "AAAA": aaaa, "CNAME": cname}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("/zones?"):
            return 200, {"zones": [self.zone]}
        if method == "GET" and "/rrsets/ade/" in path:
            record_type = path.rsplit("/", 1)[-1]
            value = self.records[record_type]
            return (200, {"rrset": value}) if value is not None else (404, {})
        if method == "POST" and path == "/zones/42/rrsets":
            assert payload is not None
            self.records["A"] = {
                "name": "ade",
                "type": "A",
                "ttl": payload["ttl"],
                "records": payload["records"],
                "protection": {"change": False},
            }
            return 201, {"action": {"id": 8}}
        if method == "POST" and path.endswith("/actions/change_ttl"):
            assert payload is not None and self.records["A"] is not None
            self.records["A"]["ttl"] = payload["ttl"]
            return 200, {"action": {"id": 9}}
        if method == "POST" and path.endswith("/actions/set_records"):
            assert payload is not None and self.records["A"] is not None
            self.records["A"]["records"] = payload["records"]
            return 200, {"action": {"id": 10}}
        if method == "GET" and "/actions/" in path:
            return 200, {"action": {"status": "success"}}
        raise AssertionError((method, path, payload))


def _rrset(value: str, *, ttl: int = 300, protected: bool = False) -> dict[str, Any]:
    return {
        "name": "ade",
        "type": "A",
        "ttl": ttl,
        "records": [{"value": value, "comment": "existing"}],
        "protection": {"change": protected},
    }


def _prepare(module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "BACKUP_FILE", tmp_path / "before.json")
    monkeypatch.setattr(module, "ROLLBACK_FILE", tmp_path / "rollback.md")


def test_dns_preflight_and_noop_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    _prepare(module, tmp_path, monkeypatch)
    client = FakeClient(a=_rrset("138.199.173.27"))
    state = module.preflight(client, {"hydrogen.ns.hetzner.com", "oxygen.ns.hetzner.com"})
    assert module.apply(client, state) == "DNS_ALREADY_CORRECT"
    backup = json.loads((tmp_path / "before.json").read_text(encoding="utf-8"))
    assert backup["zone_name"] == "musematic.ai"
    assert backup["rrsets"]["A"]["records"][0]["value"] == "138.199.173.27"
    assert all(call[0] == "GET" for call in client.calls)


def test_dns_create_and_safe_update_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    _prepare(module, tmp_path, monkeypatch)
    nameservers = {"hydrogen.ns.hetzner.com", "oxygen.ns.hetzner.com"}
    create = FakeClient()
    assert module.apply(create, module.preflight(create, nameservers)) == "DNS_API_UPDATED"
    assert create.records["A"]["records"][0]["value"] == "138.199.173.27"
    update = FakeClient(a=_rrset("192.0.2.1", ttl=600))
    assert module.apply(update, module.preflight(update, nameservers)) == "DNS_API_UPDATED"
    assert update.records["A"]["ttl"] == 300
    assert update.records["A"]["records"] == [
        {
            "value": "138.199.173.27",
            "comment": "Hermes Engineering Operations Dashboard",
        }
    ]


def test_dns_preflight_preserves_exact_initial_rollback_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    _prepare(module, tmp_path, monkeypatch)
    nameservers = {"hydrogen.ns.hetzner.com", "oxygen.ns.hetzner.com"}
    client = FakeClient()
    module.preflight(client, nameservers)
    initial = (tmp_path / "before.json").read_bytes()
    client.records["A"] = _rrset("138.199.173.27")
    module.preflight(client, nameservers)
    assert (tmp_path / "before.json").read_bytes() == initial


@pytest.mark.parametrize(
    ("client", "expected"),
    [
        (FakeClient(a=_rrset("192.0.2.1", protected=True)), "DNS_RECORD_CONFLICT"),
        (
            FakeClient(
                cname={
                    "name": "ade",
                    "type": "CNAME",
                    "ttl": 300,
                    "records": [{"value": "other.example"}],
                    "protection": {"change": False},
                }
            ),
            "DNS_RECORD_CONFLICT",
        ),
    ],
)
def test_dns_conflict_and_protection_fixtures(
    client: FakeClient,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    _prepare(module, tmp_path, monkeypatch)
    with pytest.raises(module.DNSFailure, match=expected):
        module.preflight(client, {"hydrogen.ns.hetzner.com", "oxygen.ns.hetzner.com"})


def test_dns_authority_and_token_redaction_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    _prepare(module, tmp_path, monkeypatch)
    with pytest.raises(module.DNSFailure, match="DNS_ZONE_NOT_AUTHORITATIVE"):
        module.preflight(FakeClient(), {"unrelated.example"})

    token_parent = tmp_path / "token-parent"
    token_parent.mkdir(mode=0o700)
    os.chmod(token_parent, 0o700)
    token_file = token_parent / "token"
    token_file.write_text("fixture-credential", encoding="utf-8")
    os.chmod(token_file, 0o600)
    monkeypatch.setattr(module, "TOKEN_FILE", token_file)
    credential = module.token_from_file()

    def fail(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError("fixed", 403, "denied", {}, None)

    monkeypatch.setattr(module.urllib.request, "urlopen", fail)
    with pytest.raises(module.DNSFailure) as error:
        module.HetznerDNSClient(credential).request("GET", "/zones?name=musematic.ai")
    assert credential not in str(error.value)
    assert str(error.value) == "HETZNER_DNS_TOKEN_NOT_READY"
