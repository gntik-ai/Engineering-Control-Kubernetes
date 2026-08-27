#!/usr/bin/env python3
"""Hard-bound Hetzner Cloud DNS helper for ade.musematic.ai only."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

API_BASE = "https://api.hetzner.cloud/v1"
ZONE = "musematic.ai"
RECORD_NAME = "ade"
RECORD_TYPE = "A"
RECORD_VALUE = "138.199.173.27"
RECORD_TTL = 300
RECORD_COMMENT = "Hermes Engineering Operations Dashboard"
TOKEN_FILE = Path("/home/hermes/.config/hermes-dashboard/hetzner-cloud-api.token")
BACKUP_FILE = Path("/home/hermes/engineering/HERMES-DASHBOARD-DNS-BEFORE.json")
ROLLBACK_FILE = Path("/home/hermes/engineering/HERMES-DASHBOARD-DNS-ROLLBACK.md")


class DNSFailure(RuntimeError):
    pass


class Client(Protocol):
    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]: ...


class HetznerDNSClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        if not path.startswith("/zones") or ".." in path or "//" in path:
            raise DNSFailure("HETZNER_DNS_TOKEN_NOT_READY")
        data = None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(  # noqa: S310 - base and paths are fixed HTTPS values
            f"{API_BASE}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                body = response.read(2_000_000)
                value = json.loads(body) if body else {}
                if not isinstance(value, dict):
                    raise DNSFailure("HETZNER_DNS_TOKEN_NOT_READY")
                return response.status, value
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return 404, {}
            raise DNSFailure("HETZNER_DNS_TOKEN_NOT_READY") from None
        except (OSError, TimeoutError, json.JSONDecodeError) as error:
            raise DNSFailure("HETZNER_DNS_TOKEN_NOT_READY") from error


def token_from_file() -> str:
    if pwd.getpwuid(os.geteuid()).pw_name != "hermes":
        raise DNSFailure("HETZNER_DNS_TOKEN_NOT_READY")
    parent = TOKEN_FILE.parent
    parent_metadata = parent.stat(follow_symlinks=False)
    hermes = pwd.getpwnam("hermes")
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent.is_symlink()
        or parent_metadata.st_uid != hermes.pw_uid
        or parent_metadata.st_gid != hermes.pw_gid
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise DNSFailure("HETZNER_DNS_TOKEN_NOT_READY")
    descriptor = os.open(TOKEN_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != hermes.pw_uid
            or metadata.st_gid != hermes.pw_gid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 0
        ):
            raise DNSFailure("HETZNER_DNS_TOKEN_NOT_READY")
        raw = os.read(descriptor, 4096)
    finally:
        os.close(descriptor)
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise DNSFailure("HETZNER_DNS_TOKEN_NOT_READY") from error
    if not token:
        raise DNSFailure("HETZNER_DNS_TOKEN_NOT_READY")
    return token


def public_nameservers() -> set[str]:
    try:
        result = subprocess.run(  # noqa: S603 - executable and every argument are fixed
            ["/usr/bin/dig", "+short", "NS", ZONE],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DNSFailure("DNS_ZONE_NOT_AUTHORITATIVE") from error
    return {line.strip().rstrip(".").lower() for line in result.stdout.splitlines() if line.strip()}


def preflight(client: Client, delegated_nameservers: set[str]) -> dict[str, Any]:
    query = urllib.parse.urlencode({"name": ZONE, "per_page": 50})
    status, payload = client.request("GET", f"/zones?{query}")
    zones = payload.get("zones") if status == 200 else None
    if not isinstance(zones, list):
        raise DNSFailure("HETZNER_DNS_TOKEN_NOT_READY")
    matches = [zone for zone in zones if isinstance(zone, dict) and zone.get("name") == ZONE]
    if len(matches) != 1:
        raise DNSFailure("HETZNER_DNS_TOKEN_NOT_READY")
    zone = matches[0]
    zone_id = zone.get("id")
    authoritative = zone.get("authoritative_nameservers")
    assigned = authoritative.get("assigned") if isinstance(authoritative, dict) else None
    assigned_set = {
        str(value).rstrip(".").lower() for value in assigned or [] if isinstance(value, str)
    }
    if (
        not isinstance(zone_id, int)
        or zone.get("mode") != "primary"
        or not assigned_set
        or delegated_nameservers != assigned_set
    ):
        raise DNSFailure("DNS_ZONE_NOT_AUTHORITATIVE")
    records = {
        record_type: _get_rrset(client, zone_id, RECORD_NAME, record_type)
        for record_type in ("A", "AAAA", "CNAME")
    }
    if records["CNAME"] is not None or records["AAAA"] is not None:
        raise DNSFailure("DNS_RECORD_CONFLICT")
    existing = records["A"]
    if existing is not None:
        protection = existing.get("protection")
        if isinstance(protection, dict) and protection.get("change") is True:
            raise DNSFailure("DNS_RECORD_CONFLICT")
        values = _record_values(existing)
        if len(values) != 1:
            raise DNSFailure("DNS_RECORD_CONFLICT")
    result = {
        "zone": zone,
        "zone_id": zone_id,
        "assigned_nameservers": sorted(assigned_set),
        "delegated_nameservers": sorted(delegated_nameservers),
        "rrsets": records,
    }
    _write_backup(result)
    return result


def apply(client: Client, state: dict[str, Any]) -> str:
    zone_id = _validated_zone_id(state)
    existing = state["rrsets"]["A"]
    if existing is not None:
        values = _record_values(existing)
        if values == [RECORD_VALUE] and existing.get("ttl") == RECORD_TTL:
            verify(client, zone_id)
            return "DNS_ALREADY_CORRECT"
        if len(values) != 1:
            raise DNSFailure("DNS_RECORD_CONFLICT")
        if existing.get("ttl") != RECORD_TTL:
            _action(
                client,
                zone_id,
                f"/zones/{zone_id}/rrsets/{RECORD_NAME}/{RECORD_TYPE}/actions/change_ttl",
                {"ttl": RECORD_TTL},
            )
        _action(
            client,
            zone_id,
            f"/zones/{zone_id}/rrsets/{RECORD_NAME}/{RECORD_TYPE}/actions/set_records",
            {"records": [{"value": RECORD_VALUE, "comment": RECORD_COMMENT}]},
        )
    else:
        status, payload = client.request(
            "POST",
            f"/zones/{zone_id}/rrsets",
            {
                "name": RECORD_NAME,
                "type": RECORD_TYPE,
                "ttl": RECORD_TTL,
                "records": [{"value": RECORD_VALUE, "comment": RECORD_COMMENT}],
            },
        )
        if status not in {200, 201}:
            raise DNSFailure("DNS_RECORD_CONFLICT")
        _wait_action(client, zone_id, payload)
    verify(client, zone_id)
    return "DNS_API_UPDATED"


def verify(client: Client, zone_id: int) -> dict[str, Any]:
    rrset = _get_rrset(client, zone_id, RECORD_NAME, RECORD_TYPE)
    if rrset is None or _record_values(rrset) != [RECORD_VALUE] or rrset.get("ttl") != RECORD_TTL:
        raise DNSFailure("DNS_API_UPDATED_PROPAGATION_PENDING")
    return rrset


def rollback(client: Client) -> str:
    backup = _read_backup()
    zone_id = _validated_zone_id(backup)
    before = backup["rrsets"]["A"]
    current = _get_rrset(client, zone_id, RECORD_NAME, RECORD_TYPE)
    if current is not None:
        protection = current.get("protection")
        if isinstance(protection, dict) and protection.get("change") is True:
            raise DNSFailure("DNS_RECORD_CONFLICT")
    if before is None:
        if current is not None:
            status, payload = client.request(
                "DELETE", f"/zones/{zone_id}/rrsets/{RECORD_NAME}/{RECORD_TYPE}"
            )
            if status not in {200, 204}:
                raise DNSFailure("DNS_RECORD_CONFLICT")
            _wait_action(client, zone_id, payload)
        return "DNS_ROLLBACK_COMPLETE"
    records = before.get("records")
    ttl = before.get("ttl")
    if not isinstance(records, list) or not isinstance(ttl, int):
        raise DNSFailure("DNS_RECORD_CONFLICT")
    safe_records = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("value"), str):
            raise DNSFailure("DNS_RECORD_CONFLICT")
        safe_records.append(
            {"value": record["value"], "comment": str(record.get("comment") or "")[:500]}
        )
    if current is None:
        status, payload = client.request(
            "POST",
            f"/zones/{zone_id}/rrsets",
            {"name": RECORD_NAME, "type": RECORD_TYPE, "ttl": ttl, "records": safe_records},
        )
        if status not in {200, 201}:
            raise DNSFailure("DNS_RECORD_CONFLICT")
        _wait_action(client, zone_id, payload)
    else:
        _action(
            client,
            zone_id,
            f"/zones/{zone_id}/rrsets/{RECORD_NAME}/{RECORD_TYPE}/actions/change_ttl",
            {"ttl": ttl},
        )
        _action(
            client,
            zone_id,
            f"/zones/{zone_id}/rrsets/{RECORD_NAME}/{RECORD_TYPE}/actions/set_records",
            {"records": safe_records},
        )
    return "DNS_ROLLBACK_COMPLETE"


def _get_rrset(client: Client, zone_id: int, name: str, record_type: str) -> dict[str, Any] | None:
    status, payload = client.request("GET", f"/zones/{zone_id}/rrsets/{name}/{record_type}")
    if status == 404:
        return None
    rrset = payload.get("rrset") if status == 200 else None
    if not isinstance(rrset, dict):
        raise DNSFailure("HETZNER_DNS_TOKEN_NOT_READY")
    if rrset.get("name") != name or rrset.get("type") != record_type:
        raise DNSFailure("DNS_RECORD_CONFLICT")
    return rrset


def _record_values(rrset: dict[str, Any]) -> list[str]:
    records = rrset.get("records")
    if not isinstance(records, list):
        raise DNSFailure("DNS_RECORD_CONFLICT")
    values: list[str] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("value"), str):
            raise DNSFailure("DNS_RECORD_CONFLICT")
        values.append(record["value"])
    return values


def _action(client: Client, zone_id: int, path: str, payload: dict[str, Any]) -> None:
    status, response = client.request("POST", path, payload)
    if status not in {200, 201}:
        raise DNSFailure("DNS_RECORD_CONFLICT")
    _wait_action(client, zone_id, response)


def _wait_action(client: Client, zone_id: int, payload: dict[str, Any]) -> None:
    action = payload.get("action")
    if not isinstance(action, dict):
        return
    action_id = action.get("id")
    if not isinstance(action_id, int):
        raise DNSFailure("DNS_RECORD_CONFLICT")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status, response = client.request("GET", f"/zones/{zone_id}/actions/{action_id}")
        current = response.get("action") if status == 200 else None
        if not isinstance(current, dict):
            raise DNSFailure("DNS_RECORD_CONFLICT")
        if current.get("status") == "success":
            return
        if current.get("status") == "error":
            raise DNSFailure("DNS_RECORD_CONFLICT")
        time.sleep(0.5)
    raise DNSFailure("DNS_RECORD_CONFLICT")


def _write_backup(state: dict[str, Any]) -> None:
    if _preserve_existing_backup(state):
        return
    zone = state["zone"]
    safe = {
        "captured_at": datetime.now(UTC).isoformat(),
        "zone_id": state["zone_id"],
        "zone_name": zone.get("name"),
        "zone_mode": zone.get("mode"),
        "zone_protection": zone.get("protection"),
        "assigned_nameservers": state["assigned_nameservers"],
        "delegated_nameservers": state["delegated_nameservers"],
        "record_name": RECORD_NAME,
        "record_type": RECORD_TYPE,
        "rrsets": state["rrsets"],
    }
    _atomic_text(BACKUP_FILE, json.dumps(safe, indent=2, sort_keys=True) + "\n")
    rollback_text = f"""# Hermes Dashboard DNS Rollback

