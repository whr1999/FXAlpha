from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_TMP = REPO_ROOT / "runtime" / "tmp"
SERVICE_RUNTIME = REPO_ROOT / "runtime" / "api_logs"
PID_FILE = SERVICE_RUNTIME / "fxalpha_api_18081.pid"
RUNTIME_TMP.mkdir(parents=True, exist_ok=True)
for _tmp_key in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_tmp_key] = str(RUNTIME_TMP)

sys.path.insert(0, str(REPO_ROOT))

from api_server import start_api


PORT = 18081
THIS_SCRIPT = REPO_ROOT / "scripts" / "start_fxalpha_api_18081.py"
PREFERRED_UNIT = "fxalpha-api-18081.service"


def _process_cmdline(pid: str | int) -> str:
    try:
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _process_systemd_unit(pid: str | int) -> str:
    try:
        cgroup = Path(f"/proc/{int(pid)}/cgroup").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return PREFERRED_UNIT if PREFERRED_UNIT in cgroup else ""


def port_owner(port: int = PORT) -> dict[str, str]:
    try:
        completed = subprocess.run(
            ["ss", "-ltnp", f"sport = :{int(port)}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except Exception as exc:
        return {"error": str(exc)}
    match = re.search(r"pid=(\d+)", completed.stdout or "")
    if not match:
        return {}
    pid = match.group(1)
    return {
        "pid": pid,
        "cmd": _process_cmdline(pid),
        "unit": _process_systemd_unit(pid),
    }


def validate_single_owner(port: int = PORT) -> None:
    owner = port_owner(port)
    if not owner or owner.get("error"):
        return
    pid = owner.get("pid", "")
    if pid and pid == str(os.getpid()):
        return
    cmd = owner.get("cmd", "")
    unit = owner.get("unit", "")
    if unit == PREFERRED_UNIT:
        raise SystemExit(f"fxalpha_api_port_owned_by_systemd:{pid}:{unit}")
    if str(THIS_SCRIPT) in cmd or "scripts/start_fxalpha_api_18081.py" in cmd:
        raise SystemExit(f"fxalpha_api_port_owned_by_orphan_direct_launcher:{pid}:{cmd}")
    raise SystemExit(f"fxalpha_api_port_18081_in_use:{pid}:{cmd or 'unknown'}")


def main() -> None:
    validate_single_owner(PORT)
    SERVICE_RUNTIME.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        start_api(host="127.0.0.1", port=PORT)
    finally:
        try:
            if PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
                PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()
