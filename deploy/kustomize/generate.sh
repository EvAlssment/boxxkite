#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "${script_dir}/../.." && pwd)
chart_dir="${repo_root}/deploy/helm/boxxkite"
output_file="${script_dir}/base/helm-rendered.yaml"
helm_bin=${HELM_BIN:-helm}

render_chart() {
  "${helm_bin}" template boxxkite "${chart_dir}" \
    --namespace boxxkite \
    --set namespace=boxxkite
}

case "${1:-write}" in
  write)
    render_chart >"${output_file}"
    printf 'Wrote %s\n' "${output_file}"
    ;;
  check)
    if [[ ! -f "${output_file}" ]]; then
      printf 'Missing generated base: %s\n' "${output_file}" >&2
      exit 1
    fi
    rendered=$(mktemp)
    trap 'rm -f "${rendered}"' EXIT
    render_chart >"${rendered}"
    diff -u "${output_file}" "${rendered}"
    ;;
  *)
    printf 'Usage: %s [write|check]\n' "$0" >&2
    exit 2
    ;;
esac
