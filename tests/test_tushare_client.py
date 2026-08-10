from __future__ import annotations

import sys
import types
from pathlib import Path

import pandas as pd
import pytest

from integrations.tushare import client


@pytest.fixture(autouse=True)
def _default_windows_host_route_gate(monkeypatch):
    monkeypatch.setattr(
        client,
        "_windows_host_route_preflight",
        lambda hosts: {
            "status": "ok",
            "issues": [],
            "warnings": [],
            "hosts": {host: {"route_is_direct": True, "route_uses_proxy_tun": False} for host in hosts},
        },
    )


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers or {},
                "json": json or {},
                "timeout": timeout,
            }
        )
        return _FakeResponse(
            {
                "code": 0,
                "data": {
                    "fields": ["cal_date", "is_open"],
                    "items": [["20260602", "1"]],
                },
            }
        )


class _ResettingSession:
    def __init__(self, *, fail_first: bool):
        self.fail_first = fail_first
        self.calls = 0
        self.closed = False

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise ConnectionResetError(104, "Connection reset by peer")
        return _FakeResponse(
            {
                "code": 0,
                "data": {
                    "fields": ["cal_date", "is_open"],
                    "items": [["20260602", "1"]],
                },
            }
        )

    def close(self):
        self.closed = True


def test_windows_powershell_executable_falls_back_for_systemd_path(monkeypatch, tmp_path):
    powershell = tmp_path / "powershell.exe"
    powershell.touch()
    monkeypatch.delenv("FXALPHA_POWERSHELL_EXE", raising=False)
    monkeypatch.setattr(client.shutil, "which", lambda name: None)
    monkeypatch.setattr(client, "_WINDOWS_POWERSHELL_CANDIDATES", (Path(powershell),))

    assert client._windows_powershell_executable() == str(powershell)


def test_direct_tushare_client_query_uses_ip_url_and_host_header():
    session = _FakeSession()
    pro = client.DirectTushareClient(
        token="token",
        api_host="api.waditu.com",
        base_urls=["http://8.140.225.26"],
        timeout_seconds=12.0,
        bind_ip="192.168.1.8",
    )
    pro.session = session

    frame = pro.trade_cal(
        exchange="SSE",
        start_date="20260525",
        end_date="20260605",
        fields="cal_date,is_open",
    )

    assert isinstance(frame, pd.DataFrame)
    assert list(frame.columns) == ["cal_date", "is_open"]
    assert session.calls[0]["url"] == "http://8.140.225.26/dataapi/trade_cal"
    assert session.calls[0]["headers"]["Host"] == "api.waditu.com"
    assert session.calls[0]["json"]["token"] == "token"
    assert session.calls[0]["json"]["params"]["exchange"] == "SSE"
    assert session.calls[0]["timeout"] == (5.0, 12.0)


def test_tushare_network_preflight_fails_when_socket_uses_tun(monkeypatch):
    monkeypatch.setattr(client, "get_tushare_config", lambda overrides=None: {
        "token": "token",
        "api_host": "api.waditu.com",
        "api_port": 80,
        "api_scheme": "http",
        "api_timeout_seconds": 5.0,
        "dns_timeout_seconds": 1.0,
        "dns_servers": ["223.5.5.5"],
        "direct_ip_candidates": ["8.140.225.26"],
    })
    monkeypatch.setattr(client, "_pick_non_tun_default_route", lambda: ("192.168.1.1", "eth2", "192.168.1.8"))
    monkeypatch.setattr(client, "_prepare_direct_env", lambda extra_hosts: {"proxy_env_removed": ["HTTP_PROXY"], "no_proxy": ",".join(extra_hosts)})
    monkeypatch.setattr(client, "_resolve_direct_ips", lambda config, bind_ip: {"resolved_ips": ["8.140.225.26"], "hosts": {}, "dns_servers": [], "fallback_used": False})
    monkeypatch.setattr(client, "_ensure_direct_host_routes", lambda hosts: {"status": "ok", "issues": [], "hosts": {}, "selected_route": {"gateway": "192.168.1.1", "iface": "eth2", "source": "192.168.1.8"}})
    monkeypatch.setattr(client, "_route_get", lambda host: f"{host} via 192.168.1.1 dev eth2 src 192.168.1.8")
    monkeypatch.setattr(client, "_probe_tcp", lambda ip, port, timeout, bind_ip=None: {"ip": ip, "port": port, "ok": True, "local_address": "198.18.0.1", "error": None})

    report = client.tushare_network_preflight(verify_http=False, token="token")

    assert report["status"] == "failed"
    assert "tushare_no_reachable_direct_ip" in report["issues"]
    assert any("tushare_socket_uses_proxy_tun" in warning for warning in report["warnings"])


