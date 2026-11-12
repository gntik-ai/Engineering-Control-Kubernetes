#!/usr/bin/env bash
# Download the tools named in a lock file, verify each archive against its pinned
# SHA-256, and install the one binary it holds.
#
# Usage: install-tools.sh <tools.lock> <target directory>
set -euo pipefail

lock="${1:?usage: install-tools.sh <tools.lock> <target directory>}"
target="${2:?usage: install-tools.sh <tools.lock> <target directory>}"
work="$(mktemp -d)"
trap 'rm -rf -- "${work}"' EXIT
install -d -m 0755 "${target}"

installed=0
while read -r name version sha256 url rest; do
  if [[ -z "${name}" || "${name}" == \#* ]]; then continue; fi
  if [[ -n "${rest}" || ! "${name}" =~ ^[a-z][a-z0-9-]*$ || ! "${version}" =~ ^[0-9]+(\.[0-9]+)+$ \
    || ! "${sha256}" =~ ^[a-f0-9]{64}$ || "${url}" != https://* ]]; then
    echo "malformed line for '${name}' in ${lock}" >&2
    exit 1
  fi
  archive="${work}/${name}.tar.gz"
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --retry 3 --output "${archive}" "${url}"
  echo "${sha256}  ${archive}" | sha256sum --check --strict --quiet
  mkdir "${work}/${name}"
  tar --extract --gzip --file "${archive}" --directory "${work}/${name}" --no-same-owner
  mapfile -t found < <(find "${work}/${name}" -type f -name "${name}")
  if [[ ${#found[@]} -ne 1 ]]; then
    echo "${name}: expected one file named '${name}' in the archive, found ${#found[@]}" >&2
    exit 1
  fi
  install -m 0755 "${found[0]}" "${target}/${name}"
  echo "installed ${name} ${version}"
  installed=$((installed + 1))
done <"${lock}"

if [[ ${installed} -eq 0 ]]; then
  echo "no tools named in ${lock}" >&2
  exit 1
fi
