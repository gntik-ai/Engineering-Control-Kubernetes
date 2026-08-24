from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import yaml

from engineering_control.engineering.models import (
    CampaignPlan,
    CredentialReadiness,
    E2EFlow,
    PersonaManifest,
    ProjectManifest,
    ReadinessStatus,
)

PRESETS: dict[str, dict[str, tuple[str, ...]]] = {
    "snapshot": {
        "surfaces": ("source", "deployment", "runtime"),
        "categories": (),
        "lenses": ("deployment", "observability"),
    },
    "static-full": {
        "surfaces": ("source", "deployment", "pair"),
        "categories": (),
        "lenses": ("correctness", "contract", "security", "deployment", "supply-chain"),
    },
    "e2e-smoke": {
        "surfaces": ("source", "deployment", "pair", "runtime"),
        "categories": ("public", "authentication", "standard-user", "admin", "authorization"),
        "lenses": ("correctness", "authorization", "ux", "accessibility", "observability"),
    },
    "full-e2e": {
        "surfaces": ("source", "deployment", "pair", "runtime"),
        "categories": (
            "public",
            "authentication",
            "standard-user",
            "admin",
            "authorization",
            "integrations",
        ),
        "lenses": (
            "correctness",
            "contract",
            "security",
            "authorization",
            "tenant-isolation",
            "reliability",
            "data-lifecycle",
            "ux",
            "accessibility",
            "observability",
            "deployment",
        ),
    },
    "security": {
        "surfaces": ("source", "deployment", "pair", "runtime"),
        "categories": ("authentication", "authorization"),
        "lenses": ("security", "authorization", "tenant-isolation", "supply-chain"),
    },
    "ux": {
        "surfaces": ("source", "runtime"),
        "categories": ("public", "authentication", "standard-user", "admin"),
        "lenses": ("ux", "accessibility"),
    },
    "deployment": {
        "surfaces": ("deployment", "pair", "runtime"),
        "categories": ("public",),
        "lenses": ("deployment", "operations", "observability", "supply-chain"),
    },
}


class EngineeringCatalog:
    """Loads only bounded, schema-backed Engineering Control manifests."""

    def __init__(
        self,
        *,
        project_root: Path,
        persona_root: Path,
        flow_root: Path,
        credential_root: Path = Path("/etc/engineering-control/credentials"),
    ) -> None:
        self.project_root = project_root
        self.persona_root = persona_root
        self.flow_root = flow_root
        self.credential_root = credential_root.resolve()

    def project(self, project: str) -> ProjectManifest:
        return ProjectManifest.model_validate(self._yaml(self.project_root / f"{project}.yaml"))

    def personas(self, project: str) -> PersonaManifest:
        return PersonaManifest.model_validate(self._yaml(self.persona_root / f"{project}.yaml"))

    def flows(self, project: str) -> list[E2EFlow]:
        directory = self.flow_root / project
        if not directory.is_dir():
            raise ValueError(f"flow catalog is missing for {project}")
        flows: list[E2EFlow] = []
        for path in sorted(directory.glob("*.yaml")):
            value = self._yaml(path)
            documents = value.get("flows") if set(value) == {"flows"} else [value]
            if not isinstance(documents, list):
                raise ValueError(f"flow document is invalid: {path.name}")
            flows.extend(E2EFlow.model_validate(item) for item in documents)
        ids = [flow.id for flow in flows]
        if len(ids) != len(set(ids)):
            raise ValueError(f"flow IDs are not unique for {project}")
        if any(flow.project != project for flow in flows):
            raise ValueError(f"flow project binding changed for {project}")
        return flows

    def plan(self, project: str, preset: str, environment: str) -> CampaignPlan:
        if preset not in PRESETS:
            raise ValueError("unknown audit preset")
        manifest = self.project(project)
        personas = self.personas(project)
        all_flows = self.flows(project)
        policy = PRESETS[preset]
        selected = [flow for flow in all_flows if flow.category in policy["categories"]]
        persona_by_id = {persona.id: persona for persona in personas.personas}
        selected_personas = sorted({flow.persona for flow in selected})
        missing = sorted(set(selected_personas) - set(persona_by_id))
        if missing:
            raise ValueError(f"flows reference unknown personas: {missing}")
        readiness: list[CredentialReadiness] = []
        for persona_id in selected_personas:
            reference = persona_by_id[persona_id].credential_ref
            if reference is not None:
                readiness.append(self.credential_readiness(reference))
        requires_runtime = "runtime" in policy["surfaces"]
        observer = self.credential_readiness(manifest.deployment.observer_credential_ref)
        if requires_runtime:
            readiness.append(observer)
        blockers = [
            f"CREDENTIAL:{item.reference.removeprefix('file:')}:{item.blocker}"
            for item in readiness
            if item.blocker != "NONE"
        ]
        kubernetes = (
            ReadinessStatus.OPERATIONAL
            if requires_runtime and observer.present and observer.permissions_valid
            else ReadinessStatus.BLOCKED_BY_CREDENTIAL
            if requires_runtime
            else ReadinessStatus.DISABLED
        )
        allocation = len(selected) * max(1, len(policy["lenses"]))
        return CampaignPlan.model_validate(
            {
                "project": project,
                "preset": preset,
                "environment": environment,
                "repository_pair": manifest.repositories,
                "repository_surfaces": list(policy["surfaces"]),
                "personas": selected_personas,
                "flows": [flow.id for flow in selected],
                "lenses": list(policy["lenses"]),
                "mutating_flows": [flow.id for flow in selected if flow.mutation == "disposable"],
                "credential_readiness": readiness,
                "kubernetes_readiness": kubernetes,
                "estimated_model_allocation": {
                    "codex": (allocation + 1) // 2,
                    "claude": allocation // 2,
                },
                "blockers": blockers,
            }
        )

    def credential_readiness(self, reference: str) -> CredentialReadiness:
        raw_path = Path(reference.removeprefix("file:"))
        try:
            resolved = raw_path.resolve(strict=True)
        except FileNotFoundError:
            return CredentialReadiness(
                reference=reference,
                present=False,
                permissions_valid=False,
                blocker="MISSING",
            )
        if not resolved.is_relative_to(self.credential_root):
            raise ValueError("credential reference escaped the configured credential root")
        metadata = resolved.stat()
        permissions_valid = stat.S_ISREG(metadata.st_mode) and not (
            metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        )
        return CredentialReadiness(
            reference=reference,
            present=True,
            permissions_valid=permissions_valid,
            blocker="NONE" if permissions_valid else "PERMISSIONS",
        )

    @staticmethod
    def _yaml(path: Path) -> dict[str, Any]:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"required manifest is missing or unsafe: {path.name}")
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"manifest is not an object: {path.name}")
        return value
