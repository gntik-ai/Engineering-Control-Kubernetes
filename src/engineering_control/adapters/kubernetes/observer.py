from __future__ import annotations

from pathlib import Path

from engineering_control.adapters.command import CommandResult, SafeCommandRunner


class KubernetesObserver:
    """Read-only kubectl façade; Secret retrieval and all mutation verbs are rejected."""

    _resources = {
        "pods",
        "deployments",
        "statefulsets",
        "daemonsets",
        "services",
        "ingresses",
        "jobs",
        "cronjobs",
        "events",
        "applications.argoproj.io",
        "externalsecrets.external-secrets.io",
    }

    def __init__(
        self,
        runner: SafeCommandRunner,
        *,
        kubeconfig: Path,
        context: str,
        working_directory: Path,
    ) -> None:
        self.runner = runner
        self.kubeconfig = kubeconfig
        self.context = context
        self.working_directory = working_directory

    def get(self, resource: str, *, namespace: str, name: str | None = None) -> CommandResult:
        if resource not in self._resources:
            raise ValueError("resource is not exposed by the observer façade")
        arguments = self._base() + ["get", resource]
        if name:
            arguments.append(name)
        arguments.extend(("--namespace", namespace, "--output", "json"))
        return self.runner.run("kubectl", arguments, cwd=self.working_directory)

    def logs(
        self, pod: str, *, namespace: str, container: str | None = None, tail: int = 500
    ) -> CommandResult:
        if not 1 <= tail <= 5000:
            raise ValueError("observer log tail must be between 1 and 5000")
        arguments = self._base() + [
            "logs",
            pod,
            "--namespace",
            namespace,
            "--tail",
            str(tail),
        ]
        if container:
            arguments.extend(("--container", container))
        return self.runner.run("kubectl", arguments, cwd=self.working_directory)

    def verify_permissions(
        self,
        namespaces: tuple[str, ...],
        observed_resources: tuple[str, ...] = (),
    ) -> None:
        namespace_resources = set(observed_resources) | {
            "*",
            "configmaps",
            "pods",
            "secrets",
            "serviceaccounts",
            "roles.rbac.authorization.k8s.io",
            "rolebindings.rbac.authorization.k8s.io",
        }
        forbidden = {
            *(verb_resource for verb_resource in (
                ("get", "secrets"),
                ("list", "secrets"),
                ("watch", "secrets"),
            )),
            *(
                (verb, resource)
                for verb in ("create", "update", "patch", "delete", "deletecollection")
                for resource in namespace_resources
            ),
        }
        for namespace in namespaces:
            for verb, resource in sorted(forbidden):
                self._require_denied(verb, resource, namespace=namespace)
        cluster_forbidden = {
            ("create", "*"),
            ("update", "*"),
            ("patch", "*"),
            ("delete", "*"),
            ("deletecollection", "*"),
            ("create", "namespaces"),
            ("delete", "namespaces"),
            ("create", "clusterroles.rbac.authorization.k8s.io"),
            ("create", "clusterrolebindings.rbac.authorization.k8s.io"),
            ("create", "customresourcedefinitions.apiextensions.k8s.io"),
            ("impersonate", "users"),
            ("impersonate", "groups"),
            ("impersonate", "serviceaccounts"),
            ("escalate", "roles.rbac.authorization.k8s.io"),
            ("escalate", "clusterroles.rbac.authorization.k8s.io"),
            ("bind", "roles.rbac.authorization.k8s.io"),
            ("bind", "clusterroles.rbac.authorization.k8s.io"),
        }
        for verb, resource in sorted(cluster_forbidden):
            self._require_denied(verb, resource)

    def _require_denied(self, verb: str, resource: str, *, namespace: str | None = None) -> None:
        arguments = self._base() + ["auth", "can-i", verb, resource]
        if namespace is not None:
            arguments.extend(("--namespace", namespace))
        result = self.runner.run(
            "kubectl",
            arguments,
            cwd=self.working_directory,
            check=False,
        )
        decision = result.stdout.strip().lower()
        if decision == "yes":
            location = f" in {namespace}" if namespace is not None else " cluster-wide"
            raise PermissionError(
                f"observer credential is overprivileged: {verb} {resource}{location}"
            )
        if decision != "no":
            raise PermissionError(
                "observer credential authorization could not be proven least-privileged"
            )

    def _base(self) -> list[str]:
        if not self.kubeconfig.is_file():
            raise ValueError("observer kubeconfig does not exist")
        return ["--kubeconfig", str(self.kubeconfig), "--context", self.context]
