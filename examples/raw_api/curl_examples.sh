#!/usr/bin/env bash
# Raw HTTP calls against the boxkite sidecar -- no LangChain, no boxkite
# Python package at all. Useful if you're integrating from a different
# language/agent framework and just want the wire contract.
#
# Talks directly to the sidecar started by `boxkite up` (or
# `docker compose -f deploy/docker-compose.yml up`), NOT the hosted
# control-plane -- see ../hosted_control_plane for that flow, which wraps
# these same operations behind session-scoped, authenticated routes.
#
# Request/response shapes here match sidecar/main.py's Pydantic models
# exactly (ExecRequest/ExecResponse, FileCreateRequest/FileCreateResponse,
# ViewRequest/ViewResponse) as of this writing.
#
# Prerequisites:
#   boxkite up   (from the repo root)
#
# Usage:
#   ./curl_examples.sh

set -euo pipefail

SIDECAR_URL="${SIDECAR_URL:-http://localhost:8080}"

if [ -z "${SIDECAR_AUTH_TOKEN:-}" ]; then
  LOCAL_ENV="$HOME/.boxkite/local.env"
  if [ -f "$LOCAL_ENV" ]; then
    SIDECAR_AUTH_TOKEN="$(grep '^SIDECAR_AUTH_TOKEN=' "$LOCAL_ENV" | cut -d= -f2)"
  fi
fi

if [ -z "${SIDECAR_AUTH_TOKEN:-}" ]; then
  echo "Set SIDECAR_AUTH_TOKEN, or run 'boxkite up' first so ~/.boxkite/local.env has one." >&2
  exit 1
fi

echo "== /health (no auth required) =="
curl -sS "$SIDECAR_URL/health"
echo -e "\n"

echo "== POST /exec =="
curl -sS -X POST "$SIDECAR_URL/exec" \
  -H "Content-Type: application/json" \
  -H "X-Sidecar-Auth-Token: $SIDECAR_AUTH_TOKEN" \
  -d '{"command": "python3 -c \"print(21 * 2)\"", "timeout": 30}'
echo -e "\n"

echo "== POST /file-create =="
curl -sS -X POST "$SIDECAR_URL/file-create" \
  -H "Content-Type: application/json" \
  -H "X-Sidecar-Auth-Token: $SIDECAR_AUTH_TOKEN" \
  -d '{"path": "raw_api_hello.txt", "content": "hello from the raw sidecar API\n"}'
echo -e "\n"

echo "== POST /view (read the file back) =="
curl -sS -X POST "$SIDECAR_URL/view" \
  -H "Content-Type: application/json" \
  -H "X-Sidecar-Auth-Token: $SIDECAR_AUTH_TOKEN" \
  -d '{"path": "raw_api_hello.txt"}'
echo -e "\n"

echo "== POST /view on a directory (listing) =="
curl -sS -X POST "$SIDECAR_URL/view" \
  -H "Content-Type: application/json" \
  -H "X-Sidecar-Auth-Token: $SIDECAR_AUTH_TOKEN" \
  -d '{"path": "/workspace"}'
echo -e "\n"

echo "== POST /str-replace =="
curl -sS -X POST "$SIDECAR_URL/str-replace" \
  -H "Content-Type: application/json" \
  -H "X-Sidecar-Auth-Token: $SIDECAR_AUTH_TOKEN" \
  -d '{"path": "raw_api_hello.txt", "old_str": "hello", "new_str": "greetings"}'
echo -e "\n"

echo "Done."
