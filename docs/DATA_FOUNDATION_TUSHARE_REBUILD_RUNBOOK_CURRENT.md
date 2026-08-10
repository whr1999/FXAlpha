# FXAlpha Tushare Rebuild Runbook

Updated: 2026-08-01

This is the current runbook for Tushare-only source rebuilds. It covers source staging packages; production is changed only by the promote path described in `DATA_FOUNDATION_DAILY_RUNBOOK_CURRENT.md`.

## Scope

Current rebuild target:

- source: Tushare direct API
- schema: `tushare_v1`
- research table: `research_daily`
- PIT padding: 120 trading days before the first requested target trading day
- default network mode: `direct`
- default assembly chunking: small trading-date chunks
- production source fallback: none

AmazingData has been removed and is not an available rebuild source or fallback.

## Entry Points

Read-only source preflight:

```bash
cd <repo-root>
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-tushare-preflight \
  --start-date 20180101 \
  --cutoff-date <YYYYMMDD> \
  --pad-trading-days 120 \
  --proxy-mode direct
```

Direct network preflight:

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-tushare-network
```

Dry-run full rebuild plan:

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-tushare-full-rebuild \
  --start-date 20180101 \
  --cutoff-date <YYYYMMDD> \
  --pad-trading-days 120 \
  --proxy-mode direct \
  --dry-run
```

Real rebuild:

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-tushare-full-rebuild \
  --start-date 20180101 \
  --cutoff-date <YYYYMMDD> \
  --pad-trading-days 120 \
  --proxy-mode direct
```

Status:

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-tushare-full-rebuild-status
```

## Package Layout

Each rebuild writes a new staging package under:

```text
./runtime/data_foundation/staging/tushare-fullrebuild-*
```

Package structure:

```text
bronze/tushare_raw/
  stock_basic/
  stock_st/
  daily/
  daily_basic/
  adj_factor/
  moneyflow/
  margin_detail/
  pro_bar_hfq/              # retained layout slot; empty in local-derivation mode
  income/
  balancesheet/
  fina_indicator/
  holder_num/
  cyq_perf/
  index_daily/
silver/
  research_daily.h5
  canonical_daily.h5
  index_daily.h5
  metadata.json
  quality_report.json
manifest.json
full_rebuild_progress.json
```

## Download Strategy

Trade-date batch endpoints:

- `daily`
- `daily_basic`
- `adj_factor`
- `moneyflow`
- `margin_detail`
- `stock_st`
- `stk_limit`

Code-by-code endpoints:

- `income`
- `balancesheet`
- `fina_indicator`
- `holder_num`
- `cyq_perf`
- `index_daily`

Each source stage writes parquet partitions to disk immediately. Resume uses `full_rebuild_progress.json`; a cursor advances only after the partition is written.

HFQ is not a code-by-code API stage. The rebuild derives
`hfq_open/hfq_high/hfq_low/hfq_close = daily OHLC * adj_factor` locally and
records `hfq_derivation.mode=local`, the formula, and `api_calls=0` in the
manifest. Raw quality validates daily OHLC and adjustment-factor coverage; it
must not require nonexistent `pro_bar_hfq` partitions. The empty layout slot is
still accepted so old package readers remain compatible.

Provider quota, network interruption, WSL OOM, and unexpected process loss are
recoverable blockers. Resume the same package and source package id; do not
create a new package to bypass a failed `research_daily`, `stk_limit`, or other
source stage.

`stock_basic` is downloaded as three status snapshots: `L`, `P`, and `D`.
Only `L/P` enter the market-data download universe. `D` is kept for status
truth and historical metadata. `stock_st` is downloaded by trade date and
drives `st_status=ST`; name fallback is only allowed for `st_status`, not for
overriding `list_status`.

## Network Contract

Tushare must run in direct mode:

- proxy environment variables are cleared inside the client
- direct DNS/IP candidates are checked
- requests use `trust_env=False`
- real Tushare IP probes must not resolve to `198.18.*`
- WSL and Windows host route gates must not route Tushare IPs through FlClash/TUN
- no proxy fallback and no alternate source fallback

If the direct-network gate fails, the rebuild fails before download.

## Memory And Reliability Rules

- raw source stages write small files immediately
- full-history frames are not kept in memory during download
- source assembly reads bounded windows
- daily update merges into a staged full HDF with temp-file validation and
  atomic replace before promote
- Qlib daily update copies production seed and patches bins from
  `replace_from_date`
- partial staged HDF files must not be reused as production
- smoke tests may limit scope with `--max-trade-days` and `--max-codes`

## Quality Interpretation

Hard blockers:

- source API failure or incomplete source-window rebuild
- duplicate K-line primary keys
- core price-range violations
- required benchmark index missing or stale
- required production compatibility fields missing
- latest-date mismatch across HDF/Qlib/QuantGPT after promote

Warnings unless compatibility is broken:

- `pe_ttm_structural_missing`
- `moneyflow_coverage_gap`
- `margin_detail_coverage_gap`
- `cyq_perf_row_count_mismatch_codes`

Missing values must remain missing; do not synthesize `0` or forward-fill outside the existing PIT repair rules.

## Promotion Boundary

Full rebuild packages do not directly update production.

To promote a completed rebuild package, use the staged production promote path:

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-promote-staged --package-id <package_id>
```

For normal production daily updates, use:

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-daily-routine --target-date auto --timeout-minutes 180
```

After daily promote or rebuild promote, run the layered production audit:

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-production-audit \
  --replace-from-date <YYYYMMDD> \
  --deep-sample-count 20 \
  --write-report
```

Use `--full-scan` only after a layered audit failure, on a weekend/manual
maintenance pass, or when explicitly checking full-history duplicate primary
keys.
