#!/usr/bin/env bash
#
# Live verification harness for the opt-in Kata Containers RuntimeClass
# (docs/KATA-CONTAINERS-SCOPING.md, deploy/pod-template-kata.yaml).
#
# WHY THIS EXISTS (GitHub issue #179, a follow-up to #118)
# --------------------------------------------------------
# The Kata scaffolding (BOXKITE_KATA_RUNTIME_CLASS_ENABLED,
# deploy/pod-template-kata.yaml, tests/test_kata_template_parity.py) is
# "implemented against the real Kubernetes API shape, never exercised against
# a live Kata-enabled cluster" -- the same honesty this repo already applies
# to K8sVolumeProvisioner/KanikoJobBuildRunner. Issue #179 names the three
# concrete questions that ONLY a live Kata node can answer, and that no amount
# of code inspection or plain-runc/kind testing can settle (kind/runc cannot
# reproduce Kata's per-pod VM boundary):
#
#   1. Does a boxkite-shaped pod actually schedule and RUN under
#      runtimeClassName -> kata (and is it genuinely a separate-kernel VM)?
#   2. Does Kata's DEFAULT (non-block, virtio-fs-backed) emptyDir backend
#      honor Kubernetes' emptyDir.sizeLimit? Every DEFAULT_SANDBOX_*_VOLUME_
#      SIZE_LIMIT in resource_config.py is a cross-tenant disk-DoS control; if
#      Kata silently ignores sizeLimit the way its documented block-backed
#      modes do, that control silently stops working the moment
#      runtimeClassName: kata is set -- a security regression, not a cosmetic
#      gap (docs/KATA-CONTAINERS-SCOPING.md §3).
#      ANSWERED 2026-07-16 (issue #179): NOT enforced on a live GKE +
#      kata-deploy v3.32.0 cluster (kata-qemu-runtime-rs) -- guest writes
#      never reach the host directory kubelet measures. See the scoping doc's
#      dated update. The check stays here so OTHER deployments (e.g. AKS's own
#      Cloud Hypervisor/mshv build + config) can be verified the same way.
#   3. Does the sidecar's nsenter/setns() exec mechanism
#      (sidecar/sidecar_execution.py's build_k8s_exec_command) survive Kata's
#      VM boundary? The scoping doc reasons it should (both containers share
#      one guest kernel), but that was paper analysis, not an empirical check.
#
# This script turns "provision a cluster and run the checks" (the issue's own
# suggested next step) into a single, repeatable command. Run it against ANY
# cluster that has a Kata RuntimeClass and at least one Kata-capable node:
#   - an AKS "Pod Sandboxing" node pool (--workload-runtime KataVmIsolation,
#     RuntimeClass kata-vm-isolation), or
#   - a self-managed / kind cluster with nested virtualization + kata-deploy.
#
# WHAT IT DELIBERATELY DOES NOT DO
# --------------------------------
# It does NOT build or pull boxkite's own images. The three questions above
# are properties of the Kata *runtime* + Kubernetes, not of boxkite's
# application code, so this reproduces the security-relevant *pod shape* of
# deploy/pod-template-kata.yaml (runtimeClassName, shareProcessNamespace, the
# sandbox securityContext, the sidecar capability grant, an emptyDir with a
# sizeLimit, the `tail -f /dev/null` sandbox marker get_sandbox_pid() looks
# for) using a stock image with nsenter/dd available. A tiny sizeLimit is used
# so the DoS test fills in seconds instead of writing 5Gi -- the *enforcement
# mechanism* is what is under test, not the specific 1-5Gi production values.
#
# HYPERVISOR CAVEAT: AKS Pod Sandboxing runs the Cloud Hypervisor VMM on the
# Microsoft Hypervisor (mshv) -- NOT QEMU (confirmed by the ".mshv1" guest
# kernel suffix). A self-managed cluster usually runs kata-qemu. The emptyDir
# backend/defaults can differ by config, and AKS ships its own
# configuration.toml, so record which handler you ran against (the summary
# prints it) before generalizing. To read AKS's actual Kata defaults off a
# kata-vm-isolation node (they may override upstream shared-fs defaults):
#   kubectl debug node/<kata-node> -it --image=mcr.microsoft.com/cbl-mariner/busybox:2.0 \
#     -- chroot /host sh -c 'grep -rEh "emptydir_mode|disable_guest_empty_dir|shared_fs" \
#        /opt/kata /etc/kata-containers /opt/confidential-containers 2>/dev/null'
#
# USAGE
# -----
#   ./scripts/verify-kata-live.sh                 # auto-detect kata RuntimeClass, run all 3 checks
#   RUNTIME_CLASS=kata-vm-isolation ./scripts/verify-kata-live.sh
#   WITH_CONTROL=1 ./scripts/verify-kata-live.sh  # ALSO run the same checks under runc as a control
#   RUNTIME_CLASS=runc ./scripts/verify-kata-live.sh   # control only (methodology check; proves nothing about Kata)
#
# Env knobs (all optional): NAMESPACE, IMAGE, SIZE_LIMIT, WRITE_MB,
# READY_TIMEOUT, EVICT_TIMEOUT, KEEP (leave pods for inspection).
#
# Exit code: 0 if every check that ran reached a definitive PASS/ENFORCED
# verdict; non-zero if any check FAILED or could not be determined.

