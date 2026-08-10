# FXAlpha Data Update And Trading Operation Contract

Updated: 2026-08-09

## Goal

Data-foundation updates and trading operations are separate production chains.

1. Data foundation owns Tushare production data staging, quality checks, downstream compatibility outputs, and promote.
2. Trading owns production-model predictions, account recommendations, pending orders, Qlib paper execution, account snapshots, and historical replay after production data is already healthy.

`daily-ops-routine` and the legacy `/trade/*` write family are retired. The only production trading writers are account-scoped Fleet/Replay operations guarded by the shared paper-operation lock.

## Heartbeat Split

| Task | Suggested time | Allowed entrypoints | Disallowed entrypoints |
| --- | --- | --- | --- |
| Data foundation daily update | Tuesday-Saturday 02:00 Asia/Shanghai | `data-status`, `data-tushare-network`, `data-daily-preflight`, `data-stage-update --dry-run`, `data-daily-routine`, `data-production-audit` | `pred-update`, `trade-*`, `daily-ops-routine`, `model-train`, `factor-research` |
| Trading non-data steps | After data is promoted and verified | `paper-fleet-status`, `paper-fleet-preflight`, `paper-fleet-run`, `paper-replay-*` | `data-daily-routine`, `data-stage-update`, `data-promote-staged`, model training |

Legacy API write calls return HTTP 410, and legacy CLI write commands exit non-zero with the canonical replacement. A request without an explicit active account must never scan or execute pending plans globally.

## Data Foundation Daily Update

```bash
cd <repo-root>
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-daily-preflight --target-date auto
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-stage-update --target-date auto --dry-run
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-daily-routine --target-date auto --timeout-minutes 180
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-production-audit --replace-from-date <YYYYMMDD> --deep-sample-count 20 --write-report
```

Production write scope:

- staging: `./runtime/data_foundation/staging/{package_id}/`
- raw production: `./data/raw/tushare/`
- Qlib: `./data/qlib/`
- QuantGPT: `./data/quantgpt/`
- pointer: `./runtime/data_foundation/CURRENT_PRODUCTION_DATASET.json`
- status: `./runtime/data_foundation/latest_status.json`, `./runtime/data_foundation/daily_update_status.json`
- backups: `./runtime/data_foundation/production_backups/{promotion_id}/`

## Data Foundation Blockers

Data update must stop before rebuild/promote when any hard gate fails:

- Windows host route or WSL direct-network gate routes Tushare through FlClash/TUN
- HDF smoke write/read fails
- source-window Tushare rebuild fails
- source-window quality fails
- merged compatibility quality fails
- duplicate K-line primary keys appear in the checked window
- core price ranges are invalid
- required benchmark indices are missing or stale
- HDF/Qlib/QuantGPT latest dates are inconsistent
- `data-status` reports `partial_promote_status.detected=true`
- production registry/latest status and actual HDF/Qlib/QuantGPT dates disagree
- production lock exists or active production-conflicting processes are running

AmazingData, proxy fallback, and market-data-only fallback are not allowed.

## Trading Steps

Trading steps may run only after data status reports a healthy promoted production dataset.

```bash
cd <repo-root>
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-status
PYTHONPATH=<repo-root> .venv/bin/python cli.py paper-fleet-status
PYTHONPATH=<repo-root> .venv/bin/python cli.py paper-fleet-preflight
PYTHONPATH=<repo-root> .venv/bin/python cli.py paper-fleet-run
```

If prediction runtime cache needs repair and production data is already healthy, run only:

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py pred-update --to-date <qlib_latest>
```

Do not start data staging or promote from the trading chain.

`fxalpha-data-daily.service` may trigger `fxalpha-paper-fleet-daily.service` only through systemd `OnSuccess`; a blocked or failed data service must never trigger trading. Fleet execution is idempotent by account, signal date and configuration hash.

For replay semantics, account/deployment isolation, evidence paths and recovery rules, use `./docs/PRODUCTION_MULTI_MODEL_PAPER_TRADING_CURRENT.md` and `./docs/TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md`.

## Required Heartbeat Report Fields

Data heartbeat reports must include:

- current stage
- production `latest_trade_date`
- selected `target_date`
- `replace_from_date`
- number of trade dates refreshed
- source-window quality pass/fail
- merged compatibility quality pass/fail
- promote status
- promote journal path
- HDF/Qlib/QuantGPT latest-date alignment
- production consistency and partial promote status
- schema alignment for `LIST_DATE/list_date/delist_date/up_limit/down_limit`
- post-promote audit report path and 20-stock direct Tushare sample result
- cleanup preview path and reclaimable bytes; execute path/reclaimed bytes only
  when a separately approved cleanup operation exists
- current blocker
- code/config changes made during the run
- commands run
