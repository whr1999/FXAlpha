# FXAlpha Tushare Direct Network Policy

Updated: 2026-08-01

## Rule

Tushare production data updates must use domestic direct routing.

Allowed:

- FlClash can remain running for Codex/browser internet access.
- Windows host `/32` routes may pin Tushare IPs to the real WLAN gateway.
- WSL direct probes may use fixed real Tushare IP candidates with the correct `Host: api.waditu.com` header.

Not allowed:

- proxy fallback
- TUN/TAP/Wintun route for Tushare API traffic
- any retired-provider fallback
- market-data-only fallback
- stopping or rewriting the user's FlClash configuration from FXAlpha code

## Host Route Guard

Windows guard script:

```text
%USERPROFILE%\Documents\New project\scripts\fxalpha_tushare_direct_route_guard.ps1
```

WSL repository copy:

```text
./scripts/tushare_direct_route_guard.ps1
```

Check mode:

```powershell
powershell.exe -ExecutionPolicy Bypass -File "%USERPROFILE%\Documents\New project\scripts\fxalpha_tushare_direct_route_guard.ps1" -Mode check -Json
```

Ensure mode requires an elevated Windows shell and adds persistent `/32` host routes only for the configured Tushare IPs:

```powershell
powershell.exe -ExecutionPolicy Bypass -File "%USERPROFILE%\Documents\New project\scripts\fxalpha_tushare_direct_route_guard.ps1" -Mode ensure -Json
```

## Production Gate

`data-daily-preflight` must block if:

- `host_route_gate` reports `host_tushare_route_uses_proxy_tun:*`
- direct DNS/IP probe resolves only to `198.18.*`
- selected route/source address is a proxy/TUN path
- HTTP probe to Tushare direct endpoint fails

When blocked, do not start `data-stage-update` or `data-daily-routine`.

## Headless systemd execution

The user service must not rely on the interactive WSL `PATH` to discover
Windows PowerShell. The installed and repository service units set:

```ini
Environment=FXALPHA_POWERSHELL_EXE=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
```

The Tushare client resolves the executable in this order: explicit
`FXALPHA_POWERSHELL_EXE`, `powershell.exe`/`pwsh.exe` on `PATH`, then the fixed
WSL Windows PowerShell path. Failure to resolve or run the host route guard is a
hard preflight blocker; it is not permission to skip the route check or use a
proxy.
