# Local kind cluster

Use this guide when you want the actual Kubernetes pod-per-session runtime on
your laptop. The detailed setup and its architecture-specific limitations
remain in [`deploy/local-kind/README.md`](../../deploy/local-kind/README.md).

```bash
./deploy/local-kind/setup.sh
kubectl proxy --context kind-boxxkite-dev --reject-paths='' &
export SANDBOX_IMAGE=boxxkite-sandbox:local
export SIDECAR_IMAGE=boxxkite-sidecar:local
export SANDBOX_USE_K8S_PROXY=true
export RUNTIME_MODE=k8s
```

The setup script builds and loads the images, creates the kind cluster, and
applies the cluster resources. Verify that warm pods appear before creating a
session:

```bash
kubectl get pods -l app=sandbox,pool=warm --context kind-boxxkite-dev
```

This is a real Kubernetes path, not a faster version of the Compose
quickstart. For the shortest local trial, return to [Getting started](../getting-started/).