def test_tushare_network_preflight_reports_http_probe(monkeypatch):
    monkeypatch.setattr(client, "get_tushare_config", lambda overrides=None: {
        "token": "token",
        "api_host": "api.waditu.com",
        "api_port": 80,
        "api_scheme": "http",
        "api_timeout_seconds": 5.0,
        "dns_timeout_seconds": 1.0,
        "dns_servers": ["223.5.5.5"],
        "direct_ip_candidates": ["8.140.225.26"],
    })
    monkeypatch.setattr(client, "_pick_non_tun_default_route", lambda: ("192.168.1.1", "eth2", "192.168.1.8"))
    monkeypatch.setattr(client, "_prepare_direct_env", lambda extra_hosts: {"proxy_env_removed": ["HTTP_PROXY"], "no_proxy": ",".join(extra_hosts)})
    monkeypatch.setattr(client, "_resolve_direct_ips", lambda config, bind_ip: {"resolved_ips": ["8.140.225.26"], "hosts": {}, "dns_servers": [], "fallback_used": False})
    monkeypatch.setattr(client, "_ensure_direct_host_routes", lambda hosts: {"status": "ok", "issues": [], "hosts": {}, "selected_route": {"gateway": "192.168.1.1", "iface": "eth2", "source": "192.168.1.8"}})
    monkeypatch.setattr(client, "_route_get", lambda host: f"{host} via 192.168.1.1 dev eth2 src 192.168.1.8")
    monkeypatch.setattr(client, "_probe_tcp", lambda ip, port, timeout, bind_ip=None: {"ip": ip, "port": port, "ok": True, "local_address": "192.168.1.8", "error": None})
    monkeypatch.setattr(client, "_build_direct_session", lambda bind_ip=None: object())
    monkeypatch.setattr(
        client,
        "_probe_trade_cal",
        lambda session, base_url, host_header, token, timeout_seconds: {
            "url": f"{base_url}/dataapi/trade_cal",
            "rows": 5,
            "columns": ["exchange", "cal_date"],
        },
    )

    report = client.tushare_network_preflight(verify_http=True, token="token")

    assert report["status"] == "ok"
    assert report["http_probe"]["rows"] == 5
    assert report["resolved_ips"] == ["8.140.225.26"]
    assert report["host_route_gate"]["status"] == "ok"


def test_tushare_network_preflight_blocks_when_windows_host_route_uses_tun(monkeypatch):
    monkeypatch.setattr(client, "get_tushare_config", lambda overrides=None: {
        "token": "token",
        "api_host": "api.waditu.com",
        "api_port": 80,
        "api_scheme": "http",
        "api_timeout_seconds": 5.0,
        "dns_timeout_seconds": 1.0,
        "dns_servers": ["223.5.5.5"],
        "direct_ip_candidates": ["8.140.225.26"],
    })
    monkeypatch.setattr(client, "_pick_non_tun_default_route", lambda: ("172.19.192.1", "eth0", "172.19.193.100"))
    monkeypatch.setattr(client, "_prepare_direct_env", lambda extra_hosts: {"proxy_env_removed": [], "no_proxy": ",".join(extra_hosts)})
    monkeypatch.setattr(client, "_resolve_direct_ips", lambda config, bind_ip: {"resolved_ips": ["8.140.225.26"], "hosts": {}, "dns_servers": [], "fallback_used": False})
    monkeypatch.setattr(client, "_ensure_direct_host_routes", lambda hosts: {"status": "ok", "issues": [], "hosts": {}})
    monkeypatch.setattr(
        client,
        "_windows_host_route_preflight",
        lambda hosts: {
            "status": "failed",
            "issues": ["host_tushare_route_uses_proxy_tun:8.140.225.26"],
            "warnings": [],
            "hosts": {
                "8.140.225.26": {
                    "route_is_direct": False,
                    "route_uses_proxy_tun": True,
                    "interface_alias": "FlClash",
                    "next_hop": "198.18.0.2",
                }
            },
        },
    )
    monkeypatch.setattr(client, "_route_get", lambda host: f"{host} via 172.19.192.1 dev eth0 src 172.19.193.100")
    monkeypatch.setattr(client, "_probe_tcp", lambda ip, port, timeout, bind_ip=None: {"ip": ip, "port": port, "ok": True, "local_address": bind_ip, "error": None})

    report = client.tushare_network_preflight(verify_http=False, token="token")

    assert report["status"] == "failed"
    assert "host_tushare_route_uses_proxy_tun:8.140.225.26" in report["issues"]
    assert report["host_route_gate"]["hosts"]["8.140.225.26"]["interface_alias"] == "FlClash"


