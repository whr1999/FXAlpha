# FXAlpha Current Production Dataset Registry

Updated: 2026-06-17

## Source Of Truth

The current production dataset is machine-readable from:

- `./runtime/data_foundation/CURRENT_PRODUCTION_DATASET.json`
- `./runtime/data_foundation/latest_status.json`
- `./runtime/data_foundation/daily_update_status.json`

Do not copy a historical package id or trade date from this document into code or prompts. Read the pointer files before every production decision.

Current production policy:

- `source`: `tushare`
- `schema_version`: `tushare_v1`
- `compatibility_mode`: `tushare_raw_hdf_compat`
- AmazingData has been removed and is not an available production or fallback data source.

## Canonical Production Paths

| Asset | Path | Meaning |
| --- | --- | --- |
| Raw HDF | `./data/raw/tushare/stock_daily.h5` | Production HDF with raw prices, backward factor, and adjusted compatibility columns. |
| Raw metadata | `./data/raw/tushare/metadata.json` | Production source, schema, price mode, and latest-date metadata. |
| Trading calendar | `./data/raw/tushare/trade_calendar.txt` | Data-foundation production calendar and target-date truth. |
| Trading calendar meta | `./data/raw/tushare/trade_calendar_meta.json` | Calendar generation metadata. |
| Quality report | `./data/raw/tushare/tushare_quality_report.json` | Current production compatibility quality report. |
| Raw quality report | `./data/raw/tushare/tushare_raw_quality_report.json` | Current source-window/raw quality report. |
| Qlib | `./data/qlib/` | Qlib provider assets generated from promoted production data. |
| QuantGPT stocks | `./data/quantgpt/stocks/` | Adjusted-price stock parquet files. |
| QuantGPT benchmark | `./data/quantgpt/benchmark/` | Benchmark parquet files. |

Never read production data from `./runtime/data_foundation/staging/`.

## Consumer Semantics

- QuantGPT consumes adjusted research prices from `adj_*`.
- Qlib conversion consumes raw OHLC plus explicit `backward_factor` directly from the production HDF.
- Qlib consumes adjusted OHLC as canonical provider price fields and retains `raw_*` plus `$factor` for audit and trade-unit handling.
- Missing values remain `NaN` / `pd.NA` / `NaT`.
- `high_limited` and `low_limited` are not part of the current Tushare production field contract.

## Hard Gates

Promotion requires:

- source-window rebuild completed successfully
- source-window quality passed
- merged compatibility quality passed
- no duplicate `code + kline_time` keys in the checked window
- core price ranges are valid (`low <= open/close <= high`)
- required benchmark indices `000300.SH`, `000905.SH`, `000852.SH` are present and aligned to the latest HDF date
- HDF, Qlib, QuantGPT stock parquet, and QuantGPT benchmark latest dates are aligned

Known warnings that are not blockers unless they break compatibility:

- structural `pe_ttm` gaps
- sparse `moneyflow`
- sparse `margin_detail`
- `cyq_perf` coverage-length differences across stocks

## Promotion And Cleanup Rules

- `data-stage-update` writes only staging packages.
- `data-promote-staged` is the only command that updates production paths, and it rejects a staged daily package unless its canonical `quality_report.json` is a passing full report with field, activity, metadata, limit-price, adjusted-price, and schema summaries.
- `data-daily-routine` may call promote after all gates pass.
- After promote, the routine writes a safe cleanup dry-run report.
- Weekly safe cleanup may execute only through `domain.platform_ops.cleanup_executor.run_cleanup(profile="safe")`; do not manually delete staging or backup trees.

Protected cleanup assets include the current production package, current promotion backup, recent packages, packages newer than 24 hours, and any busy/locked data-foundation state.
