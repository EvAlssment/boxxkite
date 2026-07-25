# syntax=docker/dockerfile:1.4
#
# Control-plane API container.
#
# Build from the REPO ROOT (matches deploy/sidecar.Dockerfile's convention),
# because this service depends on the sibling `boxkite` package at ../src:
#
#   docker build -f control-plane/Dockerfile -t boxkite-control-plane:local .
#
# This image is intentionally separate from sidecar.Dockerfile/sandbox.Dockerfile:
# it never runs alongside agent-generated code and holds no elevated Linux
# capabilities. It talks to SandboxManager (which talks to the K8s API and the
# per-pod sidecars), never to a sandbox pod's filesystem directly.

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Copy the whole repo (both the `boxkite` package and control-plane) so the
# editable `-e ..` requirement resolves.
COPY --link src ./src
COPY --link pyproject.toml ./pyproject.toml
COPY --link control-plane ./control-plane

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN cd control-plane && pip install --no-cache-dir -r requirements.txt

FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --link --from=builder /opt/venv /opt/venv
COPY --link --from=builder /build/src ./src
COPY --link control-plane/src ./control-plane/src
# alembic.ini + migrations/ -- read at startup by db.py's init_schema()
# (via migrations_runner.py) to apply pending schema migrations. Not
# under control-plane/src, so needs its own explicit COPY.
COPY --link control-plane/alembic.ini ./control-plane/alembic.ini
COPY --link control-plane/migrations ./control-plane/migrations

RUN adduser --disabled-password --gecos "" --uid 1001 controlplane \
    && chown -R controlplane:controlplane /app

ENV PYTHONPATH="/app/src:/app/control-plane/src"

EXPOSE 8090

USER controlplane

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8090/health || exit 1

# --proxy-headers/--forwarded-allow-ips=* trusts X-Forwarded-Proto/-For from
# whatever connects to this container -- safe here specifically because this
# image is deployed behind Cloud Run's front-end proxy, which is the only
# thing that can reach the container at all (no direct public ingress
# bypassing it). Without this, request.base_url() (used to build OAuth
# redirect_uri values in routers/oauth.py and routers/social_login.py, and
# the AS metadata issuer/*_endpoint fields) reports the scheme Cloud Run's
# internal proxy uses to reach the container -- http -- instead of the
# externally-visible https, producing a redirect_uri that does not match
# what's registered with GitHub/Google and breaks the OAuth flow entirely.
CMD ["uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8090", "--proxy-headers", "--forwarded-allow-ips=*"]
