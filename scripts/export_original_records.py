#!/usr/bin/env python3
"""Export one project's records from the original system, for Engineering Control.

Run it on the host of the original system, with the Python of its virtual environment.
It reads the database and writes one file. It changes nothing: every statement is a
SELECT inside a read-only transaction, and the file is the only thing it writes.

    sudo /opt/engineering-control/venv/bin/python export_original_records.py \\
        --project falcone-workflow --output falcone-workflow.records.json

The database's address is taken from --database-url, else from the environment variable
ENGINEERING_DATABASE_URL, else from the files in /etc/engineering-control/env. It is never
printed and never written to the file.

What is exported, for the one project: its audit campaigns, its findings with their
verifications and the coverage cells they belong to, the issues published for them, its
change sets and their approvals, its enhancements, its backlog campaigns and, for each
GitHub issue, the latest record of it. Engineering Control's project page takes the file
("Move work here") and shows what it would bring over before anything is stored.
"""

from __future__ import annotations

import argparse
import datetime
import decimal
import glob
import json
import os
import sys
import uuid
from typing import Any

FORMAT = "engineering-control-records/1"
ENV_FILES = "/etc/engineering-control/env/*.env"
# Columns left out: large, and read again from GitHub when needed.
OMITTED = {"backlog_issue_records": {"github_snapshot_json"}}


def _database_url(given: str | None) -> str:
    if given:
        return given
    found = os.environ.get("ENGINEERING_DATABASE_URL")
    if found:
        return found
    for path in sorted(glob.glob(ENV_FILES)):
        try:
            lines = open(path, encoding="utf-8").read().splitlines()  # noqa: SIM115
        except OSError:
            continue
        for line in lines:
            name, _, value = line.strip().partition("=")
            if name.removeprefix("export ").strip() == "ENGINEERING_DATABASE_URL" and value:
                return value.strip().strip("'\"")
    sys.exit(
        "The database's address was not found. Give it with --database-url, or run this "
        "with ENGINEERING_DATABASE_URL set (the original system's services have it)."
    )


def _plain(value: Any, *, structured: bool = False) -> Any:
    """A value as JSON can hold it. ``structured`` says the column holds JSON: a driver
    that hands such a column over as text has it read."""
    if isinstance(value, datetime.datetime | datetime.date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes | memoryview):
        return None
    if structured and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project", required=True, help="the project's name, as the original has it"
    )
    parser.add_argument("--output", required=True, help="the file to write")
    parser.add_argument("--database-url", default=None)
    arguments = parser.parse_args()

    try:
        from sqlalchemy import create_engine, inspect, text
    except ImportError:
        sys.exit("Run this with the Python of the original system's virtual environment.")

    engine = create_engine(_database_url(arguments.database_url))
    project = arguments.project
    tables: dict[str, list[dict[str, Any]]] = {}

    with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SET TRANSACTION READ ONLY"))
        inspector = inspect(connection)
        present = set(inspector.get_table_names())
        structured: dict[str, set[str]] = {}

        def rows(table: str, where: str, **parameters: Any) -> list[dict[str, Any]]:
            if table not in present:
                return []
            if table not in structured:
                structured[table] = {
                    str(column["name"])
                    for column in inspector.get_columns(table)
                    if "JSON" in str(column["type"]).upper()
                }
            found = connection.execute(text(f"SELECT * FROM {table} WHERE {where}"), parameters)  # noqa: S608
            left_out = OMITTED.get(table, set())
            return [
                {
                    key: _plain(value, structured=key in structured[table])
                    for key, value in row.items()
                    if key not in left_out
                }
                for row in found.mappings()
            ]

        def among(table: str, column: str, values: list[Any]) -> list[dict[str, Any]]:
            """The rows whose column is one of the values, asked for in portions."""
            found: list[dict[str, Any]] = []
            wanted = sorted({str(value) for value in values if value})
            for start in range(0, len(wanted), 400):
                portion = wanted[start : start + 400]
                names = ", ".join(f":v{index}" for index in range(len(portion)))
                found += rows(
                    table,
                    f"CAST({column} AS VARCHAR(64)) IN ({names})",
                    **{f"v{index}": value for index, value in enumerate(portion)},
                )
            return found

        tables["campaigns"] = rows("campaigns", "project = :project", project=project)
        tables["findings"] = rows("findings", "project = :project", project=project)
        finding_ids = [row["id"] for row in tables["findings"]]
        tables["coverage_cells"] = among(
            "coverage_cells", "id", [row.get("coverage_cell_id") for row in tables["findings"]]
        )
        tables["finding_verifications"] = among("finding_verifications", "finding_id", finding_ids)
        tables["issues"] = among("issues", "finding_id", finding_ids)
        tables["change_sets"] = rows("change_sets", "project = :project", project=project)
        tables["approvals"] = among(
            "approvals", "change_set_id", [row["id"] for row in tables["change_sets"]]
        )
        tables["enhancements"] = rows("enhancements", "project = :project", project=project)
        tables["github_backlog_campaigns"] = rows(
            "github_backlog_campaigns", "project = :project", project=project
        )
        records = among(
            "backlog_issue_records",
            "backlog_campaign_id",
            [row["id"] for row in tables["github_backlog_campaigns"]],
        )
        # Each import of the backlog recorded every issue again: the latest record of an
        # issue is the one that says how it stands.
        latest: dict[tuple[str, int], dict[str, Any]] = {}
        for record in sorted(records, key=lambda row: str(row.get("updated_at") or "")):
            latest[(str(record.get("repository")), int(record.get("issue_number") or 0))] = record
        tables["backlog_issue_records"] = list(latest.values())
        # Work the original still has under way for the project: nothing is brought over
        # in the middle of a run, so the import says so and waits unless told otherwise.
        since = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=14)).isoformat()
        in_flight = [
            {key: row.get(key) for key in ("workflow_id", "run_type", "status", "started_at")}
            for row in rows(
                "runs",
                "project = :project AND finished_at IS NULL AND started_at IS NOT NULL "
                "AND CAST(started_at AS VARCHAR(40)) > :since",
                project=project,
                since=since,
            )
        ]
        revision = None
        if "alembic_version" in present:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        connection.rollback()

    document = {
        "format": FORMAT,
        "project": project,
        "exported_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "schema_revision": revision,
        "in_flight": in_flight,
        "tables": tables,
    }
    with open(arguments.output, "w", encoding="utf-8") as output:
        json.dump(document, output, ensure_ascii=False, separators=(",", ":"))
    os.chmod(arguments.output, 0o600)
    size = os.path.getsize(arguments.output)
    print(f"Wrote {arguments.output} ({size / 1_000_000:.1f} MB) for the project {project}:")
    for name, found in tables.items():
        print(f"  {name}: {len(found)}")
    if not any(tables.values()):
        print("Nothing was found. Is the project's name written as the original has it?")
    if in_flight:
        print(
            f"The original system still has {len(in_flight)} run(s) under way for this project, "
            "started within the last 14 days:"
        )
        for run in in_flight[:20]:
            print(f"  {run['run_type']} {run['workflow_id']} ({run['status']})")
        print("Let them end and export again, or bring the file over knowing that they run.")


if __name__ == "__main__":
    main()
