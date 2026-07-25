# syntax=docker/dockerfile:1.4

# Next.js-Ready Sandbox Container
# Same JS/TS-only positioning as deploy/sandbox-node.Dockerfile (no Python
# anywhere), plus the one thing a generic Node base doesn't give you: a
# real, dependency-installed Next.js (App Router) starter already sitting on
# disk, so a session can run a working `next dev`/`next build` immediately,
# with zero network access needed at session runtime. Closes another slice
# of docs/E2B-COMPARISON.md's "template gallery" gap -- the first base in
# the gallery scoped to a specific framework rather than a bare language
# runtime.
#
# WHY A PRE-INSTALLED TEMPLATE, NOT JUST "NODE + NPM, SCAFFOLD AT RUNTIME":
# deploy/sandbox-node.Dockerfile already gives any base a Node version well
# above Next.js's own documented floor (>=20.9 per nextjs.org/docs/app/
# getting-started/installation's "System requirements" section, checked
# directly rather than assumed from training data -- this image's
# nodejs-22 clears that with room to spare). So "just Node, npm removed,
# same as boxkite-node" would be the same file under a new name, adding
# nothing a caller couldn't already get from boxkite-node -- not worth a
# separate base. The obvious alternative -- have the agent invoke
# `npx create-next-app` itself at session runtime -- is deliberately NOT
# what this image does: create-next-app needs a live npm to write and then
# `npm install` a brand-new project's package.json, and this repo has
# already decided, for the whole JS/Python ecosystem, that a running
# session pod never gets a package manager back (see the npm-removal step
# below, and docs/DECLARATIVE-BUILDER-DESIGN.md section 2's explicit
# rejection of session-time installs for the same reason: it requires
# re-adding a package manager plus either a default network-egress hole or
# a build-phase-only egress carve-out -- materially more attack surface on
# the exact pod that subsequently runs untrusted agent-directed commands).
# Scaffolding a project is exactly a package-manager-needing,
# network-needing operation, so it happens once here, at image BUILD time
# -- the same place image_builder.py's render_dockerfile already does its
# own npm_packages layering for the declarative builder -- instead of
# being left to session runtime or a per-account declarative-builder request.
#
# WHY /opt/nextjs-template, NOT /workspace: deploy/pod-template.yaml mounts
# /workspace as a fresh, empty EmptyDir volume on every pod start (see the
# "workspace" volume mount on both the sandbox and sidecar containers) --
# anything a Dockerfile bakes into /workspace is silently shadowed and
# never reaches a real running session. The template therefore lives at
# /opt/nextjs-template, a path no volume mount overlays, and a session
# copies it into /workspace itself to start from it:
#   cp -r /opt/nextjs-template/. /workspace/ && cd /workspace && \
#     node_modules/.bin/next dev
# `node_modules/.bin/next` runs directly via node -- no npm needed at
# runtime, the same way any other globally-installed npm package's bin
# shim keeps working after `apk del npm` (see
# deploy/sandbox-node.Dockerfile and image_builder.py's render_dockerfile
# comment on the identical install-then-remove-npm pattern for
# declarative-builder npm_packages).
#
# SECURITY NOTES (identical posture to deploy/sandbox-node.Dockerfile):
# - npm is REMOVED after the template's dependencies are installed --
#   runtime only needs node, exactly like sandbox-node.Dockerfile.
# - No Python/pip anywhere in this image -- nothing to remove because it
#   was never installed, not merely stripped post-build.
# - Container runs with minimal environment variables (no API keys, credentials)
# - Generated commands can run in an isolated network namespace at runtime
# - Runs as non-root user (UID 1001)
# - /opt/nextjs-template is read-only at runtime (readOnlyRootFilesystem,
#   see deploy/pod-template.yaml) -- a session can only ever `cp` FROM it,
#   never modify the vendored template itself.

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

# Vendor a minimal, dependency-installed Next.js (App Router, plain
# JavaScript -- no TypeScript devDependencies, keeping the pinned surface
# here small; a caller who wants TypeScript can still layer it in via the
# declarative builder's npm_packages the same way any other base can) app
# at /opt/nextjs-template. --save-exact pins whatever next/react/react-dom
# versions resolve at THIS image's own build time into package.json -- the
# same "pin to what this specific build actually produced" discipline
# go-1.24/nodejs-22 already apply at the apk-package-name level above.
RUN mkdir -p /opt/nextjs-template/app \
    && cd /opt/nextjs-template \
    && npm init -y >/dev/null \
    && npm pkg set type="module" >/dev/null \
    && npm pkg set scripts.dev="next dev" scripts.build="next build" scripts.start="next start" >/dev/null \
    && npm install --save-exact --no-audit --no-fund next react react-dom \
    && node -e "if (!/^\d+\./.test(require('next/package.json').version)) process.exit(1)"

RUN printf '%s\n' \
    "/** @type {import('next').NextConfig} */" \
    "const nextConfig = {};" \
    "export default nextConfig;" \
    > /opt/nextjs-template/next.config.mjs

RUN printf '%s\n' \
    "export const metadata = {" \
    "  title: \"boxkite-nextjs\"," \
    "  description: \"Next.js-ready boxkite sandbox\"," \
    "};" \
    "" \
    "export default function RootLayout({ children }) {" \
    "  return (" \
    "    <html lang=\"en\">" \
    "      <body>{children}</body>" \
    "    </html>" \
    "  );" \
    "}" \
    > /opt/nextjs-template/app/layout.js

RUN printf '%s\n' \
    "export default function Page() {" \
    "  return <h1>Hello from boxkite-nextjs</h1>;" \
    "}" \
    > /opt/nextjs-template/app/page.js

RUN printf '%s\n' \
    "This is a pre-installed Next.js (App Router) starter, vendored outside" \
    "/workspace because /workspace is mounted fresh (empty) on every session." \
    "Copy it into /workspace to start from it:" \
    "" \
    "  cp -r /opt/nextjs-template/. /workspace/" \
    "  cd /workspace" \
    "  node_modules/.bin/next dev" \
    > /opt/nextjs-template/README

# SECURITY: Remove npm to prevent runtime package installation. The
# declarative builder (image_builder.py's render_dockerfile) reinstalls npm
# transiently, in its own layer, to install a caller's pinned npm_packages,
# then removes it again in that same layer -- this base image itself never
# ships a package manager, and neither does the vendored template above
# (its dependencies are already installed; only the npm binary goes away).
RUN apk del npm node-gyp || true \
    && rm -rf /usr/bin/npm /usr/bin/npx /root/.npm

# Create non-root user for sandbox execution
RUN adduser -D -u 1001 -s /bin/bash sandbox

# Create directories (will be mounted as EmptyDir volumes)
RUN mkdir -p /workspace /mnt/skills /mnt/user-data/uploads /mnt/user-data/outputs \
    && chown -R sandbox:sandbox /workspace /opt/nextjs-template \
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
