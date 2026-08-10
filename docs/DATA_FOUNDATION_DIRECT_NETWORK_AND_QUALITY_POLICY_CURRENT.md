# FXAlpha Data Foundation Direct Network And Quality Policy

Updated: 2026-08-01

This document defines the current direct-network and no-fallback policy for the
production daily data path.

## Network Gate

Production daily updates now use Tushare direct mode.

- `data-daily-preflight` calls the Tushare direct-network preflight.
- The process must avoid the mihomo/TUN proxy path and verify a domestic direct
  route before source refresh begins.
- The WSL default environment may still keep proxy variables for other tools,
  but the Tushare production process must prove a direct route for its own
  session before the refresh window starts.
- Headless systemd execution resolves Windows PowerShell from
  `FXALPHA_POWERSHELL_EXE`, `PATH`, then the fixed WSL Windows PowerShell path;
  an unavailable host guard is a hard blocker.

Any failure blocks staging with `tushare_network_not_direct` or the lower-level
route or HTTP probe issue captured in the preflight payload.

## No Fallback

Data incompleteness is a hard failure.

- `data-daily-routine` has no retired-provider fallback.
- `data-stage-update` does not honor `FXALPHA_MARKET_DATA_ONLY`.
- The removed AmazingData check and network commands are no longer exposed.
- Failed staging packages remain in `runtime/data_foundation/staging/` for
  diagnosis; production readers stay on the existing `data/` paths.

## Daily Refresh Policy

Daily production refresh is done by rolling rebuild and merge:

1. rebuild a short Tushare window from the current production latest date
2. prepare compatibility artifacts for that window
3. merge the refreshed window back into the current production full HDF
4. regenerate staged Qlib and QuantGPT outputs directly from the merged HDF
5. run compatibility quality before promote

There is no secondary source fallback inside this workflow.

## Benchmark And Compatibility Quality

The merged compatibility HDF must continue to satisfy the production bridge:

- required benchmarks remain:
  - `000300.SH`
  - `000905.SH`
  - `000852.SH`
- raw price and factor compatibility checks must pass
- `stk_limit` coverage must pass: non-index stock rows require official `up_limit/down_limit`, except explicitly marked `structural_no_limit` rows
- daily merge must preserve and, when needed, derive `limit_source_kind`
- downstream staged outputs must agree on latest date

Warnings for structural source sparsity such as `moneyflow`, `margin_detail`,
or `pe_ttm` do not automatically block promotion unless they violate the
production compatibility gate.

## Verification Commands

Non-destructive checks:

```bash
python3 cli.py data-tushare-network
python3 -m py_compile domain/data_foundation/tushare_daily.py domain/data_foundation/tushare_rebuild.py domain/data_foundation/tushare_production.py domain/data_foundation/quality_check.py services/data_foundation_service.py
python3 -m pytest -q tests/test_tushare_client.py tests/test_tushare_rebuild.py tests/test_tushare_daily.py tests/test_tushare_limit_backfill.py tests/test_qlib_incremental_missing_instrument.py
python3 cli.py data-status
python3 cli.py data-production-audit --full-scan
python3 cli.py data-daily-preflight --target-date auto
python3 cli.py data-stage-update --target-date auto --dry-run
```

Do not run real `data-daily-routine` or promote until the direct-network gate,
the source-window rebuild, the merged compatibility quality, and the staged
downstream conversions all pass.
