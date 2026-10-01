#!/usr/bin/env python3
"""NEW-108: re-derive root-owned change-request approval attestations from PostgreSQL.

The GitHub worker has no database credential; it publishes a change request only while a
root-owned attestation exists. This root oneshot consumes id-only request files written by
the API/controller (engineering-control, mode 0600) and, for every requested or currently
attested change request, writes an attestation only for a re-verified APPROVED,
unpublished row, and revokes it otherwise.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
from datetime import UTC, datetime
from pathlib import Path

from engineering_control.config import Settings
from engineering_control.db.change_requests import ChangeRequestRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--requests",
        type=Path,
        default=Path("/var/lib/engineering-control/change-request-approvals"),
    )
    parser.add_argument(
        "--attestations",
        type=Path,
        default=Path("/etc/engineering-control/policy/change-requests"),
    )
    return parser.parse_args()


def main() -> int:
    if os.geteuid() != 0:
        raise PermissionError("change request attestation requires root")
    args = parse_args()
    request_owner = pwd.getpwnam("engineering-control").pw_uid
    group = grp.getgrnam("engineering-control").gr_gid
    if args.attestations.is_symlink() or not args.attestations.is_dir():
        raise ValueError("attestation root must be a real directory")
    repository = ChangeRequestRepository(Settings().database_url.get_secret_value())
    try:
        summary = repository.reconcile_attestations_sync(
            args.requests,
            args.attestations,
            now=datetime.now(UTC),
            request_owner=request_owner,
            attestation_group=group,
        )
    finally:
        repository.engine.dispose()
    print("CHANGE_REQUEST_ATTESTATIONS " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