def test_pick_non_tun_default_route_accepts_wsl_nat_eth0(monkeypatch):
    class Completed:
        returncode = 0
        stdout = "default via 172.19.192.1 dev eth0 proto kernel\n"
        stderr = ""

    monkeypatch.setattr(client.subprocess, "run", lambda *args, **kwargs: Completed())
    monkeypatch.setattr(client, "_iface_ipv4_address", lambda iface: "172.19.193.100")

    route = client._pick_non_tun_default_route()

    assert route == ("172.19.192.1", "eth0", "172.19.193.100")


def test_tushare_network_preflight_accepts_wsl_nat_direct_route(monkeypatch):
    monkeypatch.setattr(client, "get_tushare_config", lambda overrides=None: {
        "token": "token",
        "api_host": "api.waditu.com",
        "api_port": 80,
        "api_scheme": "http",
        "api_timeout_seconds": 5.0,
        "dns_timeout_seconds": 1.0,
        "dns_servers": ["223.5.5.5"],
        "direct_ip_candidates": ["8.140.225.26"],
    })
    monkeypatch.setattr(client, "_pick_non_tun_default_route", lambda: ("172.19.192.1", "eth0", "172.19.193.100"))
    monkeypatch.setattr(client, "_prepare_direct_env", lambda extra_hosts: {"proxy_env_removed": ["HTTP_PROXY"], "no_proxy": ",".join(extra_hosts)})
    monkeypatch.setattr(client, "_resolve_direct_ips", lambda config, bind_ip: {"resolved_ips": ["8.140.225.26"], "hosts": {}, "dns_servers": [], "fallback_used": False})
    monkeypatch.setattr(client, "_ensure_direct_host_routes", lambda hosts: {"status": "ok", "issues": [], "hosts": {}, "selected_route": {"gateway": "172.19.192.1", "iface": "eth0", "source": "172.19.193.100"}})
    monkeypatch.setattr(client, "_route_get", lambda host: f"{host} via 172.19.192.1 dev eth0 src 172.19.193.100")
    monkeypatch.setattr(client, "_probe_tcp", lambda ip, port, timeout, bind_ip=None: {"ip": ip, "port": port, "ok": True, "local_address": "172.19.193.100", "error": None})
    monkeypatch.setattr(client, "_build_direct_session", lambda bind_ip=None: object())
    monkeypatch.setattr(
        client,
        "_probe_trade_cal",
        lambda session, base_url, host_header, token, timeout_seconds: {
            "url": f"{base_url}/dataapi/trade_cal",
            "rows": 5,
            "columns": ["exchange", "cal_date"],
        },
    )

    report = client.tushare_network_preflight(verify_http=True, token="token")

    assert report["status"] == "ok"
    assert report["selected_route"]["iface"] == "eth0"
    assert report["http_probe"]["rows"] == 5