set -euo pipefail

NAMESPACE="${NAMESPACE:-default}"
# Stock image with util-linux (nsenter/unshare) + coreutils (dd/uname).
# mcr mirror avoids Docker Hub anonymous pull-rate limits on the node.
IMAGE="${IMAGE:-mcr.microsoft.com/mirror/docker/library/ubuntu:24.04}"
SIZE_LIMIT="${SIZE_LIMIT:-64Mi}"     # emptyDir sizeLimit under test (small = fast)
WRITE_MB="${WRITE_MB:-128}"          # how much to write (> SIZE_LIMIT) to trip the limit
READY_TIMEOUT="${READY_TIMEOUT:-300s}"   # Kata VM boot is slower than runc
EVICT_TIMEOUT="${EVICT_TIMEOUT:-300}"    # seconds to watch for sizeLimit eviction.
                                         # kubelet's emptyDir accounting is periodic
                                         # (volume-stats-agg-period, often ~60s+), so
                                         # this must comfortably exceed several cycles;
                                         # a still-Running pod at timeout is reported
                                         # INDETERMINATE, never as proof of non-enforcement.
SANDBOX_UID=1001
SANDBOX_GID=1001
PODS_CREATED=()

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
say()     { printf '%s\n' "$*"; }
section() { printf '\n%s=== %s ===%s\n' "$c_dim" "$*" "$c_rst"; }
pass() { printf '%s[PASS]%s %s\n' "$c_grn" "$c_rst" "$*"; }
fail() { printf '%s[FAIL]%s %s\n' "$c_red" "$c_rst" "$*"; }
warn() { printf '%s[WARN]%s %s\n' "$c_yel" "$c_rst" "$*"; }

FAILED=0

