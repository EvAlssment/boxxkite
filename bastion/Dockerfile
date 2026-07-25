# syntax=docker/dockerfile:1.4
#
# bastion/ container -- a standalone SSH listener, deliberately NOT part of
# the sandbox/sidecar pod (see docs/SSH-BASTION-DESIGN.md and this
# component's own README). Unlike control-plane/Dockerfile, this image has
# no dependency on the sibling `boxkite` package -- bastion/ never talks to
# the sidecar or SandboxManager directly, only to control-plane's public
# HTTP/WS API -- so it can build standalone from this directory:
#
#   docker build -t boxkite-bastion:local bastion/

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY --link pyproject.toml ./pyproject.toml
COPY --link src ./src

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir .

FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY --link --from=builder /opt/venv /opt/venv

RUN adduser --disabled-password --gecos "" --uid 1001 bastion \
    && chown -R bastion:bastion /app

# 2222, not 22 -- no CAP_NET_BIND_SERVICE/root needed to bind it. Front this
# with a LoadBalancer/NodePort mapping to 22 externally if desired.
EXPOSE 2222

USER bastion

CMD ["boxkite-bastion"]
