"""Rules every manifest under deploy/base has to keep, whatever is added later."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from engineering_control.config import Settings

BASE = Path(__file__).parents[1] / "deploy" / "base"
IMAGE = "engineering-control"  # the placeholder kustomization.yaml pins to a commit
BOOTSTRAP_ACCOUNT = "engineering-control-bootstrap"
CONSOLE_ACCOUNT = "engineering-control-console"
LAUNCHER_ACCOUNT = "engineering-control-launcher"
EGRESS_LABELS = {
    "app.kubernetes.io/name": "engineering-control",
    "app.kubernetes.io/component": "egress",
}
TOOLS_LABELS = {
    "app.kubernetes.io/name": "engineering-control",
    "app.kubernetes.io/component": "tools",
}
PRIVATE_NETWORKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
SANDBOXES = BASE.parent / "sandboxes"
SANDBOX_NAMESPACE = "engineering-control-sandboxes"


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
    # The sandbox image is a setting of the launcher. The patch that pins it names that
    # setting by its position, and always the commit of the image above.
    (patch,) = kustomization["patches"]
    assert patch["target"] == {"kind": "Deployment", "name": "engineering-control-launcher"}
    (operation,) = yaml.safe_load(patch["patch"])
    launcher = next(spec for name, spec in _pod_specs() if name.endswith("-launcher"))
    position = int(
        operation["path"].removeprefix("/spec/template/spec/containers/0/env/").split("/")[0]
    )
    assert operation["path"].endswith(f"/env/{position}/value") and operation["op"] == "replace"
    assert launcher["containers"][0]["env"][position]["name"] == "ENGINEERING_SANDBOX_IMAGE"
    assert operation["value"] == (
        f"ghcr.io/gntik-ai/engineering-control-kubernetes-sandbox:{image['newTag']}"
    )
    Settings(sandbox_image=operation["value"])


def test_every_pod_runs_unprivileged_with_a_read_only_filesystem() -> None:
    specs = _pod_specs()
    assert len(specs) >= 6
    for name, spec in specs:
        pod = spec["securityContext"]
        assert pod["runAsNonRoot"] is True, name
        images = {container["image"] for container in spec["containers"]}
        assert images == {IMAGE}, name  # the platform's own image, and nothing else
        assert pod["runAsUser"] == 10001 and pod["runAsGroup"] == 10001, name
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


def test_only_the_bootstrap_jobs_the_console_and_the_launcher_have_a_cluster_identity() -> None:
    for name, spec in _pod_specs():
        tokens = [
            source
            for volume in spec.get("volumes", [])
            for source in volume.get("projected", {}).get("sources", [])
            if "serviceAccountToken" in source
        ]
        if spec["serviceAccountName"] == BOOTSTRAP_ACCOUNT:
            assert name in {"Job/bootstrap-secrets", "Job/bootstrap-admin-password"}
            assert spec["automountServiceAccountToken"] is True
        elif spec["serviceAccountName"] in (CONSOLE_ACCOUNT, LAUNCHER_ACCOUNT):
            # Each asks for its token explicitly: short-lived, in a volume of its own,
            # readable by its group only.
            assert name == f"Deployment/{spec['serviceAccountName']}"
            assert spec["automountServiceAccountToken"] is False
            assert tokens == [{"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}}]
            volume = next(v for v in spec["volumes"] if "projected" in v)
            assert volume["projected"]["defaultMode"] == 0o440
        else:
            assert spec["serviceAccountName"] == "engineering-control", name
            assert spec["automountServiceAccountToken"] is False, name
            assert tokens == [], name
    accounts = {d["metadata"]["name"]: d for d in _documents() if d["kind"] == "ServiceAccount"}
    assert set(accounts) == {
        "engineering-control",
        BOOTSTRAP_ACCOUNT,
        CONSOLE_ACCOUNT,
        LAUNCHER_ACCOUNT,
    }
    assert all(account["automountServiceAccountToken"] is False for account in accounts.values())
    # All pull the private image with the Secret deploy/registry creates.
    assert all(
        account["imagePullSecrets"] == [{"name": "engineering-control-registry"}]
        for account in accounts.values()
    )
    # In their own namespace these identities may do nothing: no role is bound to them.
    for binding in (d for d in _documents() if d["kind"] == "RoleBinding"):
        bound = {subject["name"] for subject in binding["subjects"]}
        assert not bound & {CONSOLE_ACCOUNT, LAUNCHER_ACCOUNT}


def _sandbox_documents() -> list[dict[str, Any]]:
    kustomization = yaml.safe_load((SANDBOXES / "kustomization.yaml").read_text())
    assert "namespace" not in kustomization, "every object names its namespace itself"
    assert {path.name for path in SANDBOXES.glob("*.yaml")} == {
        "kustomization.yaml",
        *kustomization["resources"],
    }
    documents: list[dict[str, Any]] = []
    for name in kustomization["resources"]:
        documents.extend(d for d in yaml.safe_load_all((SANDBOXES / name).read_text()) if d)
    return documents


def test_the_sandbox_namespace_refuses_privileged_pods_and_all_traffic() -> None:
    documents = _sandbox_documents()
    namespace = next(d for d in documents if d["kind"] == "Namespace")
    assert namespace["metadata"]["name"] == SANDBOX_NAMESPACE
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    for document in documents:
        if document["kind"] != "Namespace":
            assert document["metadata"]["namespace"] == SANDBOX_NAMESPACE, document["kind"]
    policies = {d["metadata"]["name"]: d["spec"] for d in documents if d["kind"] == "NetworkPolicy"}
    assert set(policies) == {"deny-everything", "egress-internet", "supervisor-tools"}
    assert policies["deny-everything"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    # A step that may reach out reaches the platform's proxy and nothing else: no name
    # server, no public address, nothing of the cluster. Nothing is ever let in.
    internet = policies["egress-internet"]
    assert internet["podSelector"] == {"matchLabels": {"engineering-control/egress": "internet"}}
    assert internet["policyTypes"] == ["Egress"]
    (way_out,) = internet["egress"]
    assert way_out == {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "engineering-control"}
                },
                "podSelector": {"matchLabels": EGRESS_LABELS},
            }
        ],
        "ports": [{"protocol": "TCP", "port": 8888}],
    }
    # The sandbox of a turn of the supervisor also reaches the platform's tool service,
    # and nothing else of the control plane.
    tools = policies["supervisor-tools"]
    assert tools["podSelector"] == {"matchLabels": {"engineering-control/tools": "supervisor"}}
    assert tools["policyTypes"] == ["Egress"]
    assert tools["egress"] == [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "engineering-control"}
                    },
                    "podSelector": {"matchLabels": TOOLS_LABELS},
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8790}],
        }
    ]
    assert not any("ingress" in spec for spec in policies.values())
    settings = next(
        d for d in _documents() if d["metadata"]["name"] == "engineering-control-settings"
    )
    assert settings["data"]["ENGINEERING_SANDBOX_NAMESPACE"] == SANDBOX_NAMESPACE
    Settings(sandbox_namespace=SANDBOX_NAMESPACE)
    # Argo CD may deploy to the two namespaces of the platform and to no other.
    project = yaml.safe_load((BASE.parent / "argocd" / "project.yaml").read_text())
    assert [d["namespace"] for d in project["spec"]["destinations"]] == [
        "engineering-control",
        SANDBOX_NAMESPACE,
    ]
    application = yaml.safe_load(
        (BASE.parent / "argocd" / "sandboxes-application.yaml").read_text()
    )
    assert application["spec"]["source"]["path"] == "deploy/sandboxes"
    assert application["spec"]["destination"]["namespace"] == SANDBOX_NAMESPACE
    assert application["spec"]["project"] == "engineering-control"


def test_the_console_writes_sign_ins_the_launcher_runs_pods_and_neither_reads_a_secret() -> None:
    documents = _sandbox_documents()
    assert not [d for d in documents if d["kind"] in {"ClusterRole", "ClusterRoleBinding"}]
    roles = {d["metadata"]["name"]: d["rules"] for d in documents if d["kind"] == "Role"}
    assert roles == {
        "account-sign-ins": [
            {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create", "update", "delete"]}
        ],
        "launcher": [
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["create", "get", "delete"]},
            {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
            {
                "apiGroups": [""],
                "resources": ["persistentvolumeclaims"],
                "verbs": ["create", "delete"],
            },
        ],
    }
    for rules in roles.values():
        for rule in rules:
            if "secrets" in rule["resources"]:
                assert not {"get", "list", "watch"} & set(rule["verbs"])
            assert "*" not in rule["verbs"] and "*" not in rule["resources"]
            assert "pods/exec" not in rule["resources"]
    bound = {d["roleRef"]["name"]: d["subjects"] for d in documents if d["kind"] == "RoleBinding"}
    assert bound == {
        "account-sign-ins": [
            {"kind": "ServiceAccount", "name": CONSOLE_ACCOUNT, "namespace": "engineering-control"}
        ],
        "launcher": [
            {"kind": "ServiceAccount", "name": LAUNCHER_ACCOUNT, "namespace": "engineering-control"}
        ],
    }
    # What a sandbox runs as has no token and no role.
    (account,) = [d for d in documents if d["kind"] == "ServiceAccount"]
    assert account["metadata"]["name"] == "sandbox"
    assert account["automountServiceAccountToken"] is False
    # The cluster's ceiling for all sandboxes together leaves the launcher's own limit room
    # and stays within the node (12 processors, 30 GiB).
    (quota,) = [d for d in documents if d["kind"] == "ResourceQuota"]
    hard = quota["spec"]["hard"]
    settings = next(
        d for d in _documents() if d["metadata"]["name"] == "engineering-control-settings"
    )
    parallel = int(settings["data"]["ENGINEERING_SANDBOX_PARALLEL_STEPS"])
    assert Settings(sandbox_parallel_steps=parallel).sandbox_parallel_steps == 4
    assert int(hard["pods"]) >= parallel + 2
    assert int(hard["limits.memory"].removesuffix("Gi")) >= parallel * 6
    assert int(hard["limits.memory"].removesuffix("Gi")) <= 30 and int(hard["limits.cpu"]) <= 12
    # Nothing runs here by manifest: sandboxes are started by the platform, one per run.
    assert not [d for d in documents if d["kind"] in {"Deployment", "Job", "StatefulSet", "Pod"}]


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
    [certificate] = [
        d for d in documents if d["kind"] == "Certificate" and "ClusterIssuer" in str(d["spec"])
    ]
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


def test_sandboxes_reach_the_proxy_and_the_proxy_reaches_public_https_only() -> None:
    documents = _documents()
    [policy] = [
        d["spec"]
        for d in documents
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "egress-proxy"
    ]
    assert policy["podSelector"] == {"matchLabels": EGRESS_LABELS}
    assert policy["policyTypes"] == ["Ingress", "Egress"]
    # In: sandboxes whose step may reach out, on the proxy's port, from no other namespace.
    assert policy["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": SANDBOX_NAMESPACE}
                    },
                    "podSelector": {"matchLabels": {"engineering-control/egress": "internet"}},
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8888}],
        }
    ]
    # Out: name resolution and HTTPS to public addresses.
    names, public = policy["egress"]
    assert {port["port"] for port in names["ports"]} == {53}
    assert names["to"][0]["podSelector"] == {"matchLabels": {"k8s-app": "kube-dns"}}
    assert public["ports"] == [{"protocol": "TCP", "port": 443}]
    (block,) = public["to"]
    assert block["ipBlock"]["cidr"] == "0.0.0.0/0"
    for private in PRIVATE_NETWORKS:
        assert private in block["ipBlock"]["except"], private
    # The proxy itself: the platform's image, no identity, no volume but /tmp and the one
    # key grants are checked with (and no other of the platform's secrets), never on the
    # sandboxes' node; its service is what the launcher hands to sandboxes.
    [(name, spec)] = [(n, s) for n, s in _pod_specs() if n.endswith("-egress")]
    assert name == "Deployment/engineering-control-egress"
    assert spec["serviceAccountName"] == "engineering-control"
    assert spec["volumes"] == [
        {"name": "tmp", "emptyDir": {}},
        {
            "name": "grant-key",
            "secret": {
                "secretName": "engineering-control-secrets",
                "defaultMode": 0o440,
                "optional": True,
                "items": [{"key": "egress-grant.key", "path": "egress-grant.key"}],
            },
        },
    ]
    [container] = spec["containers"]
    assert container["command"] == ["engineering-egress"]
    assert "env" not in container
    assert container["ports"] == [{"name": "proxy", "containerPort": 8888}]
    deployment = next(
        d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"] == name[11:]
    )
    assert deployment["spec"]["template"]["metadata"]["labels"] == EGRESS_LABELS
    service = next(
        d
        for d in documents
        if d["kind"] == "Service" and d["metadata"]["name"] == "engineering-control-egress"
    )
    assert service["spec"]["selector"] == EGRESS_LABELS
    assert service["spec"]["ports"] == [{"name": "proxy", "port": 8888, "targetPort": "proxy"}]
    data = next(
        d["data"]
        for d in documents
        if d["kind"] == "ConfigMap" and "ENGINEERING_DASHBOARD_HOST" in d["data"]
    )
    assert data["ENGINEERING_SANDBOX_EGRESS_PROXY"] == "http://engineering-control-egress:8888"
    settings = Settings(
        network_boundary="cluster",
        sandbox_egress_host=data["ENGINEERING_SANDBOX_EGRESS_HOST"],
        sandbox_egress_proxy=data["ENGINEERING_SANDBOX_EGRESS_PROXY"],
    )
    assert settings.sandbox_egress_port == 8888


def test_only_a_turn_of_the_supervisor_reaches_the_tool_service_from_outside() -> None:
    documents = _documents()
    [policy] = [
        d["spec"]
        for d in documents
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "supervisor-tools"
    ]
    assert policy["podSelector"] == {"matchLabels": TOOLS_LABELS}
    assert policy["policyTypes"] == ["Ingress"]
    assert policy["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": SANDBOX_NAMESPACE}
                    },
                    "podSelector": {"matchLabels": {"engineering-control/tools": "supervisor"}},
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8790}],
        }
    ]
    # The service: the platform's image, the database, no identity towards the cluster,
    # never on the sandboxes' node.
    [(name, spec)] = [(n, s) for n, s in _pod_specs() if n.endswith("-tools")]
    assert name == "Deployment/engineering-control-tools"
    assert spec["serviceAccountName"] == "engineering-control"
    [container] = spec["containers"]
    assert container["command"] == ["engineering-tools"]
    assert container["ports"] == [{"name": "tools", "containerPort": 8790}]
    assert {entry["name"] for entry in container["env"]} == {
        "DATABASE_USER",
        "DATABASE_PASSWORD",
        "ENGINEERING_DATABASE_URL",
    }
    deployment = next(
        d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"] == name[11:]
    )
    assert deployment["spec"]["template"]["metadata"]["labels"] == TOOLS_LABELS
    service = next(
        d
        for d in documents
        if d["kind"] == "Service" and d["metadata"]["name"] == "engineering-control-tools"
    )
    assert service["spec"]["selector"] == TOOLS_LABELS
    assert service["spec"]["ports"] == [{"name": "tools", "port": 8790, "targetPort": "tools"}]
    data = next(
        d["data"]
        for d in documents
        if d["kind"] == "ConfigMap" and "ENGINEERING_DASHBOARD_HOST" in d["data"]
    )
    assert data["ENGINEERING_SUPERVISOR_TOOLS_URL"] == "http://engineering-control-tools:8790"
    settings = Settings(
        network_boundary="cluster",
        supervisor_tools_host=data["ENGINEERING_SUPERVISOR_TOOLS_HOST"],
        supervisor_tools_url=data["ENGINEERING_SUPERVISOR_TOOLS_URL"],
    )
    assert settings.supervisor_tools_port == 8790
    with pytest.raises(ValueError, match="tool service"):
        Settings(supervisor_tools_host="0.0.0.0")  # noqa: S104 - refused outside a cluster


def test_the_chat_channel_reads_alone_holds_the_bots_token_and_has_no_listener() -> None:
    documents = _documents()
    [(name, spec)] = [(n, s) for n, s in _pod_specs() if n.endswith("-chat")]
    assert name == "Deployment/engineering-control-chat"
    deployment = next(
        d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"] == name[11:]
    )
    # Telegram gives each update to one reader: never two at the same time.
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    [container] = spec["containers"]
    assert container["command"] == ["engineering-chat"]
    assert "ports" not in container
    assert not [
        d for d in documents if d["kind"] == "Service" and d["metadata"]["name"].endswith("-chat")
    ]
    # The bot's token is mounted into this process and no other; a cluster where the
    # administrator has not made the Secret yet still starts.
    holders = {
        pod: volume
        for pod, pod_spec in _pod_specs()
        for volume in pod_spec.get("volumes", [])
        if volume.get("secret", {}).get("secretName") == "engineering-control-telegram"
    }
    assert set(holders) == {name}
    volume = holders[name]
    assert volume["secret"]["optional"] is True
    assert volume["secret"]["items"] == [{"key": "bot-token", "path": "bot-token"}]
    mount = next(m for m in container["volumeMounts"] if m["name"] == volume["name"])
    assert mount["readOnly"] is True
    data = next(
        d["data"]
        for d in documents
        if d["kind"] == "ConfigMap" and "ENGINEERING_DASHBOARD_HOST" in d["data"]
    )
    assert data["ENGINEERING_TELEGRAM_TOKEN_FILE"] == f"{mount['mountPath']}/bot-token"
    host = data["ENGINEERING_CONSOLE_PUBLIC_URL"].removeprefix("https://")
    settings = Settings(
        network_boundary="cluster",
        telegram_administrator_id=int(data["ENGINEERING_TELEGRAM_ADMINISTRATOR_ID"]),
        telegram_token_file=data["ENGINEERING_TELEGRAM_TOKEN_FILE"],
        console_public_url=data["ENGINEERING_CONSOLE_PUBLIC_URL"],
        chat_digest_hour=int(data["ENGINEERING_CHAT_DIGEST_HOUR"]),
    )
    assert host in settings.dashboard_allowed_hosts
    # The token is never a setting: no variable of any pod carries it.
    for _pod, pod_spec in _pod_specs():
        for item in pod_spec["containers"]:
            assert not [e for e in item.get("env", []) if "TELEGRAM" in e["name"]]


def test_only_the_nodes_reach_the_console_from_outside_the_namespace() -> None:
    documents = _documents()
    policies = [
        d
        for d in documents
        if d["kind"] == "NetworkPolicy"
        # The two a sandbox may reach have tests of their own.
        and d["metadata"]["name"] not in {"egress-proxy", "supervisor-tools"}
    ]
    assert policies and all(p["spec"]["policyTypes"] == ["Ingress"] for p in policies)
    same_namespace = [{"from": [{"podSelector": {}}]}]
    [policy] = [p for p in policies if p["spec"]["ingress"] != same_namespace]
    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "engineering-control",
        "app.kubernetes.io/component": "console",
    }
    [rule] = policy["spec"]["ingress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8750}]
    # Single addresses only: no range that could hold a pod, no selector, no exception.
    assert rule["from"] and all(set(peer) == {"ipBlock"} for peer in rule["from"])
    addresses = [peer["ipBlock"]["cidr"] for peer in rule["from"]]
    assert all(set(peer["ipBlock"]) == {"cidr"} for peer in rule["from"])
    assert all(cidr.endswith("/32") for cidr in addresses) and len(set(addresses)) == len(addresses)
    # The same addresses, and only they, may name the client a sign-in is counted against.
    settings = next(
        d["data"]
        for d in documents
        if d["kind"] == "ConfigMap" and "ENGINEERING_DASHBOARD_HOST" in d["data"]
    )
    trusted = json.loads(settings["ENGINEERING_DASHBOARD_TRUSTED_PROXY_NETWORKS"])
    assert sorted(trusted) == sorted(addresses)


def test_what_holds_a_data_volume_and_is_no_longer_used_is_kept_apart() -> None:
    """A deployment never deletes a volume by itself: what the original's way of working
    left behind stays, in a file of its own, until the administrator removes it."""
    leftovers = [d for d in yaml.safe_load_all((BASE / "leftovers.yaml").read_text()) if d]
    assert [(d["kind"], d["metadata"]["name"]) for d in leftovers] == [
        ("PersistentVolumeClaim", "engineering-control-evidence"),
        ("Cluster", "engineering-control-temporal-postgres"),
    ]
    # Nothing of the platform mounts a claim or names that database.
    documents = _documents()
    for document in documents:
        if document["kind"] in {"Deployment", "StatefulSet", "Job"}:
            volumes = document["spec"]["template"]["spec"].get("volumes", [])
            assert not any("persistentVolumeClaim" in volume for volume in volumes)
    others = "\n".join(
        path.read_text() for path in BASE.glob("*.yaml") if path.name != "leftovers.yaml"
    )
    assert "temporal" not in others.lower() and "evidence" not in others.lower()


def test_no_storage_class_is_named() -> None:
    # The class taken from another application's settings did not exist on the cluster;
    # the default class is the one known to work.
    text = "\n".join(path.read_text() for path in BASE.glob("*.yaml"))
    assert "storageClassName:" not in text and "storageClass:" not in text


def test_the_registry_chart_holds_no_credential() -> None:
    values = yaml.safe_load((BASE.parent / "registry" / "values.yaml").read_text())
    assert values == {"username": "", "token": "", "sandboxNamespace": SANDBOX_NAMESPACE}


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
    assert len(pods) >= 8
    for name, spec in pods:
        assert spec.get("affinity", {}).get("nodeAffinity") == rule, name
        assert "nodeSelector" not in spec and "tolerations" not in spec, name
    clusters = [d for d in _documents() if d["kind"] == "Cluster"]
    assert len(clusters) == 2
    for cluster in clusters:
        name = cluster["metadata"]["name"]
        assert cluster["spec"]["affinity"]["nodeAffinity"] == rule, name
        assert "tolerations" not in cluster["spec"]["affinity"], name


def test_only_three_processes_hold_the_github_app_key_and_none_its_client_secret() -> None:
    documents = _documents()
    settings = next(
        d["data"]
        for d in documents
        if d["kind"] == "ConfigMap" and "ENGINEERING_DASHBOARD_HOST" in d["data"]
    )
    key_file = settings["ENGINEERING_GITHUB_APP_PRIVATE_KEY_FILE"]
    holders = {}
    for pod, spec in _pod_specs():
        for volume in spec.get("volumes", []):
            if volume.get("secret", {}).get("secretName") == "engineering-control-github-app":
                holders[pod] = volume
    # The console reads repositories and issues; the launcher checks out and publishes;
    # the supervisor's tools read the text of an issue.
    assert set(holders) == {
        "Deployment/engineering-control-console",
        "Deployment/engineering-control-launcher",
        "Deployment/engineering-control-tools",
    }
    for pod, volume in holders.items():
        assert volume["secret"]["items"] == [{"key": "private-key.pem", "path": "private-key.pem"}]
        # The administrator creates this Secret; a cluster without it must still start.
        assert volume["secret"]["optional"] is True
        spec = dict(_pod_specs())[pod]
        mounts = {m["name"]: m for c in spec["containers"] for m in c["volumeMounts"]}
        assert f"{mounts[volume['name']]['mountPath']}/private-key.pem" == key_file
        assert mounts[volume["name"]]["readOnly"] is True
