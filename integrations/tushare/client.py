from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml
from requests.adapters import HTTPAdapter


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_PROXY_KEYS = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
]
_NO_PROXY_KEYS = ["NO_PROXY", "no_proxy"]
_DIRECT_HOSTNAMES = ["api.waditu.com", "api.tushare.pro"]
_DEFAULT_DIRECT_DNS_SERVERS = ["223.5.5.5", "114.114.114.114"]
_DEFAULT_DIRECT_IPS = ["8.140.225.26", "60.205.198.20"]
_WINDOWS_PROXY_IFACE_KEYWORDS = (
    "flclash",
    "clash",
    "mihomo",
    "tun",
    "tap",
    "wintun",
    "wireguard",
    "tailscale",
    "zerotier",
    "vpn",
)
_WINDOWS_POWERSHELL_CANDIDATES = (
    Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"),
    Path("/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe"),
)


def _load_config() -> dict[str, Any]:
    config_path = PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def get_tushare_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = (_load_config().get("data_foundation", {}) or {}).get("tushare", {}) or {}
    merged: dict[str, Any] = {
        "token": str(cfg.get("token") or "").strip(),
        "api_host": str(cfg.get("api_host") or "api.waditu.com").strip(),
        "api_port": int(cfg.get("api_port") or 80),
        "api_scheme": str(cfg.get("api_scheme") or "http").strip().lower(),
        "api_timeout_seconds": float(cfg.get("api_timeout_seconds") or 20.0),
        "dns_timeout_seconds": float(cfg.get("dns_timeout_seconds") or 3.0),
        "dns_servers": list(cfg.get("dns_servers") or _DEFAULT_DIRECT_DNS_SERVERS),
        "direct_ip_candidates": list(cfg.get("direct_ip_candidates") or _DEFAULT_DIRECT_IPS),
    }
    if overrides:
        for key, value in overrides.items():
            if value is not None:
                merged[key] = value
    return merged


def get_tushare_token() -> str:
    config = get_tushare_config()
    token = str(config.get("token") or "").strip()
    if token:
        return token
    env_token = str(os.environ.get("TUSHARE_TOKEN") or "").strip()
    if env_token:
        return env_token
    raise RuntimeError("tushare_token_missing")


_PROXY_TUN_IFACE_PREFIXES = ("tailscale", "tun", "tap", "wg", "ppp", "zt", "utun")


def _is_proxy_tun_iface(iface: str) -> bool:
    text = str(iface or "").strip().lower()
    return any(text.startswith(prefix) for prefix in _PROXY_TUN_IFACE_PREFIXES)


def _route_uses_proxy_tun(route: str) -> bool:
    line = str(route or "").strip().lower()
    if "198.18." in line:
        return True
    parts = line.split()
    try:
        dev_idx = parts.index("dev")
        iface = parts[dev_idx + 1]
    except (ValueError, IndexError):
        return False
    return _is_proxy_tun_iface(iface)


