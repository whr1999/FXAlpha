#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE_DIR="$ROOT/deploy/systemd"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$UNIT_DIR"
install -m 0644 "$SOURCE_DIR/fxalpha-quantgpt-8003.service" "$UNIT_DIR/fxalpha-quantgpt-8003.service"
install -m 0644 "$SOURCE_DIR/fxalpha-api-18081.service" "$UNIT_DIR/fxalpha-api-18081.service"
install -m 0644 "$SOURCE_DIR/fxalpha-factor-stack.target" "$UNIT_DIR/fxalpha-factor-stack.target"

systemctl --user daemon-reload
systemctl --user enable fxalpha-factor-stack.target fxalpha-api-18081.service fxalpha-quantgpt-8003.service

if [[ "${1:-}" == "--start" ]]; then
  api_fragment="$(systemctl --user show fxalpha-api-18081.service -p FragmentPath --value 2>/dev/null || true)"
  if [[ "$api_fragment" == /run/user/*/systemd/transient/fxalpha-api-18081.service ]]; then
    systemctl --user stop fxalpha-api-18081.service
    [[ -f "$api_fragment" ]] && unlink "$api_fragment"
    systemctl --user daemon-reload
  fi

  old_qgpt_pid="$(lsof -tiTCP:8003 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
  managed_qgpt_pid="$(systemctl --user show fxalpha-quantgpt-8003.service -p MainPID --value 2>/dev/null || true)"
  if [[ -n "$old_qgpt_pid" && "$old_qgpt_pid" != "$managed_qgpt_pid" ]]; then
    old_qgpt_cmd="$(tr '\0' ' ' < "/proc/$old_qgpt_pid/cmdline" 2>/dev/null || true)"
    if [[ "$old_qgpt_cmd" == *"python3 -m quantgpt --transport http"* ]]; then
      kill -TERM "$old_qgpt_pid"
      for _ in 1 2 3 4 5; do
        kill -0 "$old_qgpt_pid" 2>/dev/null || break
        sleep 1
      done
    else
      echo "Port 8003 is owned by an unknown process; refusing to kill it: $old_qgpt_pid $old_qgpt_cmd" >&2
      exit 1
    fi
  fi

  systemctl --user restart fxalpha-quantgpt-8003.service
  systemctl --user restart fxalpha-api-18081.service
  systemctl --user start fxalpha-factor-stack.target
fi

echo "Installed FXAlpha factor-research services in $UNIT_DIR"
echo "Start: systemctl --user start fxalpha-factor-stack.target"
echo "Stop services: systemctl --user stop fxalpha-factor-stack.target fxalpha-api-18081.service fxalpha-quantgpt-8003.service"
