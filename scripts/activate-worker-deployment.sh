#!/usr/bin/env bash
# F-11 (a): promote a build_id's Worker Deployment Version to "current" so its
# workers actually start receiving new workflow executions.
#
# Setting ENGINEERING_BUILD_ID and restarting the temporal-worker/role-worker
# services is NOT enough on its own: Temporal's Worker Deployments feature
# deliberately does not route new work to a version until it is explicitly
# promoted (this is the safety property of the feature, not a bug). Skipping
# this step after enabling versioning means the new workers come up healthy,
# poll successfully, and silently process zero new workflow starts.
#
# Run this ONLY after the new build's workers are already up and polling
# (give them a few seconds after a restart) -- Temporal needs to have seen at
# least one poll from the new version before it can be set current.
#
# CAUTION: Temporal's own CLI flags Worker Deployments as an experimental
# feature -- behavior/flags may change in a future temporalio/temporal-cli
# release. Re-check `temporal worker deployment --help` if this script starts
# failing after an upgrade.
#
# Usage: scripts/activate-worker-deployment.sh <build-id> [--address host:port] [--namespace name]
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <build-id> [--address host:port] [--namespace name]" >&2
  exit 1
fi

BUILD_ID="$1"
shift
ADDRESS="127.0.0.1:7233"
NAMESPACE="engineering-control"
DEPLOYMENT_NAME="engineering-control"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --address)
      ADDRESS="$2"
      shift 2
      ;;
    --namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

TEMPORAL=$(command -v temporal || echo "$HOME/.local/bin/temporal")

echo "Deployment: $DEPLOYMENT_NAME  Build ID: $BUILD_ID  Address: $ADDRESS  Namespace: $NAMESPACE"
echo ""
echo "Current state before activation:"
"$TEMPORAL" worker deployment describe \
  --address "$ADDRESS" --namespace "$NAMESPACE" \
  --name "$DEPLOYMENT_NAME" 2>&1 || echo "(no existing deployment yet -- fine for a first activation)"

echo ""
echo "Promoting $BUILD_ID to current..."
"$TEMPORAL" worker deployment set-current-version \
  --address "$ADDRESS" --namespace "$NAMESPACE" \
  --deployment-name "$DEPLOYMENT_NAME" --build-id "$BUILD_ID" \
  --yes

echo ""
echo "Verifying:"
"$TEMPORAL" worker deployment describe-version \
  --address "$ADDRESS" --namespace "$NAMESPACE" \
  --deployment-name "$DEPLOYMENT_NAME" --build-id "$BUILD_ID"

echo ""
echo "Done. New workflow executions on this deployment now route to build $BUILD_ID."
