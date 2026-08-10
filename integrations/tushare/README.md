# FXAlpha Tushare integration

This directory holds FXAlpha-owned orchestration helpers around the Tushare
SDK. The third-party package itself is pinned as `third_party/tushare`.

Current goals:

- provide a stable Python SDK entrypoint for future data-foundation rebuilds
- keep token and initialization logic in one place
- support quick health checks before we build full downloaders

## Files

- `client.py`: shared SDK initialization helpers, direct-network preflight, and direct-IP client
- `healthcheck_tushare.py`: small connectivity and permissions probe

## Data foundation contract

The current Tushare field dictionary and fetch contract is maintained at:

[`docs/DATA_FOUNDATION_TUSHARE_FIELD_DICTIONARY_CURRENT.md`](../../docs/DATA_FOUNDATION_TUSHARE_FIELD_DICTIONARY_CURRENT.md)

Use that document as the implementation contract for full rebuild and daily update downloaders.

Current rebuild runbook:

[`docs/DATA_FOUNDATION_TUSHARE_REBUILD_RUNBOOK_CURRENT.md`](../../docs/DATA_FOUNDATION_TUSHARE_REBUILD_RUNBOOK_CURRENT.md)

## Token source

The token is read from:

1. `config.yaml:data_foundation.tushare.token`
2. `TUSHARE_TOKEN` environment variable

The config value is preferred for FXAlpha local operations.

## Standard SDK usage

According to the Tushare Python SDK documentation, the normal initialization flow is:

```python
import tushare as ts
ts.set_token("your token")
pro = ts.pro_api()
```

FXAlpha wraps this in `client.py` so callers do not need to duplicate token handling.

## Direct-network note

On the current FXAlpha workstation, WSL commonly inherits local proxy variables and fake-IP DNS answers from the Windows-side TUN setup.
Because of that, FXAlpha Tushare rebuilds default to `direct` mode.

Current `direct` mode does all of the following before download:

- clears process proxy environment variables
- resolves real Tushare IPs through public DNS bound to the non-TUN LAN source
- pins host routes for the resolved IPs to the non-TUN interface
- verifies socket local source address is not `198.18.*`
- sends API requests to `http://<real_ip>/dataapi/...` with `Host: api.waditu.com`

Operational probe:

```bash
PYTHONPATH=. .venv/bin/python cli.py data-tushare-network
```
