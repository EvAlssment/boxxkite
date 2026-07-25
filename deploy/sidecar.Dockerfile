# syntax=docker/dockerfile:1.4

# Sidecar Container
# Runs alongside sandbox container in K8s pod
# Handles HTTP API for tool execution and cloud storage sync (S3/Azure Blob)

ARG PYTHON_VERSION=3.11
ARG UV_VERSION=0.11.2

FROM cgr.dev/chainguard/wolfi-base:latest@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795 AS python-base
ARG PYTHON_VERSION

RUN apk update && apk add --no-cache \
    python-${PYTHON_VERSION} \
    ca-certificates \
    && ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python3 \
    && ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

FROM python-base AS builder
ARG PYTHON_VERSION
ARG UV_VERSION

RUN apk update && apk add --no-cache py3.11-pip

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:/usr/local/bin:/usr/bin:/bin"

WORKDIR /app

RUN python -m venv "$VIRTUAL_ENV" && \
    pip install --no-cache-dir uv==${UV_VERSION}

# Copy lockfile after the venv/uv bootstrap so lockfile-only edits only rerun
# dependency installation.
COPY --link sidecar/requirements.lock .
RUN --mount=type=cache,id=uv-sidecar,target=/root/.cache/uv \
    UV_LINK_MODE=copy uv pip install --python "$VIRTUAL_ENV/bin/python" -r requirements.lock && \
    python -m pip uninstall -y uv && \
    rm -f "$VIRTUAL_ENV/bin/pip" "$VIRTUAL_ENV/bin/pip3" "$VIRTUAL_ENV/bin/pip${PYTHON_VERSION}" && \
    rm -rf "$VIRTUAL_ENV/lib/python${PYTHON_VERSION}/site-packages/pip"*

FROM python-base
ARG PYTHON_VERSION

# Install system dependencies:
# - util-linux-misc provides nsenter for exec into the sandbox container in K8s
# - docker-cli supports local compose mode
# - tmux backs the /pty takeover route's persistent session (GitHub issues
#   #130/#144) -- it now runs as the SIDECAR's own process (never nsentered/
#   docker-exec'd into the sandbox), so the binary belongs in THIS image,
#   not any sandbox-*.Dockerfile. See sidecar/sidecar_pty.py's module-level
#   comment for the full reasoning.
RUN apk update && apk add --no-cache \
    util-linux-misc \
    procps \
    curl \
    docker-cli \
    tmux

WORKDIR /app

COPY --link --from=builder /opt/venv /opt/venv

# Copy application code (main.py plus the sidecar_* concern modules it wires
# together -- see GitHub issue #71; the sidecar is launched as `python main.py`
# with /app on sys.path, so the sibling modules must be copied alongside it).
# Wildcarded rather than named one-by-one: an explicit list silently stops
# covering a new sidecar_*.py module the moment one is added (this shipped a
# ModuleNotFoundError crash-loop in production when sidecar_node_interpreter.py
# and sidecar_browser.py were added but never added to this list).
COPY --link sidecar/*.py ./

# Create directories for shared volumes and a non-root user.
# The image defaults to non-root (satisfies CIS/Prisma 5041). K8s pod specs
# override this container to UID 0 where nsenter needs it (see
# deploy/pod-template.yaml, src/boxkite/manager.py, and src/boxkite/warm_pool.py);
# nsenter then drops to UID 1001 (--setuid/--setgid) before running agent code.
# /run/boxkite holds the takeover tmux control socket (GitHub issues
# #130/#144) -- deliberately NOT under /mnt, /workspace, or any other path
# shared with the sandbox container; see sidecar/sidecar_pty.py. Also
# created lazily at runtime (os.makedirs(..., exist_ok=True)) so this is a
# belt-and-suspenders step, not the only place it's ensured to exist.
RUN mkdir -p /mnt/skills /mnt/user-data/uploads /mnt/user-data/outputs /workspace /run/boxkite \
    && adduser -D -u 1001 sidecar \
    && chown -R sidecar:sidecar /app /mnt/user-data /workspace /run/boxkite

# Environment defaults
ENV RUNTIME_MODE=k8s \
    STORAGE_BACKEND=s3 \
    S3_BUCKET=boxkite-sandbox \
    AWS_REGION=us-east-1 \
    AZURE_STORAGE_CONTAINER=boxkite-sandbox \
    PATH="/opt/venv/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# Default to non-root for image-level least privilege. K8s pod specs override
# this to root at runtime where the nsenter/bash_tool path requires it.
USER sidecar

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "main.py"]