def test_tushare_network_preflight_accepts_partial_ip_reachability(monkeypatch):
    monkeypatch.setattr(client, "get_tushare_config", lambda overrides=None: {
        "token": "token",
        "api_host": "api.waditu.com",
        "api_port": 80,
        "api_scheme": "http",
        "api_timeout_seconds": 5.0,
        "dns_timeout_seconds": 1.0,
        "dns_servers": ["223.5.5.5"],
        "direct_ip_candidates": ["8.140.225.26", "60.205.198.20"],
    })
    monkeypatch.setattr(client, "_pick_non_tun_default_route", lambda: ("172.19.192.1", "eth0", "172.19.193.100"))
    monkeypatch.setattr(client, "_prepare_direct_env", lambda extra_hosts: {"proxy_env_removed": ["HTTP_PROXY"], "no_proxy": ",".join(extra_hosts)})
    monkeypatch.setattr(client, "_resolve_direct_ips", lambda config, bind_ip: {"resolved_ips": ["8.140.225.26", "60.205.198.20"], "hosts": {}, "dns_servers": [], "fallback_used": False})
    monkeypatch.setattr(client, "_ensure_direct_host_routes", lambda hosts: {"status": "ok", "issues": [], "hosts": {}, "selected_route": {"gateway": "172.19.192.1", "iface": "eth0", "source": "172.19.193.100"}})
    monkeypatch.setattr(client, "_route_get", lambda host: f"{host} via 172.19.192.1 dev eth0 src 172.19.193.100")

    def fake_probe(ip, port, timeout, bind_ip=None):
        if ip == "8.140.225.26":
            return {"ip": ip, "port": port, "ok": True, "local_address": "172.19.193.100", "error": None}
        return {"ip": ip, "port": port, "ok": False, "local_address": None, "error": "timeout"}

    monkeypatch.setattr(client, "_probe_tcp", fake_probe)
    monkeypatch.setattr(client, "_build_direct_session", lambda bind_ip=None: object())
    monkeypatch.setattr(
        client,
        "_probe_trade_cal",
        lambda session, base_url, host_header, token, timeout_seconds: {
            "url": f"{base_url}/dataapi/trade_cal",
            "rows": 5,
            "columns": ["exchange", "cal_date"],
        },
    )

    report = client.tushare_network_preflight(verify_http=True, token="token")

    assert report["status"] == "ok"
    assert report["resolved_ips"] == ["8.140.225.26"]
    assert report["reachable_ips"] == ["8.140.225.26"]
    assert any("tushare_socket_connect_failed:60.205.198.20" in warning for warning in report["warnings"])


def test_tushare_network_preflight_retries_transient_socket_failure(monkeypatch):
    monkeypatch.setattr(client, "get_tushare_config", lambda overrides=None: {
        "token": "token",
        "api_host": "api.waditu.com",
        "api_port": 80,
        "api_scheme": "http",
        "api_timeout_seconds": 5.0,
        "dns_timeout_seconds": 1.0,
        "dns_servers": ["223.5.5.5"],
        "direct_ip_candidates": ["8.140.225.26"],
    })
    monkeypatch.setattr(client, "_pick_non_tun_default_route", lambda: ("172.19.192.1", "eth0", "172.19.193.100"))
    monkeypatch.setattr(client, "_prepare_direct_env", lambda extra_hosts: {"proxy_env_removed": ["HTTP_PROXY"], "no_proxy": ",".join(extra_hosts)})
    monkeypatch.setattr(client, "_resolve_direct_ips", lambda config, bind_ip: {"resolved_ips": ["8.140.225.26"], "hosts": {}, "dns_servers": [], "fallback_used": False})
    monkeypatch.setattr(client, "_ensure_direct_host_routes", lambda hosts: {"status": "ok", "issues": [], "hosts": {}, "selected_route": {"gateway": "172.19.192.1", "iface": "eth0", "source": "172.19.193.100"}})
    monkeypatch.setattr(client, "_route_get", lambda host: f"{host} via 172.19.192.1 dev eth0 src 172.19.193.100")
    attempts = {"count": 0}

    def fake_probe(ip, port, timeout, bind_ip=None):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return {"ip": ip, "port": port, "ok": False, "local_address": None, "error": "TimeoutError('timed out')"}
        assert bind_ip == "172.19.193.100"
        return {"ip": ip, "port": port, "ok": True, "local_address": bind_ip, "error": None}

    monkeypatch.setattr(client, "_probe_tcp", fake_probe)
    monkeypatch.setattr(client, "_build_direct_session", lambda bind_ip=None: object())
    monkeypatch.setattr(
        client,
        "_probe_trade_cal",
        lambda session, base_url, host_header, token, timeout_seconds: {
            "url": f"{base_url}/dataapi/trade_cal",
            "rows": 5,
            "columns": ["exchange", "cal_date"],
        },
    )

    report = client.tushare_network_preflight(verify_http=True, token="token")

    assert report["status"] == "ok"
    assert report["reachable_ips"] == ["8.140.225.26"]
    assert report["socket_probes"]["8.140.225.26"]["attempt_count"] == 2
    assert report["socket_probes"]["8.140.225.26"]["recovered_after_retry"] is True
    assert any("tushare_socket_connect_recovered:8.140.225.26:attempt=2" == warning for warning in report["warnings"])


