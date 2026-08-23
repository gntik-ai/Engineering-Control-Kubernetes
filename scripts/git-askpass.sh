#!/usr/bin/env bash
set -euo pipefail

case ${1:-} in
  *Username*) printf '%s\n' x-access-token ;;
  *Password*)
    if [[ -n ${ENGINEERING_GITHUB_TOKEN:-} ]]; then
      printf '%s\n' "${ENGINEERING_GITHUB_TOKEN}"
    elif [[ -n ${ENGINEERING_GITHUB_TOKEN_FILE:-} ]]; then
      IFS= read -r token <"${ENGINEERING_GITHUB_TOKEN_FILE}"
      [[ -n ${token} ]]
      printf '%s\n' "${token}"
    else
      exit 1
    fi
    ;;
  *) exit 1 ;;
esac
