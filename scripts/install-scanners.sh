#!/usr/bin/env bash
set -euo pipefail
umask 027

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi
for command_name in curl sha256sum tar python3; do
  command -v "${command_name}" >/dev/null || {
    echo "Required command missing: ${command_name}" >&2
    exit 1
  }
done

TEMP_ROOT=$(mktemp -d)
trap 'rm -rf -- "${TEMP_ROOT}"' EXIT

install_release_binary() {
  local archive_url=$1
  local checksum_url=$2
  local archive_name=$3
  local binary_name=$4
  curl --fail --silent --show-error --location --output "${TEMP_ROOT}/${archive_name}" \
    "${archive_url}"
  curl --fail --silent --show-error --location --output "${TEMP_ROOT}/checksums.txt" \
    "${checksum_url}"
  local expected
  expected=$(awk -v name="${archive_name}" '$2 == name || $2 == "*" name {print $1}' \
    "${TEMP_ROOT}/checksums.txt" | head -n 1)
  [[ ${expected} =~ ^[a-f0-9]{64}$ ]] || {
    echo "No valid checksum found for ${archive_name}" >&2
    exit 1
  }
  printf '%s  %s\n' "${expected}" "${TEMP_ROOT}/${archive_name}" | sha256sum --check --status
  local extraction="${TEMP_ROOT}/extract-${binary_name}"
  install -d -m 0755 "${extraction}"
  tar -xzf "${TEMP_ROOT}/${archive_name}" -C "${extraction}"
  install -o root -g root -m 0755 "${extraction}/${binary_name}" "/usr/local/bin/${binary_name}"
  rm -f "${TEMP_ROOT}/checksums.txt"
}

install_release_binary \
  https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz \
  https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt \
  gitleaks_8.30.1_linux_x64.tar.gz gitleaks
install_release_binary \
  https://github.com/aquasecurity/trivy/releases/download/v0.74.0/trivy_0.74.0_Linux-64bit.tar.gz \
  https://github.com/aquasecurity/trivy/releases/download/v0.74.0/trivy_0.74.0_checksums.txt \
  trivy_0.74.0_Linux-64bit.tar.gz trivy
install_release_binary \
  https://github.com/yannh/kubeconform/releases/download/v0.8.0/kubeconform-linux-amd64.tar.gz \
  https://github.com/yannh/kubeconform/releases/download/v0.8.0/CHECKSUMS \
  kubeconform-linux-amd64.tar.gz kubeconform
install_release_binary \
  https://github.com/anchore/syft/releases/download/v1.51.0/syft_1.51.0_linux_amd64.tar.gz \
  https://github.com/anchore/syft/releases/download/v1.51.0/syft_1.51.0_checksums.txt \
  syft_1.51.0_linux_amd64.tar.gz syft

python3 -m venv /opt/engineering-scanners/semgrep
/opt/engineering-scanners/semgrep/bin/pip install --disable-pip-version-check semgrep==1.174.0
ln -sfn /opt/engineering-scanners/semgrep/bin/semgrep /usr/local/bin/semgrep
