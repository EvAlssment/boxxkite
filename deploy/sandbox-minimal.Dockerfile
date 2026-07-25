# syntax=docker/dockerfile:1.4

# Minimal Sandbox Container
# Lean python+node base with NONE of deploy/sandbox.Dockerfile's preinstalled
# data-science/document-conversion/headless-browser stack. Intended as a
# `base` value for the declarative builder (docs/DECLARATIVE-BUILDER-DESIGN.md)
# for callers who want to compose their own python_packages/apt_packages from
# scratch on a small, fast-building image instead of layering on top of the
# much larger boxkite-default footprint.
#
# SECURITY NOTES (identical posture to deploy/sandbox.Dockerfile):
# - pip is REMOVED after installation to prevent runtime package installs
# - npm is REMOVED after installation; runtime only needs node
# - Container runs with minimal environment variables (no API keys, credentials)
# - Generated commands can run in an isolated network namespace at runtime
# - Runs as non-root user (UID 1001)

ARG PYTHON_VERSION=3.11

FROM cgr.dev/chainguard/wolfi-base:latest@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795
ARG PYTHON_VERSION

RUN apk update && apk add --no-cache \
    python-${PYTHON_VERSION} \
    py3.11-pip \
    ca-certificates \
    bash \
    curl \
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

# SECURITY: Remove pip/npm to prevent runtime package installation. The
# declarative builder (image_builder.py's render_dockerfile) reinstalls
# py3.11-pip transiently, in its own layer, to install a caller's pinned
# python_packages, then removes it again in that same layer -- this base
# image itself never ships a package manager.
RUN rm -f /usr/bin/pip /usr/bin/pip3 /usr/bin/pip3.11 \
    && rm -rf /usr/lib/python3.11/ensurepip \
    && rm -rf /usr/lib/python3.11/site-packages/pip* \
    && apk del npm node-gyp || true \
    && rm -rf /usr/bin/npm /usr/bin/npx \
    && rm -rf /root/.cache/pip /root/.cache/uv

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
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1" \
    XDG_CONFIG_HOME="/tmp/.config" \
    XDG_CACHE_HOME="/tmp/.cache"

USER sandbox

WORKDIR /workspace

CMD ["tail", "-f", "/dev/null"]
