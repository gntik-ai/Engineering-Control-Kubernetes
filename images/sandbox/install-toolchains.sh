#!/usr/bin/env bash
# Download the toolchains named in a lock file, verify each archive against its pinned
# SHA-256, and unpack the one directory it holds.
#
# Usage: install-toolchains.sh <toolchains.lock> <target directory>
set -euo pipefail

lock="${1:?usage: install-toolchains.sh <toolchains.lock> <target directory>}"
target="${2:?usage: install-toolchains.sh <toolchains.lock> <target directory>}"
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
  mapfile -t found < <(find "${work}/${name}" -mindepth 1 -maxdepth 1)
  if [[ ${#found[@]} -ne 1 || "${found[0]}" != "${work}/${name}/${name}" || ! -d "${found[0]}" ]]; then
    echo "${name}: expected the archive to hold one directory named '${name}'" >&2
    exit 1
  fi
  rm -rf -- "${target:?}/${name}"
  mv -- "${found[0]}" "${target}/${name}"
  # Nothing in a toolchain may raise privileges or be writable by another user.
  chmod -R go-w,a-s -- "${target}/${name}"
  echo "installed ${name} ${version}"
  installed=$((installed + 1))
done <"${lock}"

if [[ ${installed} -eq 0 ]]; then
  echo "no toolchains named in ${lock}" >&2
  exit 1
fi
