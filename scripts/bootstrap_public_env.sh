#!/usr/bin/env bash
set -euo pipefail

export PIP_DISABLE_PIP_VERSION_CHECK=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-${ROOT}/.venv}"

"${PYTHON_BIN}" -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install \
  -c "${ROOT}/requirements/constraints-tested.txt" \
  -e "${ROOT}/third_party/qlib" \
  -e "${ROOT}/third_party/tushare" \
  -e "${ROOT}/third_party/quantgpt" \
  -e "${ROOT}[dev]"

"${VENV_DIR}/bin/python" -m pip check
"${VENV_DIR}/bin/python" - <<'PY'
import importlib.util

for retired in ("vnpy", "vnpy_paperaccount", "vnpy_portfoliostrategy"):
    if importlib.util.find_spec(retired) is not None:
        raise SystemExit(f"retired dependency is importable: {retired}")
PY

echo "FXAlpha environment ready: ${VENV_DIR}"
