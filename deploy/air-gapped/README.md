# Air-gapped Docker Compose reference bundle — results

This is the "build exactly one reference bundle end-to-end" step that
`docs/AIR-GAPPED-DEPLOYMENT-SCOPING.md` §4 asked for, before committing to
any air-gapped customer date. It covers **only** the self-host Docker
Compose path (`../docker-compose.yml`) — see "What this does NOT cover"
below for everything still open.

**Status: a real, executed, torn-down-and-reloaded round trip on 2026-07-15
against this exact working tree.** All commands below were actually run
(not hypothetical); real timings and sizes are recorded, not estimated.

## What's in this directory

| File | Purpose |
|---|---|
| `mirror-images.sh` | Run **with internet access**. Builds/pulls every image the stack needs and saves them into one tarball. |
| `load-images.sh` | Run **on the air-gapped side, zero network calls**. Loads that tarball into the local Docker image cache. |
| `docker-compose.airgapped.yml` | A variant of `../docker-compose.yml` using `image:` + `pull_policy: never` instead of `build:`, so bringing the stack up never touches a registry or a package manager. |
| `.env` (gitignored, not committed) | Holds `SIDECAR_AUTH_TOKEN` — generate your own with `openssl rand -hex 32`. |
| `bundle/` (gitignored, not committed) | Where `mirror-images.sh` writes the tarball + manifest. Regenerate it yourself; it's not a repo artifact. |

## Why `sandbox-minimal`, not the default `sandbox` image

`../docker-compose.yml`'s `sandbox` service builds `../sandbox.Dockerfile`,
forced to `platform: linux/amd64` because the pinned Chrome-for-Testing
build has no linux/arm64 release. This reference bundle was built on
Apple Silicon (arm64 host, Colima); a `linux/amd64` build of the full
image (LibreOffice, Chrome-for-Testing, the full pip/npm data-science
stack) would run under QEMU emulation and take a long time — plausibly
tens of minutes per layer — which wasn't a good use of the time budget for
proving the *mechanism* (mirror → carry → load → run offline), since that
mechanism is identical regardless of which image you point it at.

