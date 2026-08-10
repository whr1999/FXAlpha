#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

HOST="${FXALPHA_API_HOST:-127.0.0.1}"
PORT="${FXALPHA_API_PORT:-18081}"
FXALPHA_RUNTIME_TMP="${FXALPHA_RUNTIME_TMP:-$PWD/runtime/tmp}"
mkdir -p "$FXALPHA_RUNTIME_TMP"
export TMPDIR="$FXALPHA_RUNTIME_TMP"
export TEMP="$FXALPHA_RUNTIME_TMP"
export TMP="$FXALPHA_RUNTIME_TMP"

python3 -c "from api_server import start_api; start_api('${HOST}', int('${PORT}'))"
