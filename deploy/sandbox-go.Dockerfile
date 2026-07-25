# syntax=docker/dockerfile:1.4

# Go-only Sandbox Container
# A genuinely smaller footprint than sandbox-minimal.Dockerfile (and no
# JS/TS toolchain either, unlike sandbox-node.Dockerfile) for callers whose
# workload is purely Go: a `go test ./...` run, a `go build` of a CLI tool,
# a Go module's linting/vetting. No Python interpreter, no pip, no Node, no
# npm -- not just "python_packages/npm_packages left empty" but the
# runtimes themselves absent. Closes another slice of
# docs/E2B-COMPARISON.md's "template gallery" gap: a base other than
# boxkite-default/boxkite-minimal/boxkite-node for callers who'd rather not
# carry runtimes they'll never use.
#
# SECURITY NOTES (identical posture to deploy/sandbox-node.Dockerfile):
# - No Python/pip, no Node/npm anywhere in this image -- there is nothing
#   to remove because neither was ever installed, not merely stripped
#   post-build.
# - Go itself has no separate "package manager binary" the way pip/npm do:
#   `go` is a single toolchain binary that compiles, tests, and vets code,
#   and there is no equivalent "uninstall go-get to prevent runtime
#   installs" story -- `go build`/`go run`/`go test` on a module with
#   dependencies will themselves reach out over the network (GOPROXY,
#   default https://proxy.golang.org) to resolve and fetch remote modules
#   on demand. That IS a real supply-chain/egress consideration -- just a
#   different shape than the pip/npm "runtime package-manager binary
#   present" issue this repo strips elsewhere -- so it's addressed the same
#   way the rest of this repo addresses untrusted egress: via the
#   sandbox's network policy/isolated network namespace at runtime (see
#   SECURITY.md), not by trying to remove `go` itself (which would make the
#   image useless for its one stated purpose).
# - Container runs with minimal environment variables (no API keys, credentials)
# - Generated commands can run in an isolated network namespace at runtime
# - Runs as non-root user (UID 1001)

FROM cgr.dev/chainguard/wolfi-base:latest@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795

RUN apk update && apk add --no-cache \
    ca-certificates \
    bash \
    curl \
    libstdc++ \
    jq \
    openssh-client \
    git \
    go-1.24 \
    && go version | grep -Eq '^go version go1\.24\.'

# Create non-root user for sandbox execution
RUN adduser -D -u 1001 -s /bin/bash sandbox

# Create directories (will be mounted as EmptyDir volumes)
RUN mkdir -p /workspace /mnt/skills /mnt/user-data/uploads /mnt/user-data/outputs \
    && chown -R sandbox:sandbox /workspace \
    && chmod 1777 /tmp /var/tmp

# SECURITY: Set minimal environment variables
# These are the ONLY env vars that should be set - no API keys, credentials, etc.
# GOPATH/GOCACHE are pointed under /workspace (owned by the sandbox user,
# writable) rather than the default $HOME/go under a non-writable HOME, and
# GOMODCACHE follows GOPATH the same way -- otherwise `go build`/`go test`
# in a fresh session would fail on first module fetch.
ENV PATH="/usr/local/bin:/usr/bin:/bin" \
    HOME="/workspace" \
    LANG="C.UTF-8" \
    LC_ALL="C.UTF-8" \
    GOPATH="/workspace/go" \
    GOCACHE="/tmp/.cache/go-build" \
    XDG_CONFIG_HOME="/tmp/.config" \
    XDG_CACHE_HOME="/tmp/.cache"

USER sandbox

WORKDIR /workspace

CMD ["tail", "-f", "/dev/null"]
