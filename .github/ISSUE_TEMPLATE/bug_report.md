---
name: Bug report
about: Something isn't working the way it should
title: "[Bug] "
labels: bug
---

**Do not use this template for security vulnerabilities** — see
[SECURITY.md](../../SECURITY.md) for private reporting instead.

## What happened

A clear description of the bug.

## What you expected

What you expected to happen instead.

## Environment

- Deployment path: [docker-compose / local-kind / a real K8s cluster]
- boxkite version / commit:
- Storage backend: [S3 / Azure Blob / none configured]
- Kubernetes version (if applicable):

## Reproduction

Steps to reproduce, ideally including the exact `bash_tool`/`file_create`/
`str_replace`/`present_files` call or sidecar HTTP request that triggers it.

## Logs

Relevant `sandbox`/`sidecar` container logs (`docker compose logs sidecar` or
`kubectl logs <pod> -c sidecar`), redacted of any credentials.
