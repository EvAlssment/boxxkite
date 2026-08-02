#!/usr/bin/env bash
# Tear down the local kind cluster for sandbox testing.
# Usage: ./deploy/local-kind/teardown.sh

set -euo pipefail

CLUSTER_NAME="boxxkite-dev"

echo "[+] Deleting kind cluster '${CLUSTER_NAME}'..."
kind delete cluster --name "$CLUSTER_NAME" 2>/dev/null || true

echo "[+] Cleaning up local images (optional)..."
read -rp "    Remove local boxxkite-sandbox:local and boxxkite-sidecar:local images? [y/N] " answer
if [[ "${answer,,}" == "y" ]]; then
    docker rmi boxxkite-sandbox:local boxxkite-sidecar:local 2>/dev/null || true
    echo "    Images removed."
else
    echo "    Keeping images (faster rebuild next time)."
fi

echo ""
echo "[+] Done. Remember to switch your environment back to compose mode:"
echo "    RUNTIME_MODE=compose"
echo "    SIDECAR_URL=http://localhost:8080"
echo ""
echo "    And unset these (set by setup.sh):"
echo "    SANDBOX_IMAGE, SIDECAR_IMAGE, WARM_POOL_SIZE, WARM_POOL_MAX, SANDBOX_USE_K8S_PROXY"
