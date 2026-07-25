# syntax=docker/dockerfile:1.4

# Claude Code Sandbox Container
# Extends sandbox-minimal.Dockerfile with the Claude Code CLI
# (@anthropic-ai/claude-code) preinstalled, for running Claude Code headless
# against a boxkite sandbox -- see docs/CLAUDE-CODE-SANDBOX-QUICKSTART.md and
# examples/claude_code_sandbox/.
#
# WHY THIS IS A SEPARATE, OUT-OF-BAND DOCKERFILE AND NOT A DECLARATIVE-BUILDER
# IMAGE (docs/DECLARATIVE-BUILDER-DESIGN.md): the declarative builder's
# `SandboxImageBuildRequest` only accepts `python_packages`/`apt_packages` --
# there is no `npm_packages` field, so an npm-global-install use case like
# this one cannot be self-served through `POST /v1/images` today. That's a
# real, narrower gap than "no custom images at all" (see
# docs/E2B-COMPARISON.md's gap table), not something this Dockerfile tries
# to route around by other means.
#
# SECURITY NOTES (same posture as sandbox-minimal.Dockerfile otherwise):
# - pip is REMOVED after installation; runtime only needs the packages baked
#   in here.
# - npm is REMOVED after installing @anthropic-ai/claude-code globally --
#   the resulting `claude` binary and its own bundled dependencies remain,
#   but no package manager survives into the runtime image, same as every
#   other boxkite base image.
# - Claude Code's own credential (ANTHROPIC_API_KEY) is deliberately NOT
#   baked into this image or set as a build-time ENV -- see
#   docs/CLAUDE-CODE-SANDBOX-QUICKSTART.md's security note on why it must be
#   supplied per-command instead, and why that's a known, tracked gap
#   (docs/SECRETS-DESIGN.md), not a solved problem.
# - Runs as non-root user (UID 1001), same as every other boxkite base image.

ARG PYTHON_VERSION=3.11

FROM cgr.dev/chainguard/wolfi-base:latest@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795
ARG PYTHON_VERSION

RUN apk update && apk add --no-cache \
    python-${PYTHON_VERSION} \
    py3.11-pip \
    ca-certificates \
    bash \
    curl \
    git \
    libstdc++ \
    jq \
    openssh-client \
    sqlite \
    nodejs-22 \
    npm \
    && ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python3 \
    && ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python \
    && node --version | grep -Eq '^v22\.(2[2-9]|[3-9][0-9])\.' \
    && npm --version | grep -Eq '^(11\.(1[5-9]|[2-9][0-9])\.|1[2-9]\.)'

# Claude Code CLI, installed globally while npm is still present. Pin a
# specific version rather than floating @latest, matching the declarative
# builder's own pinned-version-only policy for python_packages/apt_packages
# -- an unpinned version here would be a silent, unreviewed image-content
# change on every rebuild. Bump this deliberately, not by accident.
ARG CLAUDE_CODE_VERSION=2.0.1
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && claude --version

# SECURITY: Remove pip/npm after use, same as sandbox-minimal.Dockerfile --
# the `claude` binary and its already-resolved node_modules survive; no
# package manager does.
RUN rm -f /usr/bin/pip /usr/bin/pip3 /usr/bin/pip3.11 \
    && rm -rf /usr/lib/python3.11/ensurepip \
    && rm -rf /usr/lib/python3.11/site-packages/pip* \
    && apk del npm node-gyp || true \
    && rm -rf /usr/bin/npm /usr/bin/npx \
    && rm -rf /root/.cache/pip /root/.cache/uv /root/.npm

# Create non-root user for sandbox execution
RUN adduser -D -u 1001 -s /bin/bash sandbox

# Create directories (will be mounted as EmptyDir volumes)
RUN mkdir -p /workspace /mnt/skills /mnt/user-data/uploads /mnt/user-data/outputs \
    && chown -R sandbox:sandbox /workspace \
    && chmod 1777 /tmp /var/tmp

# SECURITY: Set minimal environment variables
# These are the ONLY env vars that should be set - no API keys, credentials,
# etc. ANTHROPIC_API_KEY is deliberately absent -- see the file header.
ENV PATH="/usr/local/bin:/usr/bin:/bin" \
    HOME="/workspace" \
    LANG="C.UTF-8" \
    LC_ALL="C.UTF-8" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1" \
    XDG_CONFIG_HOME="/tmp/.config" \
    XDG_CACHE_HOME="/tmp/.cache"

USER sandbox

WORKDIR /workspace

CMD ["tail", "-f", "/dev/null"]
