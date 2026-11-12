"""Rules every manifest under deploy/base has to keep, whatever is added later."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from engineering_control.config import Settings

BASE = Path(__file__).parents[1] / "deploy" / "base"
IMAGE = "engineering-control"  # the placeholder kustomization.yaml pins to a commit
# Temporal's own images, pinned by digest, and the user they run as.
THIRD_PARTY_IMAGES = {
    "temporalio/server:1.31.2@sha256:b5ecdb8282bededae2a10c36e8d862e27d0bc2d247fc73c5416025997ab4a1da",
    "temporalio/admin-tools:1.31.2@sha256:dbc5fcd6ee8f0f4d808bf765af9a87dea9d8a283abfdcfbd2fc148496ba66107",
}
BOOTSTRAP_ACCOUNT = "engineering-control-bootstrap"


def _documents() -> list[dict[str, Any]]:
    kustomization = yaml.safe_load((BASE / "kustomization.yaml").read_text())
    documents: list[dict[str, Any]] = []
    for name in kustomization["resources"]:
        documents.extend(d for d in yaml.safe_load_all((BASE / name).read_text()) if d)
    return documents


def _pod_specs() -> list[tuple[str, dict[str, Any]]]:
    found = []
    for document in _documents():
        if document["kind"] in {"Deployment", "Job", "StatefulSet"}:
            name = f"{document['kind']}/{document['metadata']['name']}"
            found.append((name, document["spec"]["template"]["spec"]))
    return found


def test_every_file_in_the_directory_is_listed_and_nothing_else() -> None:
    kustomization = yaml.safe_load((BASE / "kustomization.yaml").read_text())
    on_disk = {path.name for path in BASE.glob("*.yaml")} - {"kustomization.yaml"}
    assert set(kustomization["resources"]) == on_disk
    assert kustomization["namespace"] == "engineering-control"


def test_the_image_is_pinned_to_a_commit() -> None:
    kustomization = yaml.safe_load((BASE / "kustomization.yaml").read_text())
    (image,) = kustomization["images"]
    assert image["name"] == IMAGE
    assert image["newName"] == "ghcr.io/gntik-ai/engineering-control-kubernetes"
    assert re.fullmatch(r"[0-9a-f]{40}", str(image["newTag"]))


def test_every_pod_runs_unprivileged_with_a_read_only_filesystem() -> None:
    specs = _pod_specs()
    assert len(specs) >= 6
    for name, spec in specs:
        pod = spec["securityContext"]
        assert pod["runAsNonRoot"] is True, name
        images = {container["image"] for container in spec["containers"]}
        third_party = images <= THIRD_PARTY_IMAGES
        assert third_party or images == {IMAGE}, name  # never a mix, never anything else
        user = 1000 if third_party else 10001
        assert pod["runAsUser"] == user and pod["runAsGroup"] == user, name
        assert pod["seccompProfile"] == {"type": "RuntimeDefault"}, name
        assert not spec.get("hostNetwork") and not spec.get("hostPID"), name
        for container in spec["containers"] + spec.get("initContainers", []):
            security = container["securityContext"]
            assert security["allowPrivilegeEscalation"] is False, name
            assert security["readOnlyRootFilesystem"] is True, name
            assert security["capabilities"] == {"drop": ["ALL"]}, name
            assert "privileged" not in security, name
            assert container["resources"]["requests"]["memory"], name
            assert container["resources"]["limits"]["memory"], name
        assert not any("hostPath" in volume for volume in spec.get("volumes", [])), name


def test_only_the_bootstrap_jobs_have_a_cluster_identity() -> None:
    for name, spec in _pod_specs():
        if spec["serviceAccountName"] == BOOTSTRAP_ACCOUNT:
            assert name in {"Job/bootstrap-secrets", "Job/bootstrap-admin-password"}
            assert spec["automountServiceAccountToken"] is True
        else:
            assert spec["serviceAccountName"] == "engineering-control", name
            assert spec["automountServiceAccountToken"] is False, name
    accounts = {d["metadata"]["name"]: d for d in _documents() if d["kind"] == "ServiceAccount"}
    assert all(account["automountServiceAccountToken"] is False for account in accounts.values())
    # Both pull the private image with the Secret deploy/registry creates.
    assert all(
        account["imagePullSecrets"] == [{"name": "engineering-control-registry"}]
        for account in accounts.values()
    )


def test_the_bootstrap_role_reaches_only_secrets_of_its_namespace() -> None:
    roles = [d for d in _documents() if d["kind"] in {"Role", "ClusterRole", "ClusterRoleBinding"}]
    assert [role["kind"] for role in roles] == ["Role"]
    for rule in roles[0]["rules"]:
        assert rule["apiGroups"] == [""] and rule["resources"] == ["secrets"]
        if set(rule["verbs"]) - {"create"}:
            assert rule["resourceNames"] == [
                "engineering-control-secrets",
                "engineering-control-admin",
            ]
        assert not {"list", "watch", "delete", "*"} & set(rule["verbs"])


def test_no_secret_value_is_in_the_repository() -> None:
    documents = _documents()
    assert not [d for d in documents if d["kind"] == "Secret"]
    text = "\n".join(path.read_text() for path in BASE.glob("*.yaml"))
    # The only password fields are a reference to a Secret key and a placeholder that the
    # Temporal pod replaces at start.
    allowed = ("key: password", 'password: "__TEMPORAL_DB_PASSWORD__"')
    for phrase in allowed:
        text = text.replace(phrase, "")
    assert "BEGIN " not in text and "password:" not in text


def test_mounted_secrets_are_group_readable_only() -> None:
    for name, spec in _pod_specs():
        for volume in spec.get("volumes", []):
            if "secret" in volume:
                assert volume["secret"]["defaultMode"] == 0o440, name


def test_only_the_console_has_a_route_from_outside_and_only_over_https() -> None:
    documents = _documents()
    for service in (d for d in documents if d["kind"] == "Service"):
        assert service["spec"].get("type", "ClusterIP") == "ClusterIP"
    [ingress] = [d for d in documents if d["kind"] == "Ingress"]
    [rule] = ingress["spec"]["rules"]
    host = rule["host"]
    backends = [path["backend"]["service"] for path in rule["http"]["paths"]]
    assert backends == [{"name": "engineering-control-console", "port": {"name": "http"}}]
    [tls] = ingress["spec"]["tls"]
    assert tls["hosts"] == [host]
    assert ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/ssl-redirect"] == "true"
    [certificate] = [d for d in documents if d["kind"] == "Certificate"]
    assert certificate["spec"]["dnsNames"] == [host]
    assert certificate["spec"]["secretName"] == tls["secretName"]
    # The console refuses any other host name and any other origin for a change.
    settings = Settings()
    assert host in settings.dashboard_allowed_hosts
    assert f"https://{host}" in settings.dashboard_allowed_origins
    # A certificate that cannot be issued must not hold back the rest of the platform.
    last = max(
        int(d["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", "0"))
        for d in documents
    )
    for document in (ingress, certificate):
        assert int(document["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]) == last
        assert last > 4


def test_only_the_console_port_accepts_connections_from_outside_the_namespace() -> None:
    policies = [d for d in _documents() if d["kind"] == "NetworkPolicy"]
    assert policies and all(p["spec"]["policyTypes"] == ["Ingress"] for p in policies)
    same_namespace = [{"from": [{"podSelector": {}}]}]
    [policy] = [p for p in policies if p["spec"]["ingress"] != same_namespace]
    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "engineering-control",
        "app.kubernetes.io/component": "console",
    }
    assert policy["spec"]["ingress"] == [{"ports": [{"protocol": "TCP", "port": 8750}]}]


def test_a_volume_claim_is_never_in_an_earlier_wave_than_the_pod_that_mounts_it() -> None:
    # The cluster's storage class binds a claim when its first pod is scheduled. Argo CD
    # waits for each wave to be healthy, so a claim in an earlier wave would never bind.
    def wave(document: dict[str, Any]) -> int:
        return int(document["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])

    documents = _documents()
    claims = {
        d["metadata"]["name"]: wave(d) for d in documents if d["kind"] == "PersistentVolumeClaim"
    }
    assert claims
    mounted: dict[str, int] = {}
    for document in documents:
        if document["kind"] in {"Deployment", "StatefulSet", "Job"}:
            for volume in document["spec"]["template"]["spec"].get("volumes", []):
                if "persistentVolumeClaim" in volume:
                    name = volume["persistentVolumeClaim"]["claimName"]
                    mounted[name] = min(mounted.get(name, 99), wave(document))
    assert claims == mounted


def test_no_storage_class_is_named() -> None:
    # The class taken from another application's settings did not exist on the cluster;
    # the default class is the one known to work.
    text = "\n".join(path.read_text() for path in BASE.glob("*.yaml"))
    assert "storageClassName:" not in text and "storageClass:" not in text


def test_the_registry_chart_holds_no_credential() -> None:
    values = yaml.safe_load((BASE.parent / "registry" / "values.yaml").read_text())
    assert values == {"username": "", "token": ""}


def test_nothing_of_the_platform_may_run_on_the_sandbox_node() -> None:
    """Sandboxes run code the platform does not control; they get a node to themselves."""
    rule = {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [
                {
                    "matchExpressions": [
                        {"key": "engineering-control/sandbox", "operator": "DoesNotExist"},
                        {"key": "kubernetes.io/hostname", "operator": "NotIn", "values": ["aibot"]},
                    ]
                }
            ]
        }
    }
    pods = _pod_specs()
    assert len(pods) >= 11
    for name, spec in pods:
        assert spec.get("affinity", {}).get("nodeAffinity") == rule, name
        assert "nodeSelector" not in spec and "tolerations" not in spec, name
    clusters = [d for d in _documents() if d["kind"] == "Cluster"]
    assert len(clusters) == 2
    for cluster in clusters:
        name = cluster["metadata"]["name"]
        assert cluster["spec"]["affinity"]["nodeAffinity"] == rule, name
        assert "tolerations" not in cluster["spec"]["affinity"], name
