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

    def verify_permissions(self, namespaces: tuple[str, ...]) -> None:
        forbidden = (
            ("get", "secrets"),
            ("create", "deployments"),
            ("patch", "deployments"),
            ("delete", "namespaces"),
            ("impersonate", "users"),
        )
        for namespace in namespaces:
            for verb, resource in forbidden:
                result = self.runner.run(
                    "kubectl",
                    self._base() + ["auth", "can-i", verb, resource, "--namespace", namespace],
                    cwd=self.working_directory,
                    check=False,
                )
                if result.stdout.strip().lower() == "yes":
                    raise PermissionError(
                        f"observer credential is overprivileged: {verb} {resource} in {namespace}"
                    )

    def _base(self) -> list[str]:
        if not self.kubeconfig.is_file():
            raise ValueError("observer kubeconfig does not exist")
        return ["--kubeconfig", str(self.kubeconfig), "--context", self.context]