This procedure restores only `{RECORD_NAME}.{ZONE}` `{RECORD_TYPE}` from `{BACKUP_FILE}`.

Do not run unless deployment rollback is explicitly requested. Run as `hermes`:

```bash
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/hetzner-dashboard-dns.py rollback
```

The helper fails closed on zone, record, protection, or backup drift and never alters another RRSet.
"""
    _atomic_text(ROLLBACK_FILE, rollback_text)


def _preserve_existing_backup(state: dict[str, Any]) -> bool:
    try:
        metadata = BACKUP_FILE.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
    ):
        raise DNSFailure("DNS_RECORD_CONFLICT")
    try:
        existing = _read_backup()
    except (OSError, json.JSONDecodeError) as error:
        raise DNSFailure("DNS_RECORD_CONFLICT") from error
    if (
        _validated_zone_id(existing) != _validated_zone_id(state)
        or existing.get("record_name") != RECORD_NAME
        or existing.get("record_type") != RECORD_TYPE
        or existing.get("assigned_nameservers") != state.get("assigned_nameservers")
        or existing.get("delegated_nameservers") != state.get("delegated_nameservers")
        or not isinstance(existing.get("rrsets"), dict)
    ):
        raise DNSFailure("DNS_RECORD_CONFLICT")
    return True


def _read_backup() -> dict[str, Any]:
    descriptor = os.open(BACKUP_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        value = json.loads(os.read(descriptor, 2_000_000))
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise DNSFailure("DNS_RECORD_CONFLICT")
    return value


def _validated_zone_id(state: dict[str, Any]) -> int:
    zone_id = state.get("zone_id")
    if not isinstance(zone_id, int):
        raise DNSFailure("DNS_ZONE_NOT_AUTHORITATIVE")
    zone = state.get("zone")
    zone_name = zone.get("name") if isinstance(zone, dict) else state.get("zone_name")
    if zone_name != ZONE:
        raise DNSFailure("DNS_ZONE_NOT_AUTHORITATIVE")
    return zone_id


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="hetzner-dashboard-dns")
    command.add_argument("operation", choices=("preflight", "apply", "verify", "rollback"))
    return command


def main() -> None:
    operation = parser().parse_args().operation
    try:
        client = HetznerDNSClient(token_from_file())
        if operation == "preflight":
            preflight(client, public_nameservers())
            result = "DNS_PREFLIGHT_OK"
        elif operation == "apply":
            result = apply(client, preflight(client, public_nameservers()))
        elif operation == "verify":
            state = preflight(client, public_nameservers())
            verify(client, _validated_zone_id(state))
            result = "DNS_API_VERIFIED"
        else:
            result = rollback(client)
        print(result)
    except DNSFailure as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
