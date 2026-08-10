# FXAlpha Data Foundation Daily Runbook

Updated: 2026-08-01

## Current Daily Contract

Production daily updates now run on the Tushare staged-safe path:

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-daily-preflight --target-date auto
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-daily-routine --target-date auto --timeout-minutes 180
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-production-audit --replace-from-date <YYYYMMDD> --deep-sample-count 20 --write-report
```

The GUI defaults remain:

- `GET /data/status`
- `POST /data/daily-preflight`
- `POST /data/stage-update` with `dry_run=true`
- `POST /data/daily-routine` with `dry_run=true`

Real execution stays behind confirmation.

`GET /data/status` now exposes `production_consistency`, `partial_promote_status`,
and `latest_audit_report`. A detected partial promote blocks the next daily
preflight until the same package is reconciled or the mismatch is explicitly
handled.

## Production Workflow

The current production workflow is:

1. Read the current production pointer from `runtime/data_foundation/CURRENT_PRODUCTION_DATASET.json`.
2. Run Tushare direct-network preflight, host-route gate, WSL headless stability gate, HDF smoke write/read, and cleanup preview.
3. Check the latest daily staging manifest. If the same target and replace boundary already have an unfinished package, resume that package and keep the same source package id. `selected_target_date` is fixed in that manifest, so a run that crosses midnight cannot silently move to a new target.
4. Compute the rolling refresh window from the current production latest date to the target date.
5. Launch or resume a short-window Tushare rebuild package starting from the current production latest date.
6. If source progress says `running` but no data process is active and the progress timestamp is stale, report `interrupted_resumable`; resume the same source package instead of creating a new one.
7. Prepare compatibility artifacts for that short-window package.
8. Merge the refreshed compatibility HDF back into the current production full HDF, replacing rows from the current production latest date onward. The merge aligns compatibility columns such as `LIST_DATE`, `list_date`, and `delist_date` before appending, and extends old production schemas with `up_limit`, `down_limit`, and `limit_source_kind`.
9. Write a canonical `trade_calendar.txt` for the staged production bundle from the merged production HDF trading dates.
10. Build staged downstream outputs incrementally: Qlib copies the production
    seed and patches bins directly from raw HDF at `replace_from_date`; QuantGPT refreshes the
    promoted stock/benchmark views from the staged compatibility bundle.
11. Run the fast staged compatibility gate with `profile="daily_compat"` and the current `replace_from_date`.
12. Materialize the canonical full-history production quality report with `profile="deep_full"`. It must contain field coverage, latest-stock activity, metadata, limit-price, adjusted-price, and schema summaries.
13. Promote the staged package only after both reports pass. `data-promote-staged` independently rejects a package with an absent, failed, or incomplete canonical quality report. After creating `promote_journal.json`, it closes the consumer gate with `promotion_in_progress` before replacing the first production target, verifies staged-vs-production target equivalence before status commit, and reconciles equivalent half-promotes on the next run. Any rollback restores artifacts and the three state files together.
14. Write the post-promote audit and safe cleanup preview. The routine output includes the audit report path, cleanup preview, and promote journal path. Cleanup execution is a separate approval-controlled operation and is never launched by the daily routine. The audit also persists the exact QuantGPT latest-day coverage snapshot, so GUI polling reads stored facts rather than re-scanning production parquet.

AmazingData code, commands, credentials, and cache assets have been removed.

## Heartbeat Resume-First Procedure

Heartbeat value-stewardship is narrow:

1. Confirm the workspace is `<repo-root>`.
2. Inspect `runtime/data_foundation/latest_status.json`, `runtime/data_foundation/daily_update_status.json`, and `python3 cli.py data-status`.
3. If a data job is already running or queued, report status only. Do not start a second routine.
4. Run `python3 cli.py data-tushare-network`. If direct routing fails, stop and report the blocker.
5. Run `python3 cli.py data-daily-preflight --target-date auto`.
6. If `already_current=true`, stop and report that production is current.
7. If an unfinished same-target daily package exists, start the same managed service/routine once. It reuses the daily package, source package, completed source cursors, and finished compatibility artifacts. Do not separately start `data-stage-update` and then a second routine.
8. If WSL OOM, `E_UNEXPECTED`, provider quota, or network failure interrupts the source rebuild, report the environment or provider blocker and keep the package resumable. Do not create a new package, switch data sources, or use proxy/fallback paths.
9. Only when preflight returns `status=go` and `already_current=false`, run `python3 cli.py data-daily-routine --target-date auto --timeout-minutes 180`.
10. After promote, inspect `post_promote_audit`; use
    `PYTHONPATH=<repo-root> .venv/bin/python cli.py data-production-audit --replace-from-date <YYYYMMDD> --deep-sample-count 20 --write-report`
    for a standalone layered check. Add `--full-scan` only when a full HDF duplicate scan is explicitly needed.

The first preflight resource sample is taken before network, consistency, and
HDF probes. If available memory is the only blocker, the routine may wait up to
15 minutes for two consecutive passing samples. It does not lower the 8 GiB
threshold, kill unrelated processes, or bypass any other blocker.

The user systemd timer runs at 02:00 Tuesday-Saturday and also enters the
idempotent routine five minutes after the user manager starts. The startup
trigger covers a WSL/user-manager restart that interrupted an already-fired
oneshot: the same package is resumed, while an already-current dataset exits
without rebuilding. Never start a duplicate routine while the service is
`activating`.

## Data Model Expectations

The daily path and the full rebuild share the same production contract:

- raw price fields remain raw
- `backward_factor` is stored explicitly
- `hfq_open/hfq_high/hfq_low/hfq_close` are derived locally as raw daily OHLC
  multiplied by `adj_factor`; the rebuild records `hfq_derivation.api_calls=0`
  and does not expect per-code `pro_bar_hfq` files
- `trade_calendar.txt` is the canonical production trading calendar
- Tushare `stk_limit` is part of both full rebuild and daily rebuild source stages; it is downloaded by trade date with `trade_date`, `ts_code`, `pre_close`, `up_limit`, and `down_limit`
- adjusted research fields live under `adj_*`
- `up_limit` and `down_limit` stay in raw price space in raw HDF; downstream adjusted Qlib audit fields are generated only by applying the same factor transform as price fields
- `stk_limit_pre_close` is preserved from Tushare and is the canonical stock
  `pre_close`, including corporate-action dates; previous-day close is only a
  missing-source fallback
- Qlib also exports `$raw_up_limit/$raw_down_limit` for audits; formal trading
  constraints use precomputed boolean limit fields, not ad hoc comparisons
  between raw and adjusted price spaces
- `limit_source_kind` records `official`, `structural_no_limit`, `missing`, or `index`; production quality must have no stock rows with `missing`
- `list_status` and `st_status` are present on production raw HDF and downstream
  stock metadata surfaces
- `list_status` is kept from Tushare `stock_basic`; name text containing `退市`
  does not force the listing status to `D`
- `st_status` uses `stock_st` plus name/listing fallback with values
  `NORMAL/ST/DELIST`
- QuantGPT consumes adjusted prices from `adj_*`
- QuantGPT preserves `list_status` and `st_status` for tradability filtering
- Qlib provider exposes adjusted OHLC as canonical `$open/$high/$low/$close/$pre_close`,
  plus `$factor`, `$change`, and `raw_*` audit fields
- Qlib must not treat `st_status` as a numeric factor
- missing values remain `NaN` / `pd.NA` / `NaT`

## Network Rules

Tushare production daily work must pass the same direct-network gate as the rebuild:

- `python3 cli.py data-tushare-network`
- Windows host route guard for Tushare `/32` routes
- WSL direct DNS/IP probe
- no proxy fallback
- no fallback source
- no market-data-only mode

If direct routing, source-window rebuild, merged compatibility quality, or downstream staged conversion fails, staging fails and production stays unchanged.

## Quality Gates

The daily routine uses two quality layers:

1. Source-window Tushare rebuild quality
   This validates the short rebuild package before it is used as refresh truth.

2. Merged compatibility quality
   This validates the merged production-compatible HDF after the refreshed window is written back into the current full history. The daily profile checks the refreshed window first: required fields, duplicate `code + kline_time`, price ranges, latest core nulls, and benchmark latest-date alignment.

3. Promotion compatibility snapshot
   This validates staged Qlib, QuantGPT stock parquet, and QuantGPT benchmark latest-date alignment before production paths are replaced.

4. Status field gate
   The production HDF and QuantGPT stock parquet must contain `list_status` and
   `st_status`. Trading and model-validation filters must exclude
   `list_status=D` and `st_status in {"ST", "DELIST"}`.

5. Official limit-price gate
   The production HDF must contain `up_limit` and `down_limit`. Every non-index stock row must have official Tushare limit prices or an explicit `structural_no_limit` marker for a no-limit day such as listing day. Missing official prices are promotion blockers; the flow must not silently fall back to hard-coded 10.5 percent thresholds.

6. Post-promote production audit
   `data-daily-routine` includes `post_promote_audit`, and
   `data-production-audit` can be run independently. The default layered audit
   checks the replace window for duplicate `code + kline_time`, latest-window
   core nulls, structural listing-day `pre_close` nulls, price sanity, schema
   alignment for `LIST_DATE/list_date/delist_date/up_limit/down_limit`,
   production quality report status, HDF / Qlib / QuantGPT
   latest-date alignment, and 20 direct Tushare sample rows. Qlib `.day.bin`
   price comparisons use float32 tolerance `0.01` and report warnings instead
   of overreacting to representation noise. Use `--full-scan` only when the
   operator needs a full-table duplicate check.

A failed post-promote audit is a hard consumer gate. The data may already have
been atomically promoted, but the routine returns `promoted_audit_failed`,
persists the failed audit, disables consumer readiness, and blocks pipeline and
trading preflight until a passing audit reopens the gate.

The gate value is mirrored in `CURRENT_PRODUCTION_DATASET.json`, the
`latest_status.json` top level, and its snapshot. A passing audit must set all
three to `open`; a failure must set them to `blocked_by_production_audit`.

Warnings that are known source-coverage behaviors, such as sparse `moneyflow`, sparse `margin_detail`, or structural `pe_ttm` gaps, remain warnings instead of promotion blockers unless they break the canonical compatibility checks.

## Cleanup

After a successful promote, `data-daily-routine` writes:

- `post_promote_cleanup_preview`
- `post_promote_cleanup_execute`
- `post_promote_cleanup_policy`

The preview is always generated. `data-daily-routine` never executes cleanup,
regardless of reclaimable size. Execution requires a separate explicit approval
and must use the governed safe profile. Current production package, current
backup, recent packages, 24-hour fresh packages, and busy states remain
protected.

## Capacity Observation

The canonical `deep_full` quality report still scans the full 9-million-row HDF
and on the 2026-08-01 run peaked near 18 GiB RSS, using swap before releasing
memory. This did not affect correctness and the governed run completed, but the
production host must retain the 8 GiB available-memory gate and configured swap.
A future performance change may replace this scan with an evidence-equivalent
streaming aggregator; it must not weaken the canonical report fields or gates.
