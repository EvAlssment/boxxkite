# syntax=docker/dockerfile:1.4

# Rust-only Sandbox Container
# A genuinely smaller footprint than sandbox-minimal.Dockerfile (and no
# JS/TS toolchain either, unlike sandbox-node.Dockerfile) for callers whose
# workload is purely Rust: a `cargo test`/`cargo build` run, `cargo clippy`,
# a crate's linting/vetting. No Python interpreter, no pip, no Node, no
# npm -- not just "python_packages/npm_packages left empty" but the
# runtimes themselves absent. Mirrors deploy/sandbox-go.Dockerfile's
# "single reviewed language runtime, nothing else" positioning exactly --
# closes another slice of docs/E2B-COMPARISON.md's "template gallery" gap.
#
# TOOLCHAIN SOURCE: Wolfi's own apk package (`rust-1.96`, pinned to rustc/
# cargo 1.96.1), not rustup's `curl | sh` installer. Two reasons this
# mirrors sandbox-go.Dockerfile's `go-1.24` precedent rather than diverging
# from it:
# - Wolfi already builds and signs a `rust-1.96` apk package from this
#   same trusted repo every other package in this image comes from (see
#   https://github.com/wolfi-dev/os/blob/main/rust-1.96.yaml) -- pulling it
#   via `apk add` keeps this image's entire supply chain on one already-
#   trusted channel (apk's own package signing), rather than adding a
#   second, differently-verified install path (rustup's TLS-only download
#   + its own separate signing key) for exactly one language runtime.
# - rustup's own install docs recommend `curl https://sh.rustup.rs | sh`,
#   which this project's own security posture (SECURITY.md's disclosed
#   docker-socket/allowlist-bypass findings) treats as exactly the kind of
#   pattern to avoid where an equivalent already-reviewed package exists --
#   piping a remote script directly into a shell as part of a build this
#   project ships to others is a strictly worse verification story than
#   `apk add rust-1.96`, which resolves through the same signed index used
#   for every other package below.
# Pinned to rust-1.96 (rustc/cargo 1.96.1, current Wolfi-packaged stable at
# the time this base was added) rather than an unpinned "latest" rust
# meta-package, matching go-1.24's exact-minor-version pin. Bump this (and
# the version grep below) deliberately on a schedule, not implicitly via a
# floating tag.
#
# LINKER: unlike Go (go-1.24 ships its own self-contained linker and needs
# no external `cc`), rustc always shells out to a system linker to produce
# the final binary -- there is no way to `cargo build`/`cargo test`
# anything without one. `gcc` is included below for exactly this reason
# (verified directly: a `cargo build` in this image fails with
# "linker `cc` not found" without it). This is not a re-introduction of
# the Python/Node runtimes this base otherwise excludes -- it is Rust's own
# unavoidable toolchain dependency, the same way the official upstream
# `rust:*` Docker images also bundle a C compiler for this exact reason.
#
# SECURITY NOTES (identical posture to deploy/sandbox-go.Dockerfile):
# - No Python/pip, no Node/npm anywhere in this image -- there is nothing
#   to remove because neither was ever installed, not merely stripped
#   post-build.
# - Rust has no separate "package manager binary" the way pip/npm do to
#   strip after use: `cargo` IS the toolchain's dependency manager, build
#   tool, and test runner all at once, the same "one binary does
#   everything, nothing to remove" shape go-1.24 already has in this
#   image's Go sibling. There is no "uninstall cargo to prevent runtime
#   installs" story that wouldn't also remove the ability to build/test at
#   all -- `cargo build`/`cargo test`/`cargo run` on a project with
#   dependencies will themselves reach out over the network (to
#   crates.io's index and its backing CDN by default, or a configured
#   alternate registry) to resolve and fetch remote crates on demand. This
#   is the same class of supply-chain/egress consideration go-1.24's own
#   GOPROXY paragraph already discloses for `go build`, just crates.io
#   instead of the Go module proxy -- addressed the same way the rest of
#   this repo addresses untrusted egress: via the sandbox's network
#   policy/isolated network namespace at runtime (see SECURITY.md), not by
#   trying to remove `cargo` itself (which would make the image useless
#   for its one stated purpose).
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
    gcc \
    rust-1.96 \
    && rustc --version | grep -Eq '^rustc 1\.96\.' \
    && cargo --version | grep -Eq '^cargo 1\.96\.'

# Create non-root user for sandbox execution
RUN adduser -D -u 1001 -s /bin/bash sandbox

# Create directories (will be mounted as EmptyDir volumes)
RUN mkdir -p /workspace /mnt/skills /mnt/user-data/uploads /mnt/user-data/outputs \
    && chown -R sandbox:sandbox /workspace \
    && chmod 1777 /tmp /var/tmp

# SECURITY: Set minimal environment variables
# These are the ONLY env vars that should be set - no API keys, credentials, etc.
# CARGO_HOME is pointed under /workspace (owned by the sandbox user,
# writable) rather than the default $HOME/.cargo under a non-writable HOME,
# mirroring GOPATH's exact reasoning in sandbox-go.Dockerfile -- otherwise
# `cargo build`/`cargo test` in a fresh session would fail on first crate
# fetch (registry index + downloaded crate sources both live under
# CARGO_HOME).
ENV PATH="/usr/local/bin:/usr/bin:/bin" \
    HOME="/workspace" \
    LANG="C.UTF-8" \
    LC_ALL="C.UTF-8" \
    CARGO_HOME="/workspace/.cargo" \
    XDG_CONFIG_HOME="/tmp/.config" \
    XDG_CACHE_HOME="/tmp/.cache"

USER sandbox

WORKDIR /workspace

CMD ["tail", "-f", "/dev/null"]