def _is_wsl_environment() -> bool:
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    for path in (Path("/proc/sys/kernel/osrelease"), Path("/proc/version")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except Exception:
            continue
        if "microsoft" in text or "wsl" in text:
            return True
    return False


def _wsl_path_to_windows(path: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["wslpath", "-w", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    value = (proc.stdout or "").strip()
    return value if proc.returncode == 0 and value else None


def _windows_powershell_executable() -> str | None:
    """Resolve PowerShell even when a systemd user service has a Linux-only PATH."""
    configured = str(os.environ.get("FXALPHA_POWERSHELL_EXE") or "").strip()
    if configured and Path(configured).is_file():
        return configured
    discovered = shutil.which("powershell.exe") or shutil.which("PowerShell.exe")
    if discovered:
        return discovered
    for candidate in _WINDOWS_POWERSHELL_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    return None


def _windows_host_route_preflight(hosts: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "skipped",
        "reason": None,
        "hosts": {},
        "issues": [],
        "warnings": [],
    }
    if not _is_wsl_environment():
        report["reason"] = "not_wsl"
        return report
    script = PROJECT_ROOT / "scripts" / "tushare_direct_route_guard.ps1"
    report["script"] = str(script)
    if not script.exists():
        report["status"] = "failed"
        report["issues"].append("host_tushare_route_guard_missing")
        return report
    windows_script = _wsl_path_to_windows(script)
    if not windows_script:
        report["status"] = "failed"
        report["issues"].append("host_tushare_route_guard_path_unavailable")
        return report
    powershell = _windows_powershell_executable()
    if not powershell:
        report["status"] = "failed"
        report["issues"].append("host_tushare_route_guard_unavailable")
        report["warnings"].append("powershell_executable_not_found")
        return report
    cmd = [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        windows_script,
        "-Mode",
        "check",
        "-Json",
        "-TushareIps",
        ",".join(hosts),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    except Exception as exc:
        report["status"] = "failed"
        report["issues"].append("host_tushare_route_guard_unavailable")
        report["warnings"].append(repr(exc))
        return report
    raw = (proc.stdout or "").strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        report["status"] = "failed"
        report["issues"].append("host_tushare_route_guard_invalid_json")
        if raw:
            report["warnings"].append(raw[-1000:])
        if proc.stderr:
            report["warnings"].append(proc.stderr.strip()[-1000:])
        return report
    if not isinstance(parsed, dict):
        report["status"] = "failed"
        report["issues"].append("host_tushare_route_guard_invalid_payload")
        return report
    parsed.setdefault("issues", [])
    parsed.setdefault("warnings", [])
    parsed.setdefault("hosts", {})
    if proc.returncode != 0 and parsed.get("status") == "ok":
        parsed["status"] = "failed"
        parsed["issues"].append("host_tushare_route_guard_nonzero_exit")
    return parsed


def _iface_ipv4_address(iface: str) -> str | None:
    try:
        proc = subprocess.run(
            ["bash", "-lc", f"ip -4 -o addr show dev {iface} scope global | awk '{{print $4}}' | cut -d/ -f1 | head -1"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    value = (proc.stdout or "").strip()
    return value or None


def _pick_non_tun_default_route() -> tuple[str, str, str | None] | None:
    try:
        proc = subprocess.run(
            ["bash", "-lc", "ip route show default"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line.startswith("default via "):
            continue
        parts = line.split()
        try:
            via_idx = parts.index("via")
            dev_idx = parts.index("dev")
        except ValueError:
            continue
        gateway = parts[via_idx + 1]
        iface = parts[dev_idx + 1]
        if gateway.startswith("198.18.") or _is_proxy_tun_iface(iface):
            continue
        return gateway, iface, _iface_ipv4_address(iface)
    return None


def _route_get(host: str) -> str:
    proc = subprocess.run(
        ["bash", "-lc", f"ip route get {host} ipproto tcp"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (proc.stdout or "").strip()


def _tcp_route_tables() -> list[str]:
    tables = ["main"]
    try:
        proc = subprocess.run(
            ["bash", "-lc", "ip rule show"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return tables
    if proc.returncode != 0:
        return tables
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if "ipproto tcp" not in line or " lookup " not in line:
            continue
        table = line.rsplit(" lookup ", 1)[-1].strip().split()[0]
        if table and table not in ("local", "default") and table not in tables:
            tables.append(table)
    return tables


def _ensure_direct_policy_rules(hosts: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {"status": "unknown", "hosts": {}, "issues": []}
    for host in hosts:
        before = subprocess.run(
            ["bash", "-lc", f'ip rule show | grep -F "to {host} lookup main" || true'],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        host_report = {"before": before, "after": None, "pinned": False, "error": None}
        if before:
            host_report["after"] = before
            host_report["pinned"] = True
            report["hosts"][host] = host_report
            continue
        proc = subprocess.run(
            ["sudo", "-n", "ip", "rule", "add", "to", host, "lookup", "main", "pref", "0"],
            capture_output=True,
            text=True,
            check=False,
        )
        after = subprocess.run(
            ["bash", "-lc", f'ip rule show | grep -F "to {host} lookup main" || true'],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        host_report["after"] = after
        host_report["pinned"] = bool(after)
        if proc.returncode != 0 and not after:
            host_report["error"] = (proc.stderr or proc.stdout or "").strip()
            report["issues"].append(f"direct_policy_rule_failed:{host}")
        if not host_report["pinned"]:
            report["issues"].append(f"direct_policy_rule_not_effective:{host}")
        report["hosts"][host] = host_report
    report["status"] = "ok" if not report["issues"] else "failed"
    return report


def _ensure_direct_host_routes(hosts: list[str], *, execute: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {"status": "unknown", "selected_route": None, "hosts": {}, "issues": []}
    route = _pick_non_tun_default_route()
    if not route:
        report["status"] = "failed"
        report["issues"].append("non_tun_default_route_missing")
        return report
    gateway, iface, source = route
    report["selected_route"] = {"gateway": gateway, "iface": iface, "source": source}
    route_tables = _tcp_route_tables()
    report["route_tables"] = route_tables
    if execute:
        policy_rules = _ensure_direct_policy_rules(hosts)
    else:
        policy_rules = {
            "status": "check_only",
            "hosts": {
                host: {
                    "before": subprocess.run(
                        ["bash", "-lc", f'ip rule show | grep -F "to {host} lookup main" || true'],
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout.strip(),
                }
                for host in hosts
            },
            "issues": [],
        }
    report["policy_rules"] = policy_rules
    report["issues"].extend(policy_rules.get("issues", []))
    for host in hosts:
        current = _route_get(host)
        host_report = {"before": current, "after": None, "pinned": False, "error": None}
        if (
            gateway in current
            and f"dev {iface}" in current
            and (not source or f"src {source}" in current)
            and not _route_uses_proxy_tun(current)
        ):
            host_report["after"] = current
            host_report["pinned"] = True
            report["hosts"][host] = host_report
            continue
        if not execute:
            host_report["after"] = current
            host_report["error"] = "direct_route_not_pinned_check_only"
            report["issues"].append(f"direct_route_not_ready:{host}")
            report["hosts"][host] = host_report
            continue
        errors: list[str] = []
        for table in route_tables:
            cmd = ["sudo", "-n", "ip", "route", "replace"]
            if table != "main":
                cmd.extend(["table", table])
            cmd.extend([host, "via", gateway, "dev", iface])
            if source:
                cmd.extend(["src", source])
            cmd.extend(["metric", "5"])
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                errors.append(f"{table}:{(proc.stderr or proc.stdout or '').strip()}")
        subprocess.run(["sudo", "-n", "ip", "route", "flush", "cache"], capture_output=True, text=True, check=False)
        after = _route_get(host)
        host_report["after"] = after
        host_report["pinned"] = (
            gateway in after
            and f"dev {iface}" in after
            and (not source or f"src {source}" in after)
            and not _route_uses_proxy_tun(after)
        )
        if errors:
            host_report["error"] = "; ".join(errors)
            report["issues"].append(f"direct_route_pin_failed:{host}")
        if not host_report["pinned"]:
            report["issues"].append(f"direct_route_pin_not_effective:{host}")
        report["hosts"][host] = host_report
    report["status"] = "ok" if not report["issues"] else "failed"
    return report


def _encode_dns_name(hostname: str) -> bytes:
    parts = [part for part in hostname.strip(".").split(".") if part]
    return b"".join(bytes([len(part)]) + part.encode("ascii") for part in parts) + b"\x00"


def _decode_dns_name(payload: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    cursor = offset
    next_offset = offset
    while cursor < len(payload):
        length = payload[cursor]
        if length == 0:
            cursor += 1
            if not jumped:
                next_offset = cursor
            break
        if length & 0xC0 == 0xC0:
            pointer = ((length & 0x3F) << 8) | payload[cursor + 1]
            cursor = pointer
            if not jumped:
                next_offset = offset + 2
            jumped = True
            continue
        cursor += 1
        labels.append(payload[cursor : cursor + length].decode("ascii", errors="ignore"))
        cursor += length
        if not jumped:
            next_offset = cursor
    return ".".join(labels), next_offset


def _dns_query_a(hostname: str, server: str, *, bind_ip: str | None, timeout: float) -> list[str]:
    transaction_id = secrets.randbelow(65535)
    query = struct.pack("!HHHHHH", transaction_id, 0x0100, 1, 0, 0, 0)
    query += _encode_dns_name(hostname)
    query += struct.pack("!HH", 1, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        if bind_ip:
            sock.bind((bind_ip, 0))
        sock.sendto(query, (server, 53))
        payload, _ = sock.recvfrom(4096)
    finally:
        sock.close()
    if len(payload) < 12:
        return []
    resp_id, _, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", payload[:12])
    if resp_id != transaction_id:
        return []
    offset = 12
    for _ in range(qdcount):
        _, offset = _decode_dns_name(payload, offset)
        offset += 4
    answers: list[str] = []
    for _ in range(ancount):
        _, offset = _decode_dns_name(payload, offset)
        if offset + 10 > len(payload):
            break
        rtype, rclass, _, rdlength = struct.unpack("!HHIH", payload[offset : offset + 10])
        offset += 10
        rdata = payload[offset : offset + rdlength]
        offset += rdlength
        if rtype == 1 and rclass == 1 and rdlength == 4:
            answers.append(socket.inet_ntoa(rdata))
    return answers


def _resolve_direct_ips(config: dict[str, Any], bind_ip: str | None) -> dict[str, Any]:
    resolved: list[str] = []
    resolution: dict[str, Any] = {"hosts": {}, "dns_servers": list(config.get("dns_servers") or []), "fallback_used": False}
    dns_servers = [str(item).strip() for item in config.get("dns_servers") or [] if str(item).strip()]
    for host in _DIRECT_HOSTNAMES:
        host_answers: list[str] = []
        errors: list[str] = []
        for server in dns_servers:
            try:
                answers = _dns_query_a(
                    host,
                    server,
                    bind_ip=bind_ip,
                    timeout=float(config.get("dns_timeout_seconds") or 3.0),
                )
            except Exception as exc:
                errors.append(f"{server}:{exc!r}")
                continue
            if answers:
                host_answers.extend(answers)
        deduped = []
        seen = set()
        for answer in host_answers:
            if answer.startswith("198.18."):
                continue
            if answer not in seen:
                seen.add(answer)
                deduped.append(answer)
        resolution["hosts"][host] = {"ips": deduped, "errors": errors}
        resolved.extend(deduped)
    deduped_resolved = []
    seen = set()
    for ip in resolved:
        if ip not in seen:
            seen.add(ip)
            deduped_resolved.append(ip)
    if not deduped_resolved:
        resolution["fallback_used"] = True
        deduped_resolved = [
            str(ip).strip()
            for ip in config.get("direct_ip_candidates") or []
            if str(ip).strip() and not str(ip).startswith("198.18.")
        ]
    resolution["resolved_ips"] = deduped_resolved
    return resolution


def _prepare_direct_env(extra_hosts: list[str]) -> dict[str, Any]:
    removed = [key for key in _PROXY_KEYS if os.environ.get(key)]
    for key in _PROXY_KEYS:
        os.environ.pop(key, None)
    no_proxy_values: list[str] = []
    for key in _NO_PROXY_KEYS:
        value = os.environ.get(key)
        if value:
            no_proxy_values.extend([item.strip() for item in value.split(",") if item.strip()])
    for host in extra_hosts:
        if host not in no_proxy_values:
            no_proxy_values.append(host)
    deduped: list[str] = []
    seen = set()
    for item in no_proxy_values:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    merged = ",".join(deduped)
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged
    return {"proxy_env_removed": removed, "no_proxy": merged}


def _probe_tcp(ip: str, port: int, *, timeout: float, bind_ip: str | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "ip": ip,
        "port": int(port),
        "ok": False,
        "local_address": None,
        "error": None,
    }
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if bind_ip:
            sock.bind((bind_ip, 0))
        sock.connect((ip, int(port)))
        report["local_address"] = sock.getsockname()[0]
        report["ok"] = True
    except Exception as exc:
        report["error"] = repr(exc)
    finally:
        sock.close()
    return report


def _probe_tcp_with_retries(
    ip: str,
    port: int,
    *,
    timeout: float,
    bind_ip: str | None = None,
    attempts: int = 3,
) -> dict[str, Any]:
    attempt_reports: list[dict[str, Any]] = []
    max_attempts = max(1, int(attempts))
    for attempt in range(1, max_attempts + 1):
        probe = _probe_tcp(ip, port, timeout=timeout, bind_ip=bind_ip)
        probe["attempt"] = attempt
        attempt_reports.append(probe)
        if probe.get("ok"):
            return {
                **probe,
                "attempts": attempt_reports,
                "attempt_count": attempt,
                "recovered_after_retry": attempt > 1,
            }
    final_probe = dict(attempt_reports[-1])
    final_probe["attempts"] = attempt_reports
    final_probe["attempt_count"] = len(attempt_reports)
    final_probe["recovered_after_retry"] = False
    return final_probe


class _SourceAddressHTTPAdapter(HTTPAdapter):
    def __init__(self, *, source_address: tuple[str, int] | None = None, **kwargs: Any) -> None:
        self._source_address = source_address
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        if self._source_address:
            pool_kwargs["source_address"] = self._source_address
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        if self._source_address:
            proxy_kwargs["source_address"] = self._source_address
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def _build_direct_session(bind_ip: str | None = None) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    adapter = _SourceAddressHTTPAdapter(source_address=(bind_ip, 0) if bind_ip else None)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"Connection": "keep-alive"})
    return session


def _response_to_frame(payload: dict[str, Any], *, api_name: str) -> pd.DataFrame:
    code = payload.get("code")
    if code not in (0, "0", None):
        message = str(payload.get("msg") or payload.get("message") or "")
        raise RuntimeError(f"tushare_api_error:{api_name}:{message}")
    data = payload.get("data") or {}
    fields = list(data.get("fields") or [])
    items = list(data.get("items") or [])
    if not fields:
        return pd.DataFrame()
    return pd.DataFrame(items, columns=fields)


def _probe_trade_cal(
    *,
    session: requests.Session,
    base_url: str,
    host_header: str,
    token: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    url = f"{base_url}/dataapi/trade_cal"
    response = session.post(
        url,
        headers={"Host": host_header, "Content-Type": "application/json"},
        json={
            "token": token,
            "params": {"exchange": "SSE", "start_date": "20260525", "end_date": "20260605"},
            "fields": "exchange,cal_date,is_open,pretrade_date",
        },
        timeout=(timeout_seconds, timeout_seconds),
    )
    response.raise_for_status()
    payload = response.json()
    frame = _response_to_frame(payload, api_name="trade_cal")
    return {"url": url, "rows": int(len(frame)), "columns": list(frame.columns)}


def tushare_network_preflight(
    config: dict[str, Any] | None = None,
    *,
    verify_http: bool = True,
    token: str | None = None,
    repair_routes: bool = False,
) -> dict[str, Any]:
    config = get_tushare_config(config)
    resolved_token = (token or str(config.get("token") or "").strip() or get_tushare_token()).strip()
    route = _pick_non_tun_default_route()
    report: dict[str, Any] = {
        "status": "unknown",
        "selected_route": None,
        "proxy_env_removed": [],
        "no_proxy": "",
        "dns_resolution": {},
        "resolved_ips": [],
        "all_resolved_ips": [],
        "reachable_ips": [],
        "route_pin": {},
        "host_route_gate": {},
        "routes": {},
        "socket_probes": {},
        "http_probes": [],
        "http_probe": None,
        "issues": [],
        "warnings": [],
    }
    if not route:
        report["status"] = "failed"
        report["issues"].append("non_tun_default_route_missing")
        return report
    gateway, iface, source = route
    report["selected_route"] = {"gateway": gateway, "iface": iface, "source": source}
    env_report = _prepare_direct_env(_DIRECT_HOSTNAMES)
    report.update(env_report)
    resolution = _resolve_direct_ips(config, source)
    report["dns_resolution"] = resolution
    ips = list(resolution.get("resolved_ips") or [])
    report["all_resolved_ips"] = list(ips)
    report["resolved_ips"] = list(ips)
    if not ips:
        report["status"] = "failed"
        report["issues"].append("tushare_real_ip_missing")
        return report
    extra_hosts = _DIRECT_HOSTNAMES + ips
    env_report = _prepare_direct_env(extra_hosts)
    report.update(env_report)
    route_pin = _ensure_direct_host_routes(ips, execute=True) if repair_routes else _ensure_direct_host_routes(ips)
    report["route_pin"] = route_pin
    report["route_mode"] = "repair" if repair_routes else "check_only"
    report["issues"].extend(route_pin.get("issues", []))
    host_route_gate = _windows_host_route_preflight(ips)
    report["host_route_gate"] = host_route_gate
    if host_route_gate.get("status") == "failed":
        report["issues"].extend(host_route_gate.get("issues") or ["host_tushare_route_gate_failed"])
    elif host_route_gate.get("status") == "skipped" and host_route_gate.get("reason") != "not_wsl":
        report["warnings"].append(f"host_tushare_route_gate_skipped:{host_route_gate.get('reason')}")
    probe_timeout = min(float(config.get("api_timeout_seconds") or 20.0), 4.0)
    reachable_ips: list[str] = []
    for ip in ips:
        current = _route_get(ip)
        report["routes"][ip] = current
        if _route_uses_proxy_tun(current):
            report["warnings"].append(f"tushare_route_uses_proxy_tun:{ip}")
            continue
        probe = _probe_tcp_with_retries(
            ip,
            int(config.get("api_port") or 80),
            timeout=probe_timeout,
            bind_ip=source,
        )
        report["socket_probes"][ip] = probe
        local_address = str(probe.get("local_address") or "")
        if not probe.get("ok"):
            report["warnings"].append(f"tushare_socket_connect_failed:{ip}:{probe.get('error')}")
        elif local_address.startswith("198.18."):
            report["warnings"].append(f"tushare_socket_uses_proxy_tun:{ip}:{local_address}")
        else:
            if probe.get("recovered_after_retry"):
                report["warnings"].append(f"tushare_socket_connect_recovered:{ip}:attempt={probe.get('attempt_count')}")
            reachable_ips.append(ip)
    report["reachable_ips"] = list(reachable_ips)
    if not reachable_ips:
        report["status"] = "failed"
        report["issues"].append("tushare_no_reachable_direct_ip")
        return report
    report["resolved_ips"] = list(reachable_ips)
    if verify_http:
        last_http_exc: Exception | None = None
        for attempt, ip in enumerate(reachable_ips, start=1):
            session = _build_direct_session(bind_ip=source)
            base_url = f"{config.get('api_scheme', 'http')}://{ip}"
            try:
                http_probe = _probe_trade_cal(
                    session=session,
                    base_url=base_url,
                    host_header=str(config.get("api_host") or "api.waditu.com"),
                    token=resolved_token,
                    timeout_seconds=float(config.get("api_timeout_seconds") or 20.0),
                )
                http_probe["ip"] = ip
                http_probe["attempt"] = attempt
                report["http_probe"] = http_probe
                report["http_probes"].append(http_probe)
                if attempt > 1:
                    report["warnings"].append(f"tushare_http_probe_recovered:{ip}:attempt={attempt}")
                break
            except Exception as exc:
                last_http_exc = exc
                report["http_probes"].append({"ip": ip, "attempt": attempt, "error": repr(exc)})
                continue
            finally:
                try:
                    session.close()
                except Exception:
                    pass
        if report["http_probe"] is None and last_http_exc is not None:
            report["issues"].append(f"tushare_http_probe_failed:{last_http_exc}")
    report["status"] = "ok" if not report["issues"] else "failed"
    return report


class DirectTushareClient:
    def __init__(
        self,
        *,
        token: str,
        api_host: str,
        base_urls: list[str],
        timeout_seconds: float,
        bind_ip: str | None = None,
        network_report: dict[str, Any] | None = None,
    ) -> None:
        self.token = token
        self.api_host = api_host
        self.base_urls = base_urls
        self.timeout_seconds = float(timeout_seconds)
        self.bind_ip = bind_ip
        self.network_report = network_report or {}
        self.session = _build_direct_session(bind_ip=bind_ip)

    def reset_session(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
        self.session = _build_direct_session(bind_ip=self.bind_ip)

    def query(self, api_name: str, fields: str = "", **params: Any) -> pd.DataFrame:
        last_exc: Exception | None = None
        connect_timeout = min(5.0, self.timeout_seconds)
        for base_url in self.base_urls:
            for _ in range(2):
                try:
                    response = self.session.post(
                        f"{base_url}/dataapi/{api_name}",
                        headers={"Host": self.api_host, "Content-Type": "application/json"},
                        json={
                            "token": self.token,
                            "params": params,
                            "fields": fields,
                        },
                        timeout=(connect_timeout, self.timeout_seconds),
                    )
                    response.raise_for_status()
                    return _response_to_frame(response.json(), api_name=api_name)
                except Exception as exc:
                    last_exc = exc
                    self.reset_session()
        raise RuntimeError(f"tushare_direct_request_failed:{api_name}:{last_exc}") from last_exc

    def __getattr__(self, api_name: str):
        def _query(**kwargs: Any) -> pd.DataFrame:
            fields = kwargs.pop("fields", "")
            return self.query(api_name, fields=fields, **kwargs)

        return _query


class TushareModuleProxy:
    def __init__(self, ts_module, pro_client) -> None:
        self._ts = ts_module
        self._pro = pro_client

    def pro_bar(self, *args: Any, **kwargs: Any):
        kwargs.setdefault("api", self._pro)
        return self._ts.pro_bar(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._ts, name)


def get_tushare_client(
    token: str | None = None,
    *,
    network_mode: str = "direct",
    network_report: dict[str, Any] | None = None,
):
    import tushare as ts

    resolved = (token or get_tushare_token()).strip()
    ts.set_token(resolved)
    if str(network_mode or "inherit").strip().lower() != "direct":
        return ts.pro_api(resolved)
    config = get_tushare_config()
    report = network_report or tushare_network_preflight(config=config, verify_http=True, token=resolved)
    if report.get("status") != "ok":
        raise RuntimeError(f"tushare_network_not_direct:{report}")
    bind_ip = ((report.get("selected_route") or {}).get("source")) or None
    resolved_ips = [str(ip).strip() for ip in report.get("resolved_ips") or [] if str(ip).strip()]
    preferred_ip = str(((report.get("http_probe") or {}).get("ip")) or "").strip()
    if preferred_ip and preferred_ip in resolved_ips:
        resolved_ips = [preferred_ip] + [ip for ip in resolved_ips if ip != preferred_ip]
    base_urls = [f"{config.get('api_scheme', 'http')}://{ip}" for ip in resolved_ips]
    return DirectTushareClient(
        token=resolved,
        api_host=str(config.get("api_host") or "api.waditu.com"),
        base_urls=base_urls,
        timeout_seconds=float(config.get("api_timeout_seconds") or 20.0),
        bind_ip=bind_ip,
        network_report=report,
    )


def get_tushare_module(token: str | None = None, *, network_mode: str = "direct", client=None):
    import tushare as ts

    resolved = (token or get_tushare_token()).strip()
    ts.set_token(resolved)
    pro_client = client if client is not None else get_tushare_client(resolved, network_mode=network_mode)
    return TushareModuleProxy(ts, pro_client)
