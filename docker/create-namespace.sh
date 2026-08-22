#!/bin/sh
set -eu

if temporal operator namespace describe --namespace "${TEMPORAL_NAMESPACE}" >/dev/null 2>&1; then
  echo "Temporal namespace already exists"
else
  temporal operator namespace create --namespace "${TEMPORAL_NAMESPACE}" --retention 30d
fi
