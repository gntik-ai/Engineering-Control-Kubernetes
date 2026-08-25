#!/usr/bin/env python3
from __future__ import annotations

import argparse
import grp
import os
from pathlib import Path

from engineering_control.policy.github_write import GITHUB_WRITE_POLICY_PATH
from engineering_control.policy.github_write_install import reconcile_policy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=GITHUB_WRITE_POLICY_PATH)
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path("/var/backups/engineering-control/policy-migrations"),
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("GitHub write-policy reconciliation must run as root")
    if args.policy != GITHUB_WRITE_POLICY_PATH:
        raise SystemExit("refusing a noncanonical GitHub write-policy destination")
    group_id = grp.getgrnam("engineering-control").gr_gid
    action, backup = reconcile_policy(
        template=args.template,
        target=args.policy,
        backup_root=args.backup_root,
        owner_uid=0,
        owner_gid=group_id,
    )
    backup_status = "yes" if backup is not None else "no"
    print(f"PASS GitHub write-policy reconciliation action={action} backup={backup_status}")


if __name__ == "__main__":
    main()
