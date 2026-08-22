# Kubernetes identities

These ClusterRoles are definitions only. Bootstrap does not apply them and creates no
ClusterRoleBinding. Bind them with a namespaced RoleBinding only after the target namespace and
service account are explicitly approved.

- `engineering-observer` excludes Secrets and every mutation verb.
- `engineering-preview-releaser` is bound separately in each pre-created preview namespace.
- `engineering-falcone-staging-releaser` is bound only in the Falcone staging namespace and uses
  `HELM_DRIVER=configmap`; it has no Secret read permission.
- Musematic promotion normally needs GitHub deployment-repository credentials, not direct mutation
  credentials. Runtime verification uses the observer identity.
- No production binding is provided by bootstrap.

The existing shared cluster-admin kubeconfig discovered during inventory is prohibited.
