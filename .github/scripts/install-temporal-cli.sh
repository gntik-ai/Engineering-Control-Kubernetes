#!/usr/bin/env bash
# Install a pinned Temporal CLI into ~/.local/bin after verifying its checksum.
set -euo pipefail

version="1.10.0"
sha256="521f461f3d108902763bb71e85cd0fd382dc9357b59604ae03da6ae734d16890"
archive="temporal_cli_${version}_linux_amd64.tar.gz"
target="${HOME}/.local/bin"
download="${RUNNER_TEMP:-/tmp}/${archive}"

curl --fail --silent --show-error --location --output "${download}" \
  "https://github.com/temporalio/cli/releases/download/v${version}/${archive}"
echo "${sha256}  ${download}" | sha256sum --check --strict
mkdir -p "${target}"
tar --extract --gzip --file "${download}" --directory "${target}" temporal
if [[ -n "${GITHUB_PATH:-}" ]]; then
  echo "${target}" >> "${GITHUB_PATH}"
fi
