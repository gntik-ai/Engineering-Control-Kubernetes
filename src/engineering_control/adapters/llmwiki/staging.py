from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RuntimeValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,119}$")
    classification: Literal[
        "non-secret", "secret-reference", "runtime-generated", "environment-specific"
    ]
    required: bool = True
    source: str = Field(min_length=1, max_length=300)


class BootstrapAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,119}$")
    surface: Literal["falcone", "keycloak", "llmwiki-runtime", "staging-deployment", "validation"]
    reconcile: Literal["discover", "create-if-absent", "update-if-drifted", "validate"]
    idempotency_key: str = Field(pattern=r"^llmwiki-staging:[a-z0-9:-]{3,200}$")
    mutation_class: Literal[
        "IDENTITY_BOOTSTRAP", "APPLICATION_DATA_MUTATION", "DEPLOYMENT_MUTATION", "NONE"
    ]
    rollback: str = Field(min_length=1, max_length=1000)


class LLMWikiStagingBootstrapPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    project: Literal["llmwiki"] = "llmwiki"
    environment: Literal["staging"] = "staging"
    mode: Literal["plan-only"] = "plan-only"
    source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    falcone_source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    falcone_deployment_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    namespace: Literal["llmwiki-e2e"] = "llmwiki-e2e"
    oidc_client_type: Literal["public"] = "public"
    oidc_flow: Literal["authorization-code-pkce"] = "authorization-code-pkce"
    oidc_client_secret: Literal[None] = None
    required_values: tuple[RuntimeValue, ...]
    actions: tuple[BootstrapAction, ...]
    blockers: tuple[str, ...] = ()
    production_supported: Literal[False] = False

    @model_validator(mode="after")
    def safety(self) -> LLMWikiStagingBootstrapPlan:
        names = {item.name for item in self.required_values}
        forbidden = {"FALCONE_SUPERADMIN_PASSWORD", "OIDC_CLIENT_SECRET", "FALCONE_ACCESS_TOKEN"}
        if names & forbidden:
            raise ValueError("LLMWiki deployment may not receive product or protocol credentials")
        if any(
            action.surface == "keycloak" and action.mutation_class != "IDENTITY_BOOTSTRAP"
            for action in self.actions
        ):
            raise ValueError(
                "Keycloak reconciliation must use the identity-bootstrap mutation class"
            )
        return self

    def approval_binding(self) -> str:
        raw = self.model_dump(mode="json", exclude={"blockers"})
        canonical = json.dumps(raw, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(canonical).hexdigest()


class LLMWikiBootstrapPlanner:
    """Build a deterministic, idempotent plan; execution is a separate approved workflow."""

    _runtime_values = (
        RuntimeValue(
            name="FALCONE_API_BASE_URL",
            classification="environment-specific",
            source="verified Falcone staging origin",
        ),
        RuntimeValue(
            name="FALCONE_ISSUER",
            classification="environment-specific",
            source="verified Keycloak discovery issuer",
        ),
        RuntimeValue(
            name="PORTAL_OIDC_CLIENT_ID",
            classification="non-secret",
            source="public-client reconciliation result",
        ),
        RuntimeValue(
            name="PORTAL_PUBLIC_ORIGIN",
            classification="environment-specific",
            source="verified staging route",
        ),
        RuntimeValue(
            name="FALCONE_WORKSPACE_ID",
            classification="non-secret",
            source="dedicated staging workspace reconciliation",
        ),
        RuntimeValue(
            name="OIDC_IDENTITY_PROVIDERS_JSON",
            classification="non-secret",
            source="enabled provider metadata",
        ),
        RuntimeValue(
            name="WIKI_PROFILE_DATABASE",
            classification="non-secret",
            source="Falcone resource reconciliation",
        ),
        RuntimeValue(
            name="WIKI_PROFILE_COLLECTION",
            classification="non-secret",
            source="Falcone resource reconciliation",
        ),
        RuntimeValue(
            name="WIKI_PROJECT_DATABASE",
            classification="non-secret",
            source="Falcone resource reconciliation",
        ),
        RuntimeValue(
            name="WIKI_PROJECT_COLLECTION",
            classification="non-secret",
            source="Falcone resource reconciliation",
        ),
        RuntimeValue(
            name="WIKI_RUN_DATABASE",
            classification="non-secret",
            source="Falcone resource reconciliation",
        ),
        RuntimeValue(
            name="WIKI_RUN_COLLECTION",
            classification="non-secret",
            source="Falcone resource reconciliation",
        ),
        RuntimeValue(
            name="WIKI_ARTIFACT_BUCKET",
            classification="non-secret",
            source="Falcone resource reconciliation",
        ),
        RuntimeValue(
            name="PORTAL_SESSION_TTL_MS",
            classification="non-secret",
            source="bounded session policy",
        ),
        RuntimeValue(
            name="PORTAL_OIDC_TRANSACTION_TTL_MS",
            classification="non-secret",
            source="bounded OIDC transaction policy",
        ),
    )

    @classmethod
    def plan(
        cls,
        *,
        source_sha: str,
        falcone_source_sha: str,
        falcone_deployment_sha: str,
        falcone_url_resolved: bool,
        public_origin_resolved: bool,
        image_pull_ready: bool,
    ) -> LLMWikiStagingBootstrapPlan:
        blockers = []
        if not falcone_url_resolved:
            blockers.append("BLOCKED_BY_FALCONE_API_URL_DISCREPANCY")
        if not public_origin_resolved:
            blockers.append("BLOCKED_BY_STAGING_PUBLIC_ORIGIN")
        if not image_pull_ready:
            blockers.append("BLOCKED_BY_IMAGE_PULL_AUTH")
        actions = (
            cls._action(
                "verify-falcone", "falcone", "discover", "NONE", "No mutation to roll back."
            ),
            cls._action(
                "reconcile-tenant-workspace",
                "falcone",
                "create-if-absent",
                "IDENTITY_BOOTSTRAP",
                "Delete only resources created by the bound bootstrap execution.",
            ),
            cls._action(
                "reconcile-public-oidc",
                "keycloak",
                "update-if-drifted",
                "IDENTITY_BOOTSTRAP",
                (
                    "Restore the captured public-client configuration or remove a newly "
                    "created client."
                ),
            ),
            cls._action(
                "reconcile-falcone-databases",
                "falcone",
                "create-if-absent",
                "APPLICATION_DATA_MUTATION",
                "Remove only empty profile, project and run resources created by this binding.",
            ),
            cls._action(
                "reconcile-artifact-bucket",
                "falcone",
                "create-if-absent",
                "APPLICATION_DATA_MUTATION",
                "Remove only an empty artifact bucket created by this binding.",
            ),
            cls._action(
                "reconcile-flows-sse",
                "falcone",
                "create-if-absent",
                "APPLICATION_DATA_MUTATION",
                "Remove only bootstrap-created Flows after dependent runs are absent.",
            ),
            cls._action(
                "verify-llm-provider-metadata",
                "falcone",
                "validate",
                "NONE",
                "No mutation to roll back.",
            ),
            cls._action(
                "render-runtime-config",
                "llmwiki-runtime",
                "validate",
                "NONE",
                "Discard rendered non-secret output.",
            ),
            cls._action(
                "reconcile-staging-manifest",
                "staging-deployment",
                "update-if-drifted",
                "DEPLOYMENT_MUTATION",
                "Restore the captured manifest and immutable image references.",
            ),
            cls._action(
                "validate-staging",
                "validation",
                "validate",
                "NONE",
                "Clean bootstrap-only identities and evidence handles.",
            ),
        )
        return LLMWikiStagingBootstrapPlan(
            source_sha=source_sha,
            falcone_source_sha=falcone_source_sha,
            falcone_deployment_sha=falcone_deployment_sha,
            required_values=cls._runtime_values,
            actions=actions,
            blockers=tuple(blockers),
        )

    @staticmethod
    def _action(
        action_id: str,
        surface: Literal[
            "falcone", "keycloak", "llmwiki-runtime", "staging-deployment", "validation"
        ],
        reconcile: Literal["discover", "create-if-absent", "update-if-drifted", "validate"],
        mutation: Literal[
            "IDENTITY_BOOTSTRAP", "APPLICATION_DATA_MUTATION", "DEPLOYMENT_MUTATION", "NONE"
        ],
        rollback: str,
    ) -> BootstrapAction:
        return BootstrapAction(
            id=action_id,
            surface=surface,
            reconcile=reconcile,
            idempotency_key=f"llmwiki-staging:{action_id}",
            mutation_class=mutation,
            rollback=rollback,
        )
