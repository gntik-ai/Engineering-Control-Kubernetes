from __future__ import annotations

from pathlib import Path

import yaml


def test_postgresql_acceptance_workflow_is_bounded_and_pinned() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow_path = root / ".github" / "workflows" / "postgresql-acceptance.yml"
    assert (root / "requirements.lock").is_file()
    document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    assert document["name"] == "PostgreSQL acceptance"
    # YAML 1.1 parsers intentionally resolve the unquoted workflow `on` key as true.
    triggers = document[True]
    assert set(triggers) == {"pull_request", "push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]
    assert document["permissions"] == {"contents": "read"}

    job = document["jobs"]["postgresql-17"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 20
    assert job["services"]["postgres"]["image"] == (
        "postgres@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73"
    )
    steps = job["steps"]
    assert steps[0]["uses"] == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    assert steps[0]["with"]["persist-credentials"] is False
    assert steps[1]["uses"] == "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"
    assert steps[1]["with"]["python-version"] == "3.14"
    assert steps[2]["run"] == (
        'python -m pip install --constraint requirements.lock --editable ".[dev]"'
    )
    assert steps[3]["run"] == (
        "python -m pytest -q -m postgresql tests/test_postgresql_acceptance.py"
    )