cleanup() {
  if [[ "${KEEP:-}" == "1" ]]; then
    warn "KEEP=1 set -- leaving pods for inspection: ${PODS_CREATED[*]:-none}"
    return
  fi
  if [[ ${#PODS_CREATED[@]} -gt 0 ]]; then
    section "teardown"
    kubectl delete pod -n "$NAMESPACE" "${PODS_CREATED[@]}" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    say "deleted: ${PODS_CREATED[*]}"
  fi
}
trap cleanup EXIT

detect_runtime_class() {
  # Pick a Kata RuntimeClass unless RUNTIME_CLASS is set. Both real-world
  # naming schemes must match: AKS Pod Sandboxing ships handler "kata"
  # (RuntimeClass kata-vm-isolation), but the upstream-recommended kata-deploy
  # install ships one RuntimeClass per shim with handler "kata-<shim>"
  # (kata-qemu, kata-qemu-runtime-rs, kata-clh, ...) and never bare "kata" --
  # issue #179's live run hit exactly this: matching only handler=="kata"
  # silently detected nothing on a real kata-deploy cluster. Bare "kata" wins
  # if present; otherwise the first kata-* handler by name (deterministic).
  if [[ -n "${RUNTIME_CLASS:-}" ]]; then echo "$RUNTIME_CLASS"; return; fi
  kubectl get runtimeclass -o json | python3 -c '
import json, sys
items = json.load(sys.stdin)["items"]
cand = [(r.get("handler", ""), r["metadata"]["name"]) for r in items]
cand = [c for c in cand if c[0] == "kata" or c[0].startswith("kata-")]
cand.sort(key=lambda c: (c[0] != "kata", c[1]))
print(cand[0][1] if cand else "")'
}

# gen_pod <name> <runtime-class-or-"runc">
gen_pod() {
  local name="$1" rc="$2" rc_line=""
  if [[ "$rc" != "runc" && "$rc" != "none" && -n "$rc" ]]; then
    rc_line="  runtimeClassName: ${rc}"
  fi
  cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${name}
  labels: { app: kata-verify, boxkite-issue: "179" }
spec:
${rc_line}
  restartPolicy: Never
  shareProcessNamespace: true
  automountServiceAccountToken: false
  terminationGracePeriodSeconds: 5
  containers:
  - name: sandbox
    image: ${IMAGE}
    command: ["tail", "-f", "/dev/null"]   # the exact marker get_sandbox_pid() greps for
    securityContext:
      runAsUser: ${SANDBOX_UID}
      runAsGroup: ${SANDBOX_GID}
      runAsNonRoot: true
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: { drop: ["ALL"] }
      seccompProfile: { type: RuntimeDefault }
    volumeMounts:
    - { name: workspace, mountPath: /workspace }
  - name: sidecar
    image: ${IMAGE}
    command: ["sleep", "infinity"]
    securityContext:
      runAsUser: 0
      allowPrivilegeEscalation: false
      capabilities:
        add: ["SYS_PTRACE", "SYS_ADMIN", "CHOWN", "SYS_CHROOT", "SETUID", "SETGID"]
        drop: ["ALL"]
    volumeMounts:
    - { name: workspace, mountPath: /workspace }
  volumes:
  - name: workspace
    emptyDir:
      sizeLimit: ${SIZE_LIMIT}
YAML
}

preflight() {
  section "preflight"
  command -v kubectl >/dev/null || { fail "kubectl not found"; exit 2; }
  command -v python3 >/dev/null || { fail "python3 not found"; exit 2; }
  kubectl version -o json >/dev/null 2>&1 || { fail "cannot reach a cluster (kubectl)"; exit 2; }
  say "context : $(kubectl config current-context)"
  say "namespace: ${NAMESPACE}"
}

# node_selector_matches <runtime-class> -> prints matching node count
kata_node_count() {
  local rc="$1"
  kubectl get runtimeclass "$rc" -o json 2>/dev/null \
    | python3 -c '
import json,sys,subprocess
rc=json.load(sys.stdin)
sel=(rc.get("scheduling") or {}).get("nodeSelector") or {}
nodes=json.loads(subprocess.check_output(["kubectl","get","nodes","-o","json"]))["items"]
def ok(n):
    l=n["metadata"]["labels"]; return all(l.get(k)==v for k,v in sel.items())
print(sum(1 for n in nodes if ok(n)))
'
}

# ── Check 1: schedules + runs + is genuinely a separate-kernel VM ──────────
check_runs() {
  local pod="$1" rc="$2"
  section "check 1/3: pod schedules & runs under '${rc}'"
  gen_pod "$pod" "$rc" | kubectl apply -n "$NAMESPACE" -f - >/dev/null
  PODS_CREATED+=("$pod")
  if kubectl wait -n "$NAMESPACE" --for=condition=Ready "pod/$pod" --timeout="$READY_TIMEOUT" >/dev/null 2>&1; then
    local node; node=$(kubectl get pod -n "$NAMESPACE" "$pod" -o jsonpath='{.spec.nodeName}')
    pass "pod Running on node ${node}"
    local host_k guest_k
    host_k=$(kubectl get node "$node" -o jsonpath='{.status.nodeInfo.kernelVersion}' 2>/dev/null || echo "?")
    guest_k=$(kubectl exec -n "$NAMESPACE" "$pod" -c sandbox -- uname -r 2>/dev/null || echo "?")
    say "  host  node kernel : ${host_k}"
    say "  guest pod  kernel : ${guest_k}"
    if [[ "$rc" == "runc" || "$rc" == "none" || -z "$rc" ]]; then
      say "  (runc control: guest kernel == host kernel is expected)"
    elif [[ "$guest_k" == "?" || "$host_k" == "?" ]]; then
      warn "could not read both kernels (host='${host_k}' guest='${guest_k}') -> cannot confirm a separate VM kernel (need nodes-get RBAC + exec)"
      FAILED=1
    elif [[ "$guest_k" != "$host_k" ]]; then
      pass "guest kernel differs from host kernel -> genuinely a separate-kernel VM"
    else
      warn "guest kernel == host kernel -> could NOT confirm a separate VM kernel (unexpected under Kata)"
      FAILED=1
    fi
    return 0
  else
    fail "pod did not reach Ready within ${READY_TIMEOUT}"
    kubectl get pod -n "$NAMESPACE" "$pod" -o wide 2>&1 | sed 's/^/  /'
    kubectl describe pod -n "$NAMESPACE" "$pod" 2>&1 | grep -A20 -i events | sed 's/^/  /' || true
    FAILED=1
    return 1
  fi
}

# ── Check 2: emptyDir.sizeLimit enforcement (the load-bearing security Q) ──
check_sizelimit() {
  local pod="$1" rc="$2"
  section "check 3/3: emptyDir.sizeLimit enforcement under '${rc}' (limit=${SIZE_LIMIT}, writing ${WRITE_MB}Mi; DESTRUCTIVE, runs last)"
  # Emit dd's OWN exit code via a sentinel so an exec-transport failure
  # (kubectl returning e.g. "context deadline exceeded") can never be
  # mistaken for a real ENOSPC -- the transport error would leave no DDEXIT.
  local dd_out ddexit
  dd_out=$(kubectl exec -n "$NAMESPACE" "$pod" -c sandbox -- \
    sh -c "dd if=/dev/zero of=/workspace/fill bs=1M count=${WRITE_MB} 2>&1; echo DDEXIT=\$?" 2>&1) || true
  printf '%s\n' "$dd_out" | sed 's/^/    /'
  ddexit=$(printf '%s\n' "$dd_out" | sed -n 's/^DDEXIT=//p' | tail -1)

  if [[ -z "$ddexit" ]]; then
    warn "dd did not run to completion inside the sandbox (exec-transport failure?) -> INDETERMINATE, not a verdict"
    FAILED=1; return 1
  fi
  # A real filesystem hard-bound: dd failed AND said so with an ENOSPC/quota
  # message (narrow strings only -- not a bare 'exceeded').
  if [[ "$ddexit" != "0" ]] && printf '%s' "$dd_out" | grep -qiE 'no space left on device|disk quota exceeded|quota exceeded'; then
    pass "write was HARD-BOUNDED by the volume (ENFORCED at the filesystem layer)"
    return 0
  fi

  # dd wrote past the limit without a hard bound -> watch for kubelet eviction.
  # The ONLY reliable per-volume signal is kubelet's emptyDir message
  #   Usage of EmptyDir volume "..." exceeds the limit "..."
  # A bare Evicted/Failed can also be node DiskPressure or OOM -- a DIFFERENT,
  # node-global mechanism, NOT the per-tenant sizeLimit control #179 asks about.
  local waited=0 outcome="" detail="" phase reason smsg
  while [[ $waited -lt $EVICT_TIMEOUT ]]; do
    detail=$(kubectl get events -n "$NAMESPACE" --field-selector "involvedObject.name=${pod}" 2>/dev/null \
             | grep -i 'exceeds the limit' | head -1 || true)
    if [[ -n "$detail" ]]; then outcome="sizelimit"; break; fi
    phase=$(kubectl get pod -n "$NAMESPACE" "$pod" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    reason=$(kubectl get pod -n "$NAMESPACE" "$pod" -o jsonpath='{.status.reason}' 2>/dev/null || echo "")
    if [[ "$phase" == "Failed" || "$reason" == "Evicted" ]]; then
      smsg=$(kubectl get pod -n "$NAMESPACE" "$pod" -o jsonpath='{.status.message}' 2>/dev/null || echo "")
      if printf '%s' "$smsg" | grep -qi 'exceeds the limit'; then outcome="sizelimit"; detail="$smsg"; break; fi
      outcome="other-eviction"; detail="phase=${phase} reason=${reason} msg=${smsg}"; break
    fi
    sleep 5; waited=$((waited+5))
  done

  if [[ "$outcome" == "sizelimit" ]]; then
    pass "sizeLimit ENFORCED via kubelet eviction after ~${waited}s"
    say "    ${detail}"
    return 0
  fi
  if [[ "$outcome" == "other-eviction" ]]; then
    warn "pod terminated, but NOT via the emptyDir 'exceeds the limit' message -> likely node DiskPressure/OOM, a DIFFERENT mechanism than the per-volume control #179 asks about. INDETERMINATE."
    say "    ${detail}"
    FAILED=1; return 1
  fi

  # Timed out with the pod still alive. This is INDETERMINATE, NOT proof of
  # non-enforcement: kubelet's emptyDir accounting is periodic and can lag.
  local used
  used=$(kubectl exec -n "$NAMESPACE" "$pod" -c sandbox -- sh -c 'du -sh /workspace 2>/dev/null | cut -f1' 2>/dev/null || echo "?")
  phase=$(kubectl get pod -n "$NAMESPACE" "$pod" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
  if [[ "$phase" == "Running" ]]; then
    warn "wrote ${WRITE_MB}Mi into a ${SIZE_LIMIT} emptyDir; pod still Running after ${EVICT_TIMEOUT}s (du=${used}). INDETERMINATE."
    warn "kubelet emptyDir enforcement is periodic (~60s+ per cycle); re-run with a larger EVICT_TIMEOUT to confirm."
    warn "If it NEVER evicts across a generous timeout, THAT is the #179 cross-tenant disk-DoS regression (docs/KATA-CONTAINERS-SCOPING.md §3) -- but do not conclude it from one short wait."
    # Settle it definitively by measuring what kubelet actually SEES on the
    # node (issue #179's live Kata run: this stayed at 4.0K while 128Mi was
    # written inside the guest -> confirmed non-enforcement):
    local uid
    uid=$(kubectl get pod -n "$NAMESPACE" "$pod" -o jsonpath='{.metadata.uid}' 2>/dev/null || echo "<pod-uid>")
    warn "To settle it definitively, measure the host-side directory kubelet accounts (via node SSH or kubectl debug node/...):"
    say  "      du -sh /var/lib/kubelet/pods/${uid}/volumes/kubernetes.io~empty-dir/workspace"
    say  "      (still ~4K after this much writing => guest writes are invisible to kubelet => sizeLimit NOT enforced)"
    FAILED=1; return 1
  fi
  warn "indeterminate: phase=${phase} du=${used} (inspect manually)"
  FAILED=1; return 1
}

# ── Check 3: nsenter/setns() exec across the (Kata VM) boundary ────────────
check_nsenter() {
  local pod="$1" rc="$2"
  section "check 2/3: nsenter/setns exec under '${rc}' (build_k8s_exec_command shape)"
  # Run boxkite's exact exec chain from the sidecar:
  #   unshare -n nsenter -t <sandbox_pid> -m -p --setuid 1001 --setgid 1001 -- sh -c ...
  # Find the sandbox PID the way get_sandbox_pid() does (the tail marker), but
  # with a pure-shell, anchored match so the search process can't self-match.
  local script out rcx=0
  script='
set -e
SB=""
for p in /proc/[0-9]*; do
  pid="${p#/proc/}"
  cmd=$(tr "\0" " " < "$p/cmdline" 2>/dev/null || true)
  case "$cmd" in
    "tail -f /dev/null "*) SB="$pid"; break ;;
  esac
done
[ -n "$SB" ] || { echo "SANDBOX_PID_NOT_FOUND"; exit 3; }
echo "SANDBOX_PID=$SB"
echo "SIDECAR_MNT=$(readlink /proc/self/ns/mnt)"
echo "SANDBOX_MNT=$(readlink /proc/$SB/ns/mnt)"
unshare -n nsenter -t "$SB" -m -p --setuid 1001 --setgid 1001 -- \
  sh -c "echo EXEC_UID=\$(id -u); echo EXEC_GID=\$(id -g); echo EXEC_MNT=\$(readlink /proc/self/ns/mnt); echo EXEC_NET=\$(readlink /proc/self/ns/net); touch /workspace/nsenter-was-here && echo EXEC_WROTE_WORKSPACE=ok"
'
  out=$(kubectl exec -n "$NAMESPACE" "$pod" -c sidecar -- sh -c "$script" 2>&1) || rcx=$?
  printf '%s\n' "$out" | sed 's/^/    /'
  local sb_mnt side_mnt exec_mnt exec_uid wrote
  sb_mnt=$(printf '%s\n' "$out" | sed -n 's/^SANDBOX_MNT=//p')
  side_mnt=$(printf '%s\n' "$out" | sed -n 's/^SIDECAR_MNT=//p')
  exec_mnt=$(printf '%s\n' "$out" | sed -n 's/^EXEC_MNT=//p')
  exec_uid=$(printf '%s\n' "$out" | sed -n 's/^EXEC_UID=//p')
  wrote=$(printf '%s\n' "$out" | sed -n 's/^EXEC_WROTE_WORKSPACE=//p')

  if [[ $rcx -ne 0 ]]; then fail "exec chain failed (exit ${rcx})"; FAILED=1; return 1; fi
  local ok=1
  [[ "$exec_uid" == "$SANDBOX_UID" ]] || { fail "setuid did not drop to ${SANDBOX_UID} (got '${exec_uid}')"; ok=0; }
  [[ -n "$exec_mnt" && "$exec_mnt" == "$sb_mnt" ]] || { fail "exec did NOT land in the sandbox mount ns (exec=${exec_mnt} sandbox=${sb_mnt})"; ok=0; }
  [[ "$exec_mnt" != "$side_mnt" ]] || { fail "exec stayed in the sidecar mount ns -- setns did not cross the boundary"; ok=0; }
  [[ "$wrote" == "ok" ]] || { fail "could not write to the sandbox workspace from the exec'd process"; ok=0; }
  if [[ $ok -eq 1 ]]; then
    pass "nsenter entered the sandbox mount+PID ns, dropped to uid ${SANDBOX_UID}, wrote workspace"
    pass "setns()/nsenter exec mechanism survives the '${rc}' boundary"
    return 0
  fi
  FAILED=1; return 1
}

run_suite() {
  local rc="$1" tag="$2"
  local pod="kata-verify-${tag}"
  local matches
  if [[ "$rc" != "runc" && "$rc" != "none" && -n "$rc" ]]; then
    matches=$(kata_node_count "$rc" 2>/dev/null || echo "?")
    say "RuntimeClass '${rc}' -> ${matches} matching node(s) for its scheduling.nodeSelector"
    if [[ "$matches" == "0" ]]; then
      warn "no node matches this RuntimeClass's nodeSelector -- the pod will stay Pending."
      warn "add a Kata-capable node pool first (e.g. AKS: az aks nodepool add --workload-runtime KataVmIsolation --os-sku AzureLinux)."
    fi
  fi
  # Order matters: check_sizelimit deliberately triggers a kubelet eviction
  # (it writes past the limit), which destroys the pod -- so the non-
  # destructive nsenter check MUST run before it, not after.
  check_runs "$pod" "$rc" || { return 1; }
  check_nsenter "$pod" "$rc" || true
  check_sizelimit "$pod" "$rc" || true
  kubectl delete pod -n "$NAMESPACE" "$pod" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}

main() {
  preflight
  if [[ "${WITH_CONTROL:-}" == "1" ]]; then
    section "CONTROL RUN (runc) -- validates the harness detects enforcement; proves nothing about Kata"
    run_suite "runc" "runc" || true
  fi
  local rc
  rc=$(detect_runtime_class)
  if [[ "${RUNTIME_CLASS:-}" == "runc" || "${RUNTIME_CLASS:-}" == "none" ]]; then
    say ""; warn "RUNTIME_CLASS=${RUNTIME_CLASS}: control-only run, not a Kata verification."
    [[ "${WITH_CONTROL:-}" == "1" ]] || run_suite "runc" "runc" || true
  elif [[ -z "$rc" ]]; then
    section "no Kata RuntimeClass found"
    fail "no RuntimeClass with handler 'kata' or 'kata-*' on this cluster. Install one (kata-deploy / AKS Pod Sandboxing) or set RUNTIME_CLASS."
    exit 3
  else
    section "KATA RUN -- RuntimeClass '${rc}'"
    run_suite "$rc" "kata" || true
  fi

  section "summary"
  if [[ $FAILED -eq 0 ]]; then
    pass "all checks that ran reached a definitive verdict"
    exit 0
  else
    fail "at least one check FAILED or was indeterminate -- see output above"
    exit 1
  fi
}
main "$@"
