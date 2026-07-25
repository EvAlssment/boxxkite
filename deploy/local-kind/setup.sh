#!/usr/bin/env bash
# Setup a local kind cluster for sandbox K8s testing.
#
# Usage:
#   ./deploy/local-kind/setup.sh          # Full setup (create cluster + build + apply)
#   ./deploy/local-kind/setup.sh reload   # Rebuild images and reload into existing cluster
#
# After running this script, start the kubectl proxy and your application:
#   kubectl proxy --context kind-boxkite-dev --reject-paths='' &
#   # Then start whatever process embeds boxkite.SandboxManager

set -euo pipefail

CLUSTER_NAME="boxkite-dev"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*" >&2; }

# ── Prerequisites ──────────────────────────────────────────────────────────
check_prereqs() {
    if ! command -v docker &>/dev/null; then
        error "docker not found. Install Docker Desktop first."
        exit 1
    fi
    if ! docker info &>/dev/null 2>&1; then
        error "Docker daemon not running. Start Docker Desktop first."
        exit 1
    fi
    if ! command -v kind &>/dev/null; then
        warn "kind not found. Installing via brew..."
        brew install kind
    fi
    if ! command -v kubectl &>/dev/null; then
        error "kubectl not found. Install kubectl first."
        exit 1
    fi
}

# ── Cluster ────────────────────────────────────────────────────────────────
create_cluster() {
    if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
        info "Cluster '${CLUSTER_NAME}' already exists"
        kubectl cluster-info --context "kind-${CLUSTER_NAME}" >/dev/null 2>&1 || {
            error "Cluster exists but is unreachable. Run: kind delete cluster --name ${CLUSTER_NAME}"
            exit 1
        }
    else
        info "Creating kind cluster '${CLUSTER_NAME}'..."
        kind create cluster --name "$CLUSTER_NAME"
    fi
    kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null 2>&1
}

# ── Images ─────────────────────────────────────────────────────────────────
build_and_load_images() {
    cd "$REPO_ROOT"

    info "Building sidecar image..."
    docker build -f deploy/sidecar.Dockerfile -t boxkite-sidecar:local . 2>&1 | tail -1

    info "Building sandbox image (this takes a few minutes the first time)..."
    docker build -f deploy/sandbox.Dockerfile -t boxkite-sandbox:local . 2>&1 | tail -1

    info "Loading sidecar image into kind..."
    kind load docker-image boxkite-sidecar:local --name "$CLUSTER_NAME"

    info "Loading sandbox image into kind (~5GB, may take a minute)..."
    kind load docker-image boxkite-sandbox:local --name "$CLUSTER_NAME"
}

# ── K8s Resources ──────────────────────────────────────────────────────────
apply_resources() {
    info "Applying K8s resources (RBAC, ConfigMap, Secret, PriorityClasses)..."
    kubectl apply -f "$SCRIPT_DIR/k8s-resources.yaml" --context "kind-${CLUSTER_NAME}"
}

# ── Main ───────────────────────────────────────────────────────────────────
main() {
    check_prereqs

    if [[ "${1:-}" == "reload" ]]; then
        info "Reload mode: rebuilding and reloading images into existing cluster"
        build_and_load_images
        apply_resources
    else
        create_cluster
        build_and_load_images
        apply_resources
    fi

    echo ""
    info "Kind cluster '${CLUSTER_NAME}' is ready!"
    echo ""
    echo "  Required environment variables for your process:"
    echo "    RUNTIME_MODE=k8s"
    echo "    SANDBOX_IMAGE=boxkite-sandbox:local"
    echo "    SIDECAR_IMAGE=boxkite-sidecar:local"
    echo "    WARM_POOL_SIZE=1"
    echo "    WARM_POOL_MAX=5"
    echo "    SANDBOX_USE_K8S_PROXY=true"
    echo ""
    echo "  Before starting your process, run kubectl proxy in background:"
    echo "    kubectl proxy --context kind-${CLUSTER_NAME} --reject-paths='' &"
    echo ""
    echo "  Warm pool pods will appear in: kubectl get pods -l app=sandbox"
    echo ""
    echo "  To tear down: ./deploy/local-kind/teardown.sh"
}

main "$@"
