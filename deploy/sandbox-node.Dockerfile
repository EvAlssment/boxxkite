# syntax=docker/dockerfile:1.4

# Node-only Sandbox Container
# A genuinely smaller footprint than sandbox-minimal.Dockerfile for callers
# who only need a JS/TS toolchain: no Python interpreter, no pip, at all --
# not just "python_packages left empty" but the runtime itself absent.
# Closes part of docs/E2B-COMPARISON.md's "template gallery" gap: a base
# other than boxkite-default/boxkite-minimal, for callers whose workload is
# purely Node/TypeScript (a frontend build, a Next.js/Vite dev server driven
# via bash_tool + the network-ingress preview-URL feature, a JS test suite)
# and who'd rather not carry a Python runtime they'll never use.
#
# SECURITY NOTES (identical posture to deploy/sandbox-minimal.Dockerfile):
# - npm is REMOVED after installation; runtime only needs node
# - No Python/pip anywhere in this image -- there is nothing to remove
#   because it was never installed, not merely stripped post-build
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
    nodejs-22 \
    npm \
    && node --version | grep -Eq '^v22\.(2[2-9]|[3-9][0-9])\.' \
    && npm --version | grep -Eq '^(11\.(1[5-9]|[2-9][0-9])\.|1[2-9]\.)'

# SECURITY: Remove npm to prevent runtime package installation. The
# declarative builder (image_builder.py's render_dockerfile) reinstalls npm
# transiently, in its own layer, to install a caller's pinned npm_packages,
# then removes it again in that same layer -- this base image itself never
# ships a package manager.
RUN apk del npm node-gyp || true \
    && rm -rf /usr/bin/npm /usr/bin/npx /root/.npm

# Create non-root user for sandbox execution
RUN adduser -D -u 1001 -s /bin/bash sandbox

# Create directories (will be mounted as EmptyDir volumes)
RUN mkdir -p /workspace /mnt/skills /mnt/user-data/uploads /mnt/user-data/outputs \
    && chown -R sandbox:sandbox /workspace \
    && chmod 1777 /tmp /var/tmp

# SECURITY: Set minimal environment variables
# These are the ONLY env vars that should be set - no API keys, credentials, etc.
ENV PATH="/usr/local/bin:/usr/bin:/bin" \
    HOME="/workspace" \
    LANG="C.UTF-8" \
    LC_ALL="C.UTF-8" \
    XDG_CONFIG_HOME="/tmp/.config" \
    XDG_CACHE_HOME="/tmp/.cache"

USER sandbox

WORKDIR /workspace

CMD ["tail", "-f", "/dev/null"]
