#!/usr/bin/env bash
# Read-only inventory of the cluster for section 16.3 of the architecture study.
# It only runs `kubectl get` and never reads Secret contents. Run it with a kubeconfig that
# points at the cluster, and share the output.
set -uo pipefail

section() { printf '\n===== %s =====\n' "$1"; }

section "Nodes"
kubectl get nodes -o wide
section "Node labels and taints"
kubectl get nodes -o custom-columns='NAME:.metadata.name,TAINTS:.spec.taints[*].key,ROLES:.metadata.labels.node\.kubernetes\.io/instance-type,ZONE:.metadata.labels.topology\.kubernetes\.io/zone'
section "k3s server and agent arguments (shows disabled components)"
kubectl get nodes -o custom-columns='NAME:.metadata.name,ARGS:.metadata.annotations.k3s\.io/node-args'
section "Allocatable and usage"
kubectl get nodes -o custom-columns='NAME:.metadata.name,CPU:.status.allocatable.cpu,MEMORY:.status.allocatable.memory,PODS:.status.allocatable.pods,DISK:.status.allocatable.ephemeral-storage'
kubectl top nodes 2>/dev/null || echo "(metrics not available)"
section "Namespaces"
kubectl get namespaces
section "Storage classes"
kubectl get storageclass
section "Ingress classes and controllers"
kubectl get ingressclass
kubectl get pods --all-namespaces -o wide | grep -Ei 'traefik|nginx|ingress|haproxy' || echo "(no ingress controller pods matched)"
section "Runtime classes (sandboxed runtimes)"
kubectl get runtimeclass 2>/dev/null || echo "(none)"
section "Network policy controller and CNI"
kubectl -n kube-system get pods -o wide | grep -Ei 'flannel|calico|cilium|kube-router|canal|network' || echo "(no CNI pods matched; k3s embeds flannel and its policy controller in the server process)"
kubectl get networkpolicy --all-namespaces --no-headers 2>/dev/null | wc -l | xargs echo "network policies defined:"
section "Operators (custom resource groups)"
kubectl get crd -o custom-columns='GROUP:.spec.group' --no-headers | sort | uniq -c | sort -rn
section "Secret stores and certificate issuers (names only)"
kubectl get clustersecretstore,secretstore --all-namespaces 2>/dev/null || true
kubectl get clusterissuer,issuer --all-namespaces 2>/dev/null || true
section "PostgreSQL clusters managed by the operator"
kubectl get clusters.postgresql.cnpg.io --all-namespaces 2>/dev/null || echo "(none)"
section "Object storage candidates"
kubectl get pods --all-namespaces | grep -Ei 'minio|seaweed|garage|rook|ceph' || echo "(none matched)"
section "Resource quotas, limit ranges and priority classes"
kubectl get resourcequota,limitrange --all-namespaces 2>/dev/null || true
kubectl get priorityclass
section "Pod security admission labels"
kubectl get namespaces -o custom-columns='NAME:.metadata.name,ENFORCE:.metadata.labels.pod-security\.kubernetes\.io/enforce'
section "Kernel, OS and container runtime per node"
kubectl get nodes -o custom-columns='NAME:.metadata.name,KERNEL:.status.nodeInfo.kernelVersion,OS:.status.nodeInfo.osImage,RUNTIME:.status.nodeInfo.containerRuntimeVersion'
