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


def test_deployment_target_templates_match_the_strict_schema() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "deployment-target.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    targets = sorted((ROOT / "config" / "targets").glob("*.yaml"))
    assert {path.stem for path in targets} == {
        "falcone-preview",
        "falcone-staging",
        "musematic-preview",
        "musematic-staging",
    }
    for path in targets:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        validator.validate(value)
        assert path.stem == f"{value['project']}-{value['environment']}"
        assert value["enabled"] is False


def test_runtime_target_templates_match_the_strict_schema() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "runtime-target.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    targets = sorted((ROOT / "config" / "runtime").glob("*.yaml"))
    assert {path.stem for path in targets} == {"falcone-staging", "musematic-staging"}
    for path in targets:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        validator.validate(value)
        assert path.stem == f"{value['project']}-{value['environment']}"
        assert value["enabled"] is False


def test_recovery_schema_accepts_only_typed_project_paths() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "recovery-plan.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    validator.validate(
        {
            "strategy": "helm",
            "deployment_id": "deployment",
            "change_set_id": "change-set",
            "release_name": "falcone",
            "namespace": "falcone-staging",
            "kube_context": "falcone-staging-releaser",
            "kubeconfig_path": "/etc/engineering-control/env/release/falcone.kubeconfig",
            "previous_revision": 7,
            "previous_manifest_sha256": "a" * 64,
            "original_artifact_binding_sha256": "b" * 64,
            "recovery_approval_binding_sha256": "c" * 64,
            "timeout_minutes": 30,
        }
    )
    validator.validate(
        {
            "strategy": "argocd-gitops",
            "deployment_id": "deployment",
            "change_set_id": "change-set",
            "deployment_repository": "musematic-deploy",
            "failed_revision": "d" * 40,
            "revert_target_revision": "e" * 40,
            "target_context": "musematic-staging",
            "target_namespace": "musematic-staging",
            "original_artifact_binding_sha256": "f" * 64,
            "required_path": "protected-deployment-repository-pr",
        }
    )
