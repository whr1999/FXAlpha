# FXAlpha Data Foundation Workflow

Updated: 2026-08-01

## Scope

This document describes the current Tushare-based data-foundation production workflow, including:

- full rebuild
- production daily update
- production compatibility export
- downstream consumer regeneration

AmazingData code and local cache assets have been removed; it is not an available data source or fallback.

## Canonical Paths

### Production read paths

- production pointer: `./runtime/data_foundation/CURRENT_PRODUCTION_DATASET.json`
- production raw HDF: `./data/raw/tushare/stock_daily.h5`
- production raw metadata: `./data/raw/tushare/metadata.json`
- production trade calendar: `./data/raw/tushare/trade_calendar.txt`
- production trade calendar meta: `./data/raw/tushare/trade_calendar_meta.json`
- production assembled quality: `./data/raw/tushare/tushare_quality_report.json`
- production raw quality: `./data/raw/tushare/tushare_raw_quality_report.json`
- production Qlib: `./data/qlib/`
- production QuantGPT stocks: `./data/quantgpt/stocks/`
- production QuantGPT benchmark: `./data/quantgpt/benchmark/`
- production audit reports: `./runtime/data_foundation/audits/`
- promote journals: `./runtime/data_foundation/production_backups/{promotion_id}/promote_journal.json`

### Code entrypoints

- full rebuild: `./domain/data_foundation/tushare_rebuild.py`
- production compat export: `./domain/data_foundation/tushare_production.py`
- daily update controller: `./domain/data_foundation/tushare_daily.py`
- status backfill: `./domain/data_foundation/tushare_status_backfill.py`
- quality check: `./domain/data_foundation/quality_check.py`
- QuantGPT conversion: `./domain/data_foundation/convert_to_quantgpt.py`
- Qlib conversion wrapper: `./scripts/data_foundation/convert_to_qlib.py`
- service entrypoints: `./services/data_foundation_service.py`
- CLI entrypoints: `./cli.py`

## Full Rebuild Workflow

| Step | Code | Input | Output |
| --- | --- | --- | --- |
| 1. direct-network preflight | `integrations/tushare/client.py`, `tushare_rebuild.tushare_preflight` | target cutoff date, direct network config, Tushare token | validated route, selected target date, padded start date, trade-date list, code universe |
| 2. source raw download | `tushare_rebuild.tushare_full_rebuild` | preflight plan, Tushare APIs | staged bronze raw parquet files under `runtime/data_foundation/staging/<package>/bronze/tushare_raw/` |
| 3. slow-field PIT alignment | `tushare_rebuild._merge_pit` and assembly helpers | raw financial, holder, margin, chip, moneyflow tables | PIT-safe merged daily frame |
| 4. research table assembly | `tushare_rebuild._assemble_research_daily` | raw daily, daily_basic, adj_factor, PIT slow fields | locally derived HFQ plus `silver/research_daily.h5` |
| 5. raw quality audit | `tushare_rebuild._build_raw_quality_report` | staged bronze outputs, list dates, target date | `silver/raw_quality_report.json` |
| 6. assembled quality audit | `tushare_rebuild._build_quality_report` | `silver/research_daily.h5`, benchmark index HDF | `silver/quality_report.json` |
| 7. index export | `tushare_rebuild` index stage | Tushare `index_daily` | `silver/index_daily.h5` |
| 8. package completion | `tushare_rebuild` manifest/status writers | full staged package | `manifest.json`, `full_rebuild_progress.json`, `tushare_full_rebuild_status.json` |

## Production Compat Export Workflow

| Step | Code | Input | Output |
| --- | --- | --- | --- |
| 1. normalize staged research table | `tushare_production._normalize_stock_chunk` | `silver/research_daily.h5` | legacy-compatible raw+adj rows |
| 2. append benchmark indices | `tushare_production._normalize_index_frame` | `silver/index_daily.h5` | unified `/daily` HDF table |
| 3. write production raw HDF | `tushare_production.prepare_tushare_production_artifacts` | normalized stock/index frames | `production_compat/raw/stock_daily.h5` |
| 4. write production raw metadata | `tushare_production._write_compat_metadata` | package manifest, quality references | `production_compat/raw/metadata.json` |
| 5. write trade calendar | `tushare_production._write_trading_calendar` | compat raw HDF `/daily.kline_time` | `production_compat/raw/trade_calendar.txt` and meta |
| 6. rebuild Qlib | `scripts/data_foundation/convert_to_qlib.py` -> `tushare_raw_to_qlib.py`; `scripts/data_foundation/convert_index_to_qlib.py` -> `tushare_index_to_qlib.py` via subprocess | compat raw HDF | `production_compat/qlib/` |
| 7. rebuild QuantGPT | `convert_to_quantgpt.convert` | compat raw HDF | `production_compat/quantgpt/stocks`, `production_compat/quantgpt/benchmark` |
| 8. snapshot compatibility state | `tushare_production._snapshot_for_compat` | compat assets | latest-date summary for HDF/Qlib/QuantGPT |

## Status Backfill Workflow

Status backfill is the migration path for adding or refreshing only
`list_status` and `st_status` on an existing production raw HDF. It never
promotes directly.

| Step | Code | Input | Output |
| --- | --- | --- | --- |
| 1. read source production HDF | `tushare_status_backfill.build_tushare_status_backfill` | `./data/raw/tushare/stock_daily.h5` | source row count and existing status distribution |
| 2. fetch status truth | `stock_basic(L/P/D)`, `stock_st(trade_date=...)` | Tushare direct API | staged `raw/stock_basic/all.parquet`, `raw/stock_st/all.parquet` |
| 3. chunked status fill | `_apply_status_fields` | source HDF chunks + status truth | staged `raw/stock_daily.h5` with `list_status` and `st_status` |
| 4. metadata/report write | `tushare_status_backfill` | source metadata + counts | staged `raw/metadata.json`, `status_backfill_report.json`, `manifest.json` |
| 5. quality gates | `quality_check.check` | staged status HDF | `deep_full` and `daily_compat` must pass before promote |

