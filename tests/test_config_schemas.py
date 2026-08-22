import json
from pathlib import Path

import yaml
from jsonschema.validators import Draft202012Validator

ROOT = Path(__file__).parents[1]


def test_all_json_schemas_are_valid_draft_2020_12() -> None:
    schemas = list((ROOT / "schemas").glob("*.schema.json"))
    assert schemas
    for path in schemas:
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_configuration_contains_no_inline_secret_values() -> None:
    paths = list((ROOT / "config").rglob("*.yaml"))
    assert paths
    for path in paths:
        text = path.read_text(encoding="utf-8")
        values = list(yaml.safe_load_all(text))
        assert values and all(isinstance(value, dict) for value in values)
        assert "BEGIN PRIVATE KEY" not in text
        assert "github_pat_" not in text
        assert "ghp_" not in text