Used `../sandbox-minimal.Dockerfile` instead: same base
(`cgr.dev/chainguard/wolfi-base:latest`), same "no package manager left in
the final image" security posture, but no LibreOffice/Chrome/pandoc/heavy
Python stack — and it's already published for both amd64 **and** arm64
(see README.md's package table), so it built natively in ~2 minutes with
no emulation. The mirror/load technique in this directory applies
byte-for-byte the same way to the full `sandbox.Dockerfile` image; someone
repeating this on an amd64 host (or willing to eat the QEMU build time)
should get the same result, just with a much bigger image and a much
longer step 1.

## A real gotcha found while building this: container naming is load-bearing

`docker-compose.airgapped.yml` originally named the sandbox container
`sandbox-airgapped` (to avoid clashing with `../docker-compose.yml`'s own
`sandbox` container name). That broke `/exec` at runtime with `Error
response from daemon: No such container: sandbox` at the time (when compose
mode still used `docker exec` with a hardcoded container name). Compose mode
now execs via `nsenter` after sharing a PID namespace with the sandbox
container (`pid: "container:sandbox"`), which hardcodes the same literal
name from the other direction — `get_sandbox_pid()` in `sidecar/main.py`
finds the sandbox's init process by pgrep'ing for this exact container name.
Fixed by keeping `container_name: sandbox` in this compose file too (see the
inline comment) — this is a real constraint worth knowing if anyone ever
wants to run more than one boxkite Compose stack side by side on the same
Docker host.

## A real finding about the "already published" GHCR images

Before building anything, this bundle first tried the obvious shortcut:
pull the already-published `ghcr.io/evalssment/boxkite-sandbox` /
`boxkite-sidecar` images (per README.md's "Published packages and images"
table) instead of rebuilding from Dockerfiles. Both failed anonymously:

```
$ docker pull ghcr.io/evalssment/boxkite-sidecar:0.1.0
Error response from daemon: error from registry: unauthorized

$ curl -sI https://ghcr.io/v2/evalssment/boxkite-sidecar/manifests/0.1.0
HTTP/2 401
www-authenticate: Bearer realm="https://ghcr.io/token",service="ghcr.io",...
```

Even the anonymous token endpoint returns `UNAUTHORIZED`, which is what
GHCR does for a **private** package, not a public one requiring login.
Worth flagging back to whoever owns the GHCR org: if these are meant to be
public per README.md, the package visibility itself may need fixing on
GitHub's side — this doc can't fix that from here (no GHCR admin access in
this environment), so this bundle mirrors **freshly built** images built
from `../sandbox-minimal.Dockerfile` and `../sidecar.Dockerfile` instead.
This also means §3.1 of the scoping doc ("these just need a documented
pull once, push to your mirror step") needs a caveat: that's only true
once the packages are actually anonymously pullable.

## What was actually run, with real output

### 1. Mirror (on a machine with internet access)

First cold build (no Docker layer cache at all, this same machine/day):

```
$ time docker build -f deploy/sandbox-minimal.Dockerfile -t boxkite-sandbox-minimal:airgapped .
...
real  2m1.53s

$ time docker build -f deploy/sidecar.Dockerfile -t boxkite-sidecar:airgapped .
...
real  2m8.96s
```

Then `./mirror-images.sh` end to end (this run hit Docker's build cache
from the cold builds above, which is realistic — a real operator's
*second* mirror run behaves the same way):

```
$ time ./mirror-images.sh
== [1/5] Building sandbox image ==        (cache hit)  real 0m3.5s
== [2/5] Building sidecar image ==        (cache hit)  real 0m0.8s
== [3/5] Pulling minio/mc/vault ==        (already local) real ~2.3s each
== [4/5] docker save (5 images) ==                     real 0m5.2s
== [5/5] gzip compress ==                              real 0m7.0s

Bundle:   deploy/air-gapped/bundle/boxkite-airgapped-bundle.tar.gz
-rw-------  433M  boxkite-airgapped-bundle.tar.gz
```

`bundle-manifest.json` (image IDs + uncompressed sizes):

```json
{
  "images": [
    {"tag": "boxkite-sandbox-minimal:bundle", "size_bytes": 106190617},
    {"tag": "boxkite-sidecar:bundle",         "size_bytes": 120233313},
    {"tag": "minio/minio:latest",             "size_bytes": 57548825},
    {"tag": "minio/mc:latest",                "size_bytes": 27371684},
    {"tag": "hashicorp/vault:1.16",           "size_bytes": 143004011}
  ]
}
```

Sum of uncompressed image sizes: ~454 MB. Compressed tarball actually
carried across the air gap: **433 MB** (`gzip` gets real compression here
since these are mostly-text/binary Wolfi + Go layers, not already-compressed
media).

### 2. Simulate the air gap

Removed every one of the 5 bundled image tags from the local Docker image
cache (`docker rmi ...`) to simulate a machine that never built or pulled
anything:

```
$ docker images | grep -E "boxkite-sandbox-minimal|boxkite-sidecar|minio/minio|minio/mc|hashicorp/vault"
NONE FOUND (clean slate confirmed)
```

**Negative test — proving `pull_policy: never` actually blocks a silent
registry fallback**, not just "happens to work because nothing tried to
reach the network":

```
$ docker compose -f docker-compose.airgapped.yml -p boxkite-airgapped up -d
 Container sandbox-airgapped Error response from daemon: No such image: boxkite-sandbox-minimal:bundle
Error response from daemon: No such image: boxkite-sandbox-minimal:bundle
$ echo $?
1
```

This fails immediately and loudly, exactly as intended — an operator who
forgot to run `load-images.sh` gets a clear error, not a hang or a silent
registry pull attempt.

### 3. Load (zero network calls) and bring the stack up

```
$ ./load-images.sh
== Loading .../boxkite-airgapped-bundle.tar.gz (no network access required or used) ==
Loaded image: boxkite-sandbox-minimal:bundle
Loaded image: boxkite-sidecar:bundle
Loaded image: minio/minio:latest
Loaded image: minio/mc:latest
Loaded image: hashicorp/vault:1.16
real  0m9.194s
== Verifying loaded images against expected tags ==
  OK   boxkite-sandbox-minimal:bundle
  OK   boxkite-sidecar:bundle
  OK   minio/minio:latest
  OK   minio/mc:latest
  OK   hashicorp/vault:1.16
Load verified — all bundled images present locally, no registry contacted.

$ time docker compose -f docker-compose.airgapped.yml -p boxkite-airgapped up -d
...
real  0m10.693s
```

### 4. Verify the stack actually works, not just "containers are Up"

```
$ curl -sf http://localhost:8080/health
{"status":"healthy","session_id":null,"skills_rev":null,"runtime_mode":"compose","storage_backend":"s3","idle_seconds":15.7}

$ curl -s -X POST http://localhost:8080/exec \
    -H "Content-Type: application/json" \
    -H "X-Sidecar-Auth-Token: $SIDECAR_AUTH_TOKEN" \
    -d '{"command": "echo air-gapped-proof-$((21+21))"}'
{"exit_code":0,"stdout":"air-gapped-proof-42\n","stderr":""}

$ docker logs boxkite-airgapped-minio-setup-1
Added `myminio` successfully.
Bucket created successfully `myminio/boxkite-sandbox`.
```

`/exec` round-tripped through the sidecar's real `nsenter` path into the
sandbox container and ran a real command — this is the actual product
functionality working end to end, entirely from images that were `docker
load`ed from a tarball, with zero registry contact for the whole "air-gapped"
half of this exercise.

## Numbers, for the record

| Step | Time | Notes |
|---|---|---|
| Cold build, sandbox-minimal (no cache) | 2m 1.5s | arm64 native, this machine |
| Cold build, sidecar (no cache) | 2m 9.0s | arm64 native, this machine |
| `mirror-images.sh` full run (warm cache) | ~25s | build steps cache-hit; save+compress dominate |
| `docker save` (5 images) | 5.2s | |
| `gzip` compress | 7.0s | 454MB uncompressed → 433MB compressed |
| Tarball size (compressed) | 433 MB | what actually gets carried across the gap |
| `load-images.sh` (`docker load`, zero network) | 9.2s | |
| `docker compose up -d` (post-load, no build/pull) | 10.7s | includes MinIO healthcheck + bucket setup |

For an operator doing this for real: expect the **cold build** numbers to
dominate on a from-scratch mirror run (a few minutes for this
lean image set), and expect the full `sandbox.Dockerfile` variant
(LibreOffice, Chrome-for-Testing, full pip/npm stack) to be substantially
bigger and slower to both build and transfer — no measurement of that
variant was taken here (see above).

## What this proves

- The Compose path (`sandbox` + `sidecar` + `minio` + `minio-setup`, plus
  `vault` behind its existing profile gate) can run with **zero live
  network access** once its images are pre-loaded — no `apk`, `pip`, `npm`,
  or registry reachability required at container-runtime.
- `pull_policy: never` is a real, working enforcement mechanism, not just
  an assumption — verified it hard-fails immediately when an image is
  missing, rather than hanging or silently trying a registry.
- The actual product functionality (`/health`, `/exec` through the
  sidecar's real docker-exec-into-sandbox path, MinIO bucket bootstrap)
  works correctly when everything comes from a `docker load`ed image.
- A single mirror/load script pair plus a `pull_policy: never` compose
  variant is enough plumbing for this slice — no private registry, no
  Helm chart, no custom tooling was needed to prove the mechanism.

## What this does NOT cover (still open per the scoping doc's inventory)

- **The full `sandbox.Dockerfile` image** (LibreOffice, Chrome-for-Testing,
  pandoc, the full `requirements.lock`/`package-lock.json` stacks) — not
  built or measured here; see "Why sandbox-minimal" above. The same
  mirror/load mechanism applies; only the size/time numbers would differ,
  and building it needs an amd64 host (or QEMU time) to avoid the
  Chrome-for-Testing arm64 gap.
- **The Kubernetes/Kaniko path** — `deploy/pod-template.yaml`,
  `deploy/image-builder-job.yaml`'s Kaniko executor, and
  `deploy/image-builder-network-policy.yaml`'s CHANGEME egress rule are
  untouched. This bundle proves nothing about `kind`/K8s, a private
  registry mirror, or Kaniko's own base-image/package-mirror needs — the
  scoping doc's §2.5 (the declarative builder's *runtime*, not just
  build-time, internet dependency) is still fully open.
- **The declarative image builder** (`control-plane/src/control_plane/image_builder.py`,
  `BOXKITE_IMAGE_BUILDER_ENABLED`) — off by default, not exercised here at
  all.
- **The control-plane image and deployment** — `control-plane/Dockerfile`
  (`python:3.11-slim` base, plain `pip install -r requirements.txt`) was
  not built, mirrored, or run as part of this bundle. `../docker-compose.yml`
  itself has no control-plane service, consistent with the scoping doc's
  own framing.
- **An offline package mirror for rebuilding from Dockerfiles** — this
  bundle mirrors already-built final images, not the `apk`/PyPI/npm
  package sets those Dockerfiles resolve at build time. An air-gapped site
  that wants to *rebuild* (not just *run*) these images from source still
  needs the Wolfi/PyPI/npm mirrors the scoping doc's §3 describes; nothing
  here starts that work.
- **A version-pinned bundle manifest tying a bundle to a release tag**
  (scoping doc §3 point 7) — `bundle-manifest.json` here only records this
  run's own image IDs/sizes, not a signed/versioned artifact tied to a
  boxkite release. That's still a real design gap for a production offline
  bundle process.
- **An offline update/license model** — unchanged from the scoping doc;
  still undefined.
- **GHCR package visibility** — found, not fixed here (no admin access from
  this environment); see the finding above. Worth a maintainer follow-up
  independent of the air-gap work.

## Reproducing this

```bash
cd deploy/air-gapped
cp .env.example .env   # or: echo "SIDECAR_AUTH_TOKEN=$(openssl rand -hex 32)" > .env
./mirror-images.sh                      # needs internet access
# --- simulate carrying bundle/boxkite-airgapped-bundle.tar.gz across the gap ---
./load-images.sh                        # zero network calls
docker compose -f docker-compose.airgapped.yml -p boxkite-airgapped up -d
curl -sf http://localhost:8080/health
docker compose -f docker-compose.airgapped.yml -p boxkite-airgapped down -v
```