def test_tushare_network_preflight_fails_over_http_probe_to_next_ip(monkeypatch):
    monkeypatch.setattr(client, "get_tushare_config", lambda overrides=None: {
        "token": "token",
        "api_host": "api.waditu.com",
        "api_port": 80,
        "api_scheme": "http",
        "api_timeout_seconds": 5.0,
        "dns_timeout_seconds": 1.0,
        "dns_servers": ["223.5.5.5"],
        "direct_ip_candidates": ["8.140.225.26", "60.205.198.20"],
    })
    monkeypatch.setattr(client, "_pick_non_tun_default_route", lambda: ("172.19.192.1", "eth0", "172.19.193.100"))
    monkeypatch.setattr(client, "_prepare_direct_env", lambda extra_hosts: {"proxy_env_removed": ["HTTP_PROXY"], "no_proxy": ",".join(extra_hosts)})
    monkeypatch.setattr(client, "_resolve_direct_ips", lambda config, bind_ip: {"resolved_ips": ["8.140.225.26", "60.205.198.20"], "hosts": {}, "dns_servers": [], "fallback_used": False})
    monkeypatch.setattr(client, "_ensure_direct_host_routes", lambda hosts: {"status": "ok", "issues": [], "hosts": {}, "selected_route": {"gateway": "172.19.192.1", "iface": "eth0", "source": "172.19.193.100"}})
    monkeypatch.setattr(client, "_route_get", lambda host: f"{host} via 172.19.192.1 dev eth0 src 172.19.193.100")
    monkeypatch.setattr(client, "_probe_tcp", lambda ip, port, timeout, bind_ip=None: {"ip": ip, "port": port, "ok": True, "local_address": bind_ip, "error": None})
    monkeypatch.setattr(client, "_build_direct_session", lambda bind_ip=None: object())

    def fake_http_probe(session, base_url, host_header, token, timeout_seconds):
        if base_url == "http://8.140.225.26":
            raise TimeoutError("first ip timed out")
        return {"url": f"{base_url}/dataapi/trade_cal", "rows": 5, "columns": ["exchange", "cal_date"]}

    monkeypatch.setattr(client, "_probe_trade_cal", fake_http_probe)

    report = client.tushare_network_preflight(verify_http=True, token="token")

    assert report["status"] == "ok"
    assert report["http_probe"]["ip"] == "60.205.198.20"
    assert report["http_probe"]["attempt"] == 2
    assert report["http_probes"][0]["ip"] == "8.140.225.26"
    assert report["http_probes"][1]["ip"] == "60.205.198.20"
    assert any("tushare_http_probe_recovered:60.205.198.20:attempt=2" == warning for warning in report["warnings"])


def test_direct_tushare_client_resets_session_after_exception(monkeypatch):
    sessions = [_ResettingSession(fail_first=True), _ResettingSession(fail_first=False)]

    monkeypatch.setattr(client, "_build_direct_session", lambda bind_ip=None: sessions.pop(0))

    pro = client.DirectTushareClient(
        token="token",
        api_host="api.waditu.com",
        base_urls=["http://8.140.225.26"],
        timeout_seconds=12.0,
        bind_ip="192.168.1.8",
    )

    frame = pro.trade_cal(
        exchange="SSE",
        start_date="20260525",
        end_date="20260605",
        fields="cal_date,is_open",
    )

    assert list(frame.columns) == ["cal_date", "is_open"]
    assert pro.session.closed is False


def test_get_tushare_client_prefers_http_probe_ip(monkeypatch):
    fake_tushare = types.SimpleNamespace(set_token=lambda token: None, pro_api=lambda token: object())
    monkeypatch.setitem(sys.modules, "tushare", fake_tushare)
    monkeypatch.setattr(client, "get_tushare_token", lambda: "token")
    monkeypatch.setattr(client, "get_tushare_config", lambda overrides=None: {
        "token": "token",
        "api_host": "api.waditu.com",
        "api_port": 80,
        "api_scheme": "http",
        "api_timeout_seconds": 20.0,
    })
    monkeypatch.setattr(
        client,
        "tushare_network_preflight",
        lambda config=None, verify_http=True, token=None: {
            "status": "ok",
            "selected_route": {"source": "172.19.193.100"},
            "resolved_ips": ["8.140.225.26", "60.205.198.20"],
            "http_probe": {"ip": "60.205.198.20"},
        },
    )

    pro = client.get_tushare_client("token", network_mode="direct")

    assert pro.base_urls == ["http://60.205.198.20", "http://8.140.225.26"]
