import json
from pathlib import Path

import pytest
import yaml
from jsonschema import ValidationError
from jsonschema.validators import Draft202012Validator

from engineering_control.models.enums import Project
from engineering_control.models.reconciliation import DeliveryControlPolicy

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


def test_control_configuration_requires_immutable_agent_pack_provenance() -> None:
    value = yaml.safe_load((ROOT / "config" / "control.yaml").read_text(encoding="utf-8"))

    assert value["agents"] == {
        "pack_root": "/opt/engineering-agent-pack/source",
        "immutable_release_required": True,
        "instruction_provenance_persisted": True,
        "invalid_pack_policy": "fail-closed",
    }


def test_project_delivery_controls_are_strict_and_match_discovered_reality() -> None:
    policies = {}
    for project in Project:
        value = yaml.safe_load(
            (ROOT / "config" / "projects" / f"{project.value}.yaml").read_text(encoding="utf-8")
        )
        assert value["project"] == project.value
        if project is Project.GNTIK_UI:
            # NEW-160: no runtime: a merge-only delivery, never the candidate pipeline.
            assert value["delivery_control"]["merge_delivery"]["enabled"] is True
            with pytest.raises(ValueError):
                DeliveryControlPolicy.model_validate(value["delivery_control"])
            continue
        policies[project] = DeliveryControlPolicy.model_validate(value["delivery_control"])
    assert policies[Project.FALCONE].source_ci_workflow == "ci"
    assert policies[Project.FALCONE].candidate_build.enabled is False
    assert policies[Project.FALCONE].immutable_promotion.enabled is True
    assert policies[Project.MUSEMATIC].candidate_build.enabled is False
    assert policies[Project.MUSEMATIC].immutable_promotion.blocker == (
        "MUSEMATIC_DIGEST_RENDERING_UNSUPPORTED"
    )


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
        "llmwiki-preview",
        "llmwiki-staging",
    }
    for path in targets:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        validator.validate(value)
        assert path.stem == f"{value['project']}-{value['environment']}"
        assert value["enabled"] is False


def test_deployment_target_schema_allows_only_complete_preview_cleanup() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "deployment-target.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    target = {
        "version": 1,
        "enabled": True,
        "project": "falcone",
        "environment": "preview",
        "strategy": "helm",
        "target_context": "falcone-preview",
        "target_namespace": "falcone-preview-deadbeef",
        "configuration": {
            "values_files": ["values-preview.yaml"],
            "release_name": "falcone-preview-deadbeef",
            "kube_context": "falcone-preview-releaser",
            "kubeconfig_path": ("/etc/engineering-control/env/release/falcone-preview.kubeconfig"),
            "cleanup_enabled": True,
            "cleanup_namespace_prefix": "falcone-preview-",
            "cleanup_kube_context": "falcone-preview-releaser",
            "cleanup_kubeconfig_path": (
                "/etc/engineering-control/env/release/falcone-preview.kubeconfig"
            ),
        },
    }

    validator.validate(target)
    target["environment"] = "staging"
    with pytest.raises(ValidationError):
        validator.validate(target)


def test_runtime_target_templates_match_the_strict_schema() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "runtime-target.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    targets = sorted((ROOT / "config" / "runtime").glob("*.yaml"))
    assert {path.stem for path in targets} == {
        "falcone-staging",
        "musematic-staging",
        "llmwiki-staging",
    }
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