The status backfill must preserve row count and all existing market columns.
For the 2026-06-18 live status backfill validation package
`tushare-status-backfill-live-20260618-final4`, source and output rows were
both `8,854,055`; the only added HDF column was `st_status`.

## Daily Update Workflow

| Step | Code | Input | Output |
| --- | --- | --- | --- |
| 1. read current production pointer | `tushare_daily._require_tushare_production` | `CURRENT_PRODUCTION_DATASET.json` | current latest trade date and source package |
| 2. compute refresh window | `tushare_daily.data_daily_preflight` -> `tushare_rebuild.tushare_preflight` | current latest date, `--target-date auto` or explicit target, Tushare trade calendar | `selected_target_date`, `replace_from_date`, short rebuild plan |
| 3. short-window rebuild | `tushare_daily.data_stage_update` -> `tushare_full_rebuild` | `replace_from_date .. selected_target_date` | staged source package `<daily-package>-source` |
| 4. source compat export | `prepare_tushare_production_artifacts` | source short-window package | source compat bundle under source package |
| 5. merge refreshed window into full history | `tushare_daily._merge_compat_hdf` | current production raw HDF + source delta HDF + `replace_from_date` | staged merged full HDF |
| 6. write staged trade calendar | `tushare_production._write_trading_calendar` | staged merged HDF | staged `trade_calendar.txt` |
| 7. build staged downstream consumers | `tushare_daily._build_compat_outputs` | staged merged HDF, current production downstream seed, refreshed window | staged Qlib and QuantGPT outputs |
| 8. staged quality gates | `quality_check.check(profile="daily_compat", replace_from_date=...)` and `quality_check.check(profile="deep_full")` | staged merged HDF and source-window reports | fast refresh-window gate plus canonical full dashboard report |
| 9. promote | `tushare_daily.data_promote_staged` | completed staged daily package with a complete passing canonical quality report | journal, consumer gate close before first target replace, staged-vs-production equivalence check, pointer/status commit; incomplete reports are blocked |
| 10. post-promote audit | `tushare_daily.production_audit_summary` | production assets and `replace_from_date` | layered audit JSON under `runtime/data_foundation/audits/` |
| 11. post-promote cleanup preview | `platform_ops.cleanup_executor.run_cleanup(profile="safe", dry_run=True)` | staging/backup/cache state after promote | preview report only; execution requires separate explicit approval |

Daily downstream generation is incremental by default:

- Qlib daily output copies the current production seed and patches `.day.bin`
  files from `replace_from_date`; historical bins are preserved.
- raw HDF merge writes a temp HDF, validates it, then atomically replaces the
  staged output. A failed merge must not clobber the old production HDF.
- routine-path large-table reads must be windowed or chunked; full HDF scans are
  reserved for explicit audit commands.

## Trading Calendar Policy

There are three calendar layers:

1. Tushare `trade_cal`
   - used to compute target-date windows for rebuild and daily update
2. production raw `trade_calendar.txt`
   - canonical production calendar owned by data foundation
3. Qlib `data/qlib/calendars/day.txt`
   - downstream consumer calendar generated from promoted production outputs

The production daily workflow must keep layer 2 and layer 3 aligned on latest date.

## Current Production Semantics

- raw HDF stores raw prices plus compatibility adjusted columns
- HFQ columns are derived locally as raw daily OHLC multiplied by
  `adj_factor`; there is no per-code `pro_bar` source stage
- raw HDF uses Tushare `stk_limit.pre_close` as the official stock `pre_close`;
  previous-day close is only a fallback when the official field is absent
- raw HDF stores `list_status` and `st_status`; `list_status` is the official
  Tushare listing status and `st_status` is ST/delisting filter metadata
- QuantGPT consumes adjusted research prices
- QuantGPT stock parquet preserves `list_status` and `st_status` for filtering
- Qlib is generated directly from raw prices plus explicit factor
- missing values remain `NaN` / `pd.NA` / `NaT`
- `trade_calendar.txt` is the canonical production trade-date truth for orchestration and audit
- a failed post-promote production audit closes the consumer-readiness gate and
  returns `promoted_audit_failed`; downstream model, pipeline, and trading work
  must not treat the promotion as a clean completion

## Retired And Diagnostic Utilities

- the unsafe, unreferenced `domain/data_foundation/pit_repair.py` production-HDF
  rewrite utility was retired on 2026-07-13; PIT repairs must use a staged,
  explicitly governed migration
- raw-dataset comparison is an operator diagnostic under
  `scripts/data_foundation/diagnostics/compare_raw_datasets.py`, not domain logic

## Operational Commands

```bash
cd <repo-root>
python3 cli.py data-daily-preflight --target-date auto
python3 cli.py data-stage-update --target-date auto --dry-run
python3 cli.py data-daily-routine --target-date auto --timeout-minutes 180
python3 cli.py data-production-audit --replace-from-date <YYYYMMDD> --deep-sample-count 20 --write-report
```

Use the WSL production interpreter in automation:

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py <command>
```

## Cleanup Policy

The production daily routine writes a safe cleanup preview report after
successful promote. It never executes cleanup automatically, regardless of
reclaimable size. Safe cleanup protects:

- current production package
- current promotion backup
- recent packages
- packages newer than 24 hours
- busy/locked data-foundation state

Safe execution is allowed only through the cleanup executor after separate
explicit approval. Do not manually delete `runtime/data_foundation/staging` or
`runtime/data_foundation/production_backups`.
