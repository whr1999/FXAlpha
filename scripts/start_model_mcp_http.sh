#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

HOST="${FXALPHA_MODEL_MCP_HOST:-127.0.0.1}"
PORT="${FXALPHA_MODEL_MCP_PORT:-8004}"

PYTHONPATH="${PWD}${PYTHONPATH:+:${PYTHONPATH}}" \
  python3 -m mcp_servers.model_server --transport http --host "${HOST}" --port "${PORT}"
