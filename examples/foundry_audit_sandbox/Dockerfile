# syntax=docker/dockerfile:1.4
#
# Foundry/Anvil smart-contract-audit sandbox (GitHub issue #137).
#
# Extends boxkite-minimal's own recipe (deploy/sandbox-minimal.Dockerfile:
# lean python+node base, no data-science/document/browser stack) with git +
# openssh-client (so an agent can git_clone a real target repo to audit --
# same addition deploy/sandbox-claude-code.Dockerfile makes, for the same
# reason) and Foundry (forge/anvil/cast/chisel), pinned to v1.0.0.
#
# WHY THIS IS A HAND-MAINTAINED DOCKERFILE, NOT A DECLARATIVE-BUILDER IMAGE
# (docs/DECLARATIVE-BUILDER-DESIGN.md): verified directly against Foundry's
# own current install docs (getfoundry.sh) before writing this file --
# Foundry publishes NO apt/apk/npm/pip package at all. "Foundry no longer
# publishes npm packages for forge, cast, anvil, or chisel"; its only
# supported install paths are the foundryup installer script
# (`curl -L https://foundry.paradigm.xyz | bash`, then `foundryup`),
# GitHub release tarballs, Docker, or building from source via cargo. None
# of those fit `SandboxImageBuildRequest`'s pinned
# apt_packages/python_packages/npm_packages fields, and the declarative
# builder deliberately has no raw-command/Dockerfile-passthrough escape
# hatch (see that doc's section 5, "no raw Dockerfile passthrough"). This is
# a real, narrower gap in what the declarative builder can express for this
# specific tool -- not a workaround of it -- the same class of gap
# deploy/sandbox-claude-code.Dockerfile's own header documents for
# npm-global-installing Claude Code before `npm_packages` existed.
#
# SECURITY NOTES (same posture as every other boxkite base image):
# - pip/npm are REMOVED after use; forge/anvil/cast/chisel need neither at
#   runtime, and leaving a package manager in the image would reintroduce
#   the "runtime package install" risk this project deliberately removes
#   elsewhere.
# - forge-std and the solc 0.8.24 compiler are fetched and cached ONCE, at
#   BUILD time, under /opt/foundry-audit-template -- deliberately NOT under
#   /workspace or /tmp, both of which are mounted as emptyDir volumes at pod
#   start (deploy/pod-template.yaml) and would silently shadow anything
#   baked into the image at those paths. This is the ONLY time this image's
#   Solidity tooling ever needs network egress: at session runtime, the
#   sandbox pod has no egress to anything but itself (see README.md's
#   network-isolation section) -- if solc or forge-std had to be fetched
#   lazily on first use instead of here, every session's first
#   `forge build` would hang or fail closed.
# - Runs as non-root user (UID 1001), same as every other boxkite base
#   image; verified directly against the pod's actual security context
#   (readOnlyRootFilesystem, cap-drop ALL, --network none) -- see
#   README.md's "What was verified, and how" section.

ARG PYTHON_VERSION=3.11

FROM cgr.dev/chainguard/wolfi-base:latest
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
    git \
    && ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python3 \
    && ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python \
    && node --version | grep -Eq '^v22\.' \
    && npm --version | grep -Eq '^1[12]\.'

# SECURITY: remove pip/npm after use -- see the header comment above.
RUN rm -f /usr/bin/pip /usr/bin/pip3 /usr/bin/pip3.11 \
    && rm -rf /usr/lib/python3.11/ensurepip \
    && rm -rf /usr/lib/python3.11/site-packages/pip* \
    && apk del npm node-gyp || true \
    && rm -rf /usr/bin/npm /usr/bin/npx \
    && rm -rf /root/.cache/pip /root/.cache/uv /root/.npm

# ---------------------------------------------------------------------------
# Foundry (forge/anvil/cast/chisel), pinned to v1.0.0.
#
# `foundryup --install v1.0.0` verifies each downloaded binary's SHA by
# default when an attestation exists for the release (the `-f/--force` flag
# exists specifically to skip that verification -- deliberately never
# passed here). Note: v1.0.0 (Feb 2025) predates Foundry's
# attestation-based verification, so foundryup logs "no attestation found
# for this release, skipping SHA verification" for this exact pinned
# version -- binaries are still fetched over HTTPS from official GitHub
# releases either way, but this specific pin does not get the SHA check a
# newer release would. Binaries land under
# /root/.foundry/bin; copied to /usr/local/bin (already on every boxkite
# image's PATH) and the original directory removed, so nothing depends on
# root's home directory surviving into the final image.
RUN mkdir -p /usr/local/bin \
    && curl -L https://foundry.paradigm.xyz | bash \
    && /root/.foundry/bin/foundryup --install v1.0.0 \
    && cp /root/.foundry/bin/forge /root/.foundry/bin/cast \
       /root/.foundry/bin/anvil /root/.foundry/bin/chisel /usr/local/bin/ \
    && rm -rf /root/.foundry/bin \
    && forge --version && anvil --version && cast --version

# ---------------------------------------------------------------------------
# Bake the example audit project (toy vulnerable/patched contracts,
# forge-std, and a warmed solc 0.8.24 cache) at BUILD time -- see the header
# comment for why /opt/foundry-audit-template specifically, not /workspace.
WORKDIR /opt/foundry-audit-template
COPY contracts/ ./contracts/
COPY scripts/ ./scripts/
RUN cd contracts \
    && git init -q && git config user.email audit@example.invalid && git config user.name audit \
    && mkdir -p lib && forge install foundry-rs/forge-std --no-commit -q \
    && HOME=/opt/foundry-audit-template forge build \
    && HOME=/opt/foundry-audit-template forge test -vv \
    && rm -rf .git .gitmodules \
    && chmod a+x /opt/foundry-audit-template/scripts/deploy_and_exploit.sh \
    && chmod -R a+rX /opt/foundry-audit-template

# Create non-root user for sandbox execution (same UID/GID as every other
# boxkite base image).
RUN adduser -D -u 1001 -s /bin/bash sandbox

# Create directories (mounted as EmptyDir volumes at runtime -- see
# deploy/pod-template.yaml. /opt/foundry-audit-template above is
# deliberately NOT one of these.)
RUN mkdir -p /workspace /mnt/skills /mnt/user-data/uploads /mnt/user-data/outputs \
    && chown -R sandbox:sandbox /workspace \
    && chmod 1777 /tmp /var/tmp

# SECURITY: minimal environment variables only -- no API keys, credentials,
# or RPC URLs. HOME=/workspace matches every other boxkite base image, and
# is why the seed step (see README.md / run_audit_sandbox.py) copies
# /opt/foundry-audit-template's contents -- including the pre-warmed
# .svm/.foundry caches -- into /workspace before the first forge command:
# forge/svm resolve their cache under $HOME.
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
