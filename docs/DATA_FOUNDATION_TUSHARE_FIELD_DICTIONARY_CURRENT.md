# FXAlpha Tushare Field Dictionary

Updated: 2026-08-01

This document is the current field contract for the Tushare-backed data foundation rebuild.

It replaces the old implicit AmazingData assumption that `open/high/low/close/pre_close` may already be adjusted. The Tushare rebuild must store raw market facts and adjusted research prices as separate columns.

## Source References

Official Tushare pages used by this contract:

- API overview: https://tushare.pro/document/2?doc_id=14
- Daily market data: https://tushare.pro/document/2?doc_id=27
- Adjusted bar interface: https://tushare.pro/document/2?doc_id=146
- Daily basic indicators: https://tushare.pro/document/2?doc_id=32
- Stock basic / listing status: https://tushare.pro/document/2?doc_id=25
- ST status: https://tushare.pro/wctapi/documents/397.md
- Daily limit prices: https://tushare.pro/wctapi/documents/183.md
- Chip distribution: https://tushare.pro/document/2?doc_id=294
- Moneyflow: https://tushare.pro/document/2?doc_id=170

The local MCP server exposes the same core interfaces for small probes. Full rebuild jobs should use the Python SDK under `./integrations/tushare/`, because long-running full-market rebuilds need checkpointing, batching, retries, and local manifests.

## Schema Policy

The canonical Tushare dataset must use this semantic split:

| Layer | Columns | Meaning |
| --- | --- | --- |
| Raw facts | `open/high/low/close/volume/amount` | Unadjusted daily bars from Tushare `daily`. |
| Adjustment | `adj_factor` | Tushare adjustment factor from `adj_factor`. |
| Official limit prices | `up_limit/down_limit` | Raw-space limit prices from Tushare `stk_limit`; not model alpha features. |
| Research prices | `hfq_open/hfq_high/hfq_low/hfq_close` | Locally derived as raw Tushare `daily` OHLC multiplied by Tushare `adj_factor`; no `pro_bar` call. |
| Official daily indicators | `pe/pb/total_mv/float_mv/turnover_rate/...` | Tushare `daily_basic`; do not recompute locally for the canonical dataset. |
| PIT slow fields | financial, holder fields | Point-in-time aligned by announcement/end dates. |
| Selected research features | moneyflow, chips | Only a small selected subset is merged into the unified research daily table. |

Missing slow fields remain `NaN`. Do not fill missing financial, holder, margin, chip, or moneyflow values with `0`.

## Current Production Consumer Contract

As of the production promote completed on `2026-06-26`, FXAlpha production does
not expose the 49-field `research_daily` table directly to every consumer in
the same physical format. Instead, the promoted Tushare package is exported
through consumer-specific production surfaces:

| Consumer | Production path | Price semantics | Notes |
| --- | --- | --- | --- |
| Raw compat HDF | `./data/raw/tushare/stock_daily.h5` | raw price columns plus legacy adjusted compat columns | canonical production compat bundle |
| Production trading calendar | `./data/raw/tushare/trade_calendar.txt` | canonical production trade-date list | daily orchestration and audit truth |
| QuantGPT | `./data/quantgpt/stocks` | adjusted research prices | stock parquet only; benchmark parquet stays under `/benchmark` |
| Qlib | `./data/qlib` | adjusted canonical OHLC plus raw audit fields | latest calendar must match production target date; `$change` is adjusted return decimal |

Production export rules:

- QuantGPT default price fields come from the adjusted layer
- Raw compat HDF, stock identity cache, and QuantGPT stock parquet preserve `list_status` and `st_status`
- Qlib conversion reads the raw layer directly and applies `backward_factor` in memory
- Qlib canonical price fields come from adjusted OHLC; raw prices are retained as `raw_*`
- Qlib limit-price audit fields are adjusted with the same factor as OHLC, but trading constraints use boolean fields generated from official raw-space limit prices before conversion
- QuantGPT may expose `up_limit/down_limit` for audit, but factor mining must not use market-rule fields as alpha features
- `adj_factor` / `backward_factor` remains explicit for audit and downstream transforms
- benchmark parquet files must not be mixed into the QuantGPT stock universe

## Listing And ST Status Contract

Only two security-status fields are allowed in the production daily surfaces:

| Field | Values | Source | Rule |
| --- | --- | --- | --- |
| `list_status` | `L/P/D/I` | Tushare `stock_basic.list_status`; index rows use `I` | Do not force `D` from name text. For stock rows, keep the official Tushare listing status. |
| `st_status` | `NORMAL/ST/DELIST` | Tushare `stock_st`, plus name fallback | `DELIST` when `list_status=D` or the security name contains `退市`; `ST` when `stock_st(code, trade_date)` matches or the name starts with `ST`, `*ST`, or `SST`; otherwise `NORMAL`. |

Status precedence is `DELIST > ST > NORMAL`. Index rows are always
`list_status=I` and `st_status=NORMAL`.

Full rebuild and daily source rebuild download `stock_basic` for `L/P/D`.
Only `L/P` are used as the new market-data download universe; `D` is retained
for status backfill and historical metadata. `stock_st` is downloaded by
trade date and stored in the staging raw package.

Trading recommendation and model-validation tradability filters must exclude
rows with `list_status=D` or `st_status in {"ST", "DELIST"}`. Qlib must not
treat `st_status` as a numeric factor; it is filter metadata.

## Research Table Contract

The phase-1 research layer must expose one unified daily table for downstream consumers such as QuantGPT and Qlib.

Research daily table:

```text
research_daily
```

Primary key:

```text
(code, trade_date)
```

Design rules:

- QuantGPT and Qlib read one daily research table by default
- slow PIT financial fields are merged into this table after announcement-date alignment
- moneyflow and chip features are merged only as a very small selected subset
- margin features are also merged only as a very small selected subset
- do not bring the full moneyflow detail table, full chip distribution table, or full margin detail table into `research_daily`

## Exact Research Table

### `research_daily`

Purpose:

- the single daily research table consumed by Qlib and QuantGPT in phase 1
- contains raw prices, adjusted prices, daily valuation indicators, PIT financial indicators, and a small selected set of flow and chip features

Exact fields:

| # | Field | Source | Notes |
| --- | --- | --- | --- |
| 1 | `code` | `daily.ts_code` | normalized stock code |
| 2 | `trade_date` | `daily.trade_date` | trading date |
| 3 | `name` | `stock_basic.name` | stock short name |
| 4 | `list_status` | `stock_basic.list_status` | listing status tag; `L/P/D` for stocks, `I` for index rows |
| 5 | `st_status` | `stock_st` plus name/listing fallback | `NORMAL/ST/DELIST`; filter metadata, not a numeric factor |
| 6 | `list_date` | `stock_basic.list_date` | listing date |
| 7 | `open` | `daily.open` | raw open |
| 8 | `high` | `daily.high` | raw high |
| 9 | `low` | `daily.low` | raw low |
| 10 | `close` | `daily.close` | raw close |
| 11 | `up_limit` | `stk_limit.up_limit` | official upper limit price in raw price space |
| 12 | `down_limit` | `stk_limit.down_limit` | official lower limit price in raw price space |
| 13 | `volume` | `daily.vol` | trading volume, unit is hands |
| 14 | `amount` | `daily.amount` | trading amount, unit is thousand CNY |
| 15 | `hfq_open` | `daily.open * adj_factor` | locally derived backward-adjusted open |
| 16 | `hfq_high` | `daily.high * adj_factor` | locally derived backward-adjusted high |
| 17 | `hfq_low` | `daily.low * adj_factor` | locally derived backward-adjusted low |
| 18 | `hfq_close` | `daily.close * adj_factor` | locally derived backward-adjusted close |
| 19 | `adj_factor` | `adj_factor.adj_factor` | kept for audit and downstream custom transforms |
| 20 | `turnover_rate` | `daily_basic.turnover_rate` | official daily indicator |
| 21 | `turnover_rate_f` | `daily_basic.turnover_rate_f` | free-float turnover |
| 22 | `pe_ttm` | `daily_basic.pe_ttm` | official PE TTM |
| 23 | `pb` | `daily_basic.pb` | official PB |
| 24 | `ps_ttm` | `daily_basic.ps_ttm` | official PS TTM |
| 25 | `dv_ttm` | `daily_basic.dv_ttm` | dividend yield TTM |
| 26 | `total_mv` | `daily_basic.total_mv` | total market value, unit is ten-thousand CNY |
| 27 | `float_mv` | `daily_basic.circ_mv` | circulating market value, unit is ten-thousand CNY |
| 28 | `total_share` | `daily_basic.total_share` | total shares, unit is ten-thousand shares |
| 29 | `float_share` | `daily_basic.float_share` | float shares, unit is ten-thousand shares |
| 30 | `free_share` | `daily_basic.free_share` | free-float shares, unit is ten-thousand shares |
| 31 | `eps` | `fina_indicator.eps` or `income.basic_eps` | PIT aligned by announcement date |
| 32 | `net_profit` | `income.n_income_attr_p` | PIT aligned by announcement date |
| 33 | `total_equity` | `balancesheet.total_hldr_eqy_exc_min_int` | PIT aligned by announcement date |
| 34 | `total_assets` | `balancesheet.total_assets` | PIT aligned by announcement date |
| 35 | `roe` | `fina_indicator.roe` | PIT aligned by announcement date |
| 36 | `roa` | `fina_indicator.roa` | PIT aligned by announcement date |
| 37 | `holder_num` | `stk_holdernumber.holder_num` | PIT aligned by disclosure date |
| 38 | `sm_net_vol` | derived from `moneyflow` | `buy_sm_vol - sell_sm_vol`, unit is hands |
| 39 | `sm_net_amount` | derived from `moneyflow` | `buy_sm_amount - sell_sm_amount`, unit is ten-thousand CNY |
| 40 | `lg_net_vol` | derived from `moneyflow` | `buy_lg_vol - sell_lg_vol`, unit is hands |
| 41 | `lg_net_amount` | derived from `moneyflow` | `buy_lg_amount - sell_lg_amount`, unit is ten-thousand CNY |
| 42 | `net_mf_vol` | `moneyflow.net_mf_vol` | total net buy volume, unit is hands |
| 43 | `net_mf_amount` | `moneyflow.net_mf_amount` | total net buy amount, unit is ten-thousand CNY |
| 44 | `cost_15pct` | `cyq_perf.cost_15pct` | raw lower chip cost bound in `research_daily`; QuantGPT/Qlib research-price exports multiply by `backward_factor` |
| 45 | `cost_85pct` | `cyq_perf.cost_85pct` | raw upper chip cost bound in `research_daily`; QuantGPT/Qlib research-price exports multiply by `backward_factor` |
| 46 | `weight_avg` | `cyq_perf.weight_avg` | raw average holding cost in `research_daily`; QuantGPT/Qlib research-price exports multiply by `backward_factor` |
| 47 | `margin_buy_amount` | `margin_detail.rzmre` | financing purchase amount, unit is CNY |
| 48 | `margin_balance` | `margin_detail.rzye` | financing balance, unit is CNY |
| 49 | `short_balance` | `margin_detail.rqye` | securities lending balance, unit is CNY |

Field count:

```text
49 fields
```

Rules:

- this is the only phase-1 downstream daily research table
- raw and adjusted prices are both required in this table
- official `up_limit/down_limit` are audit and trading-constraint inputs, not factor-mining alpha fields
- all financial fields must follow PIT semantics and must not leak future reports
- PIT merge uses announcement-first effective dates with row-wise fallback: `f_ann_date -> ann_date -> trade_date -> end_date`
- if multiple slow-field rows share the same `code + effective_date`, keep the row with the latest `end_date`
- flow and chip features are intentionally reduced to the most important subset
- amount and share fields with different units must keep their source units in metadata and converters
- `pre_close`, `hfq_pre_close`, and `volume_ratio` are not part of the phase-1 research table scope; compatibility raw HDF derives `pre_close` for legacy consumers
- if a new research field is added later, this table definition must be updated explicitly

Missing-value policy:

- numeric missing values remain `NaN`
- string missing values remain `pd.NA`
- date missing values remain `NaT`
- do not replace missing financial, margin, chip, flow, or valuation fields with `0`

Current `pe_ttm` interpretation:

- `pe_ttm` remains the official Tushare `daily_basic.pe_ttm` field
- Tushare may provide a valid `daily_basic` row with `pb/ps_ttm/...` populated while leaving `pe_ttm` empty
- in the completed `2026-06-05` staging rebuild, `pe_ttm` missing ratio was about `18.87%`
- most of those missing rows are associated with non-positive profit or return metrics, but not all of them
- therefore `pe_ttm` missing values must be preserved as `NaN` and treated as source-semantic missingness, not silently backfilled inside the canonical 49-field table

## Legacy 35-Field Mapping Notes

The old AmazingData-era 35-field table is kept here only as a translation aid while we migrate old readers. It is not the current rebuild contract.

- The active rebuild contract is the 49-field `research_daily` table documented above.
- Old fields such as `pre_close`, `pct_chg`, `amp`, `high_limited`, `low_limited`, `PE`, and `MARGIN_TRADE_BAL` are not part of the current phase-1 stock rebuild.
- When an old consumer still references one of those fields, it must be mapped explicitly from the new 49-field schema or from a bronze raw source table.

## New Canonical Fields

These fields should be added to the Tushare canonical dataset. They are not legacy AmazingData fields, but they are needed to make the rebuild auditable.

| Field | Meaning | Tushare source | Rule |
| --- | --- | --- | --- |
| `schema_version` | Dataset schema version | metadata | Use `tushare_v1`. |
| `source` | Data source | metadata | Use `tushare`. |
| `effective_target_date` | Last open trading day at or before cutoff | `trade_cal` | Single value shared by all stages. |
| `adj_factor` | Tushare adjustment factor | `adj_factor` | Required for active stocks on trading days with raw bars. |
| `hfq_open` | Backward-adjusted open | `daily.open * adj_factor` | Locally derived; required for research exports. |
| `hfq_high` | Backward-adjusted high | `daily.high * adj_factor` | Locally derived; required for research exports. |
| `hfq_low` | Backward-adjusted low | `daily.low * adj_factor` | Locally derived; required for research exports. |
| `hfq_close` | Backward-adjusted close | `daily.close * adj_factor` | Locally derived; required for research exports. |
| `up_limit` | Official upper limit price | `stk_limit.up_limit` | Raw-space audit and trading-constraint input. |
| `down_limit` | Official lower limit price | `stk_limit.down_limit` | Raw-space audit and trading-constraint input. |
| `limit_source_kind` | Limit-price source marker | production compatibility metadata | `official/structural_no_limit/missing/index`; required in production raw HDF and daily merge, not a factor feature. |
| `pe_ttm` | PE TTM | `daily_basic` | Store for research. |
| `ps_ttm` | PS TTM | `daily_basic` | Store for research. |
| `dv_ttm` | Dividend yield TTM | `daily_basic` | Store for research. |
| `turnover_rate_f` | Free-float turnover | `daily_basic` | Store for research. |
| `free_share` | Free-float shares | `daily_basic` | Store for research. |

## Research Source Tables

The rebuild should still preserve selected source tables in the bronze layer for audit, replay, and future feature expansion. But these are not separate downstream phase-1 research tables.

| Source table | Tushare source | Key | Stored fields | Used by `research_daily` |
| --- | --- | --- | --- | --- |
| `stk_limit_raw` | `stk_limit` | `(ts_code, trade_date)` | `trade_date`, `ts_code`, `pre_close`, `up_limit`, `down_limit` | `up_limit`, `down_limit`; source for production limit-trading booleans |
| `moneyflow_daily_raw` | `moneyflow` | `(ts_code, trade_date)` | all official moneyflow fields | `sm_net_vol`, `sm_net_amount`, `lg_net_vol`, `lg_net_amount`, `net_mf_vol`, `net_mf_amount` |
| `cyq_perf_raw` | `cyq_perf` | `(ts_code, trade_date)` | all official `cyq_perf` fields | `cost_15pct`, `cost_85pct`, `weight_avg` |
| `margin_detail_raw` | `margin_detail` | `(ts_code, trade_date)` | all official margin fields | `margin_buy_amount`, `margin_balance`, `short_balance` |

Rules:

- keep the raw source tables in bronze for traceability
- only the selected subset enters `research_daily`
- `margin_detail_raw` is retained for future research, not for the phase-1 core table

## Benchmark And Index Fields

Required benchmark indices:

- `000300.SH`
- `000905.SH`
- `000852.SH`

Recommended additional indices:

- `000001.SH`
- `399001.SZ`
- `399006.SZ`
- `000016.SH`

Fetch rule:

| Field | Tushare source | Source field |
| --- | --- | --- |
| `index_code` | `index_daily` | `ts_code` |
| `trade_date` | `index_daily` | `trade_date` |
| `open/high/low/close` | `index_daily` | same names |
| `volume` | `index_daily` | `vol` |
| `amount` | `index_daily` | `amount` |

Index quality is a hard gate for the three required benchmarks and a warning for the other recommended indices.

## Fetch Recipes

### 1. Calendar

Use `trade_cal(exchange="SSE", start_date, end_date, is_open="1")`.

Rules:

- Refresh calendar before every full rebuild and daily update.
- `effective_target_date` is the last open date `<= cutoff_date`.
- All stock, index, quality, Qlib, and QuantGPT outputs must use the same target date.

### 2. Universe

Use `stock_basic(list_status="L/P/D", fields=[...])` as three separate calls
and merge by `ts_code`.

Required fields:

```text
ts_code,symbol,name,market,exchange,list_date,delist_date,list_status
```

Rules:

- Full rebuild and daily rebuild download行情 only for `L ∪ P`.
- `D` rows are retained in `stock_basic` raw status snapshots and used for
  historical status backfill, but do not enter new行情 downloads.
- Name text containing `退市` must not override `list_status`; the official
  Tushare `stock_basic.list_status` remains the listing-status value.
- Do not expect rows before `list_date`.
- Store the universe snapshot used by the rebuild in the package manifest.

### 2b. ST Status

Use `stock_st(trade_date=YYYYMMDD, fields=[...])`.

Required fields:

```text
ts_code,name,trade_date,type,type_name
```

Rules:

- Pull `stock_st` for every trade date in the rebuild or refresh window.
- A `(ts_code, trade_date)` hit sets `st_status=ST`, unless the row is already
  `DELIST`.
- Name fallback is allowed only for `st_status`, not for `list_status`.
- Historical status backfill writes a staging package first and must not change
  production until the quality report is accepted and promoted.

### 3. Raw Daily Bars

Use `daily(trade_date=YYYYMMDD)` for full-market daily batches when possible. Use `daily(ts_code=..., start_date=..., end_date=...)` for repair and spot checks.

Required fields:

```text
ts_code,trade_date,open,high,low,close,vol,amount
```

Rules:

- Treat this as raw, unadjusted truth.
- Do not fill suspended days with synthetic bars.
- Validate `(ts_code, trade_date)` uniqueness.

### 3b. Official Limit Prices

Use `stk_limit(trade_date=YYYYMMDD)` for full-market daily batches.

Required fields:

```text
ts_code,trade_date,pre_close,up_limit,down_limit
```

Rules:

- Treat `up_limit/down_limit` as raw price-space official exchange-rule prices.
- Download and stage the table for every trading date in full rebuild and daily refresh windows.
- Join to stock daily rows by `(ts_code, trade_date)`; index rows may remain empty.
- A stock row with both limit prices missing must be marked `structural_no_limit` only when it is a genuine no-limit day such as listing day; all other missing stock rows are quality blockers.
- Do not replace missing official limit prices with hard-coded 10 percent, 20 percent, 30 percent, or ST bands in formal production backtests. Board/ST-derived bands are diagnostics only.

### 4. Adjusted Research Bars

Derive adjusted research bars locally from the two canonical Tushare facts:
raw `daily` OHLC and `adj_factor`.

Rules:

- Compute `hfq_open/high/low/close = daily.open/high/low/close * adj_factor`.
- Record `hfq_derivation.mode=local`, the formula, and `api_calls=0` in the
  rebuild manifest.
- Keep `daily` raw bars unchanged.
- Raw quality must verify OHLC/factor coverage and row alignment; it must not
  require per-code `pro_bar_hfq` files.
- Qlib and QuantGPT should consume `hfq_*` or a derived `adj_*` view, not raw `close`.

### 5. Adjustment Factor

Use `adj_factor(trade_date=YYYYMMDD)` for full-market batches or by stock for repair.

Required fields:

```text
ts_code,trade_date,adj_factor
```

Rules:

- Required whenever a raw daily bar exists for an active stock.
- Factor code set must match the requested active code set, after excluding legitimate non-trading/suspended rows.
- Factor integrity failure blocks the rebuild.

### 6. Daily Basic Indicators

Use `daily_basic(trade_date=YYYYMMDD)` by trading day.

Required fields:

```text
ts_code,trade_date,turnover_rate,turnover_rate_f,pe_ttm,pb,ps_ttm,dv_ttm,total_share,float_share,free_share,total_mv,circ_mv
```

Rules:

- This is the canonical source for PE/PB/share/market-cap/turnover fields.
- `circ_mv` is renamed to canonical `float_mv`.
- `total_share`, `float_share`, and `free_share` are in ten-thousand shares; `total_mv` and `circ_mv` are in ten-thousand CNY.

### 7. Financial PIT Fields

Use these interfaces by `ts_code` and date range:

- `income(ts_code=..., start_date=..., end_date=...)`
- `balancesheet(ts_code=..., start_date=..., end_date=...)`
- `fina_indicator(ts_code=..., start_date=..., end_date=...)`

Rules:

- Use `f_ann_date` when present; otherwise use `ann_date`.
- A report can only affect trade dates on or after its announcement date.
- Resolve duplicate periods by latest announcement/update flag.
- Keep `report_period`, `ann_date`, and `f_ann_date` in side columns or lineage tables.

### 8. Holder Number

Use `stk_holdernumber(ts_code=..., start_date=..., end_date=...)`.

Rules:

- PIT align by disclosure date.
- Carry the latest known `holder_num` forward after disclosure.
- Keep NaN before first known disclosure.

### 9. Margin Detail

Use `margin_detail(trade_date=YYYYMMDD)` or by date range for repair.

Rules:

- This is daily observation data only.
- Do not forward-fill missing margin rows.
- Store full official rows in `margin_detail_raw`.
- Merge only these three fields into `research_daily`: `margin_buy_amount`, `margin_balance`, `short_balance`.
- Missing rows are not base rebuild blockers.

### 10. Moneyflow

Use `moneyflow(trade_date=YYYYMMDD)` for daily full-market pulls and by `ts_code` for repairs.

Rules:

- Store full official rows in `moneyflow_daily_raw`.
- Merge only these six fields into `research_daily`: `sm_net_vol`, `sm_net_amount`, `lg_net_vol`, `lg_net_amount`, `net_mf_vol`, `net_mf_amount`.
- Validate uniqueness and date coverage, but treat old-history gaps as non-blocking research warnings unless they hit the current target window.
- Money-flow amount fields are in ten-thousand CNY, while `daily.amount` is in
  thousand CNY. For money-flow-to-turnover ratios in QuantGPT expressions, use
  `net_mf_amount * 10 / amount` rather than `net_mf_amount / amount`.

### 11. Chip Distribution

Use `cyq_perf(ts_code=..., start_date=..., end_date=...)`.

Rules:

- Store `cyq_perf` as a raw daily feature table keyed by `(ts_code, trade_date)`.
- Merge only `cost_15pct`, `cost_85pct`, and `weight_avg` into `research_daily`.
- Keep `research_daily` chip cost fields in Tushare raw price space. Downstream
  research-price exports must adjust them exactly once with `backward_factor`.
- Data starts later than raw market data, so historical absence before the source start is expected.

### 12. Derived VWAP

Tushare `daily` does not provide a direct VWAP field. FXAlpha derives VWAP only
inside official downstream conversions:

```text
raw_vwap = amount(thousand CNY) * 1000 / (volume(hand) * 100)
         = amount * 10 / volume
adjusted_vwap = raw_vwap * backward_factor
```

Rules:

- Do not store a synthetic `vwap` column in `research_daily`.
- QuantGPT stock parquet exports `vwap` as an adjusted research-price field.
- Qlib conversion derives raw VWAP in memory and writes canonical `$vwap` in adjusted price space.
- Factor evaluation, Rust bridge, and market-data fetchers must not derive
  fallback VWAP at runtime.

## Output Layout

The first Tushare full rebuild should write to a new staging package only:

```text
runtime/data_foundation/staging/tushare-fullrebuild-YYYYMMDD-target-YYYYMMDD/
  bronze/tushare_raw/
    daily/
    daily_basic/
    adj_factor/
    pro_bar_hfq/  # compatibility slot; empty when hfq_derivation.mode=local
    stock_basic/
    income/
    balancesheet/
    fina_indicator/
    holder_num/
    margin_detail/
    moneyflow/
    cyq_perf/
    index_daily/
  silver/
    canonical_daily.h5
    research_daily.h5
    index_daily.h5
    metadata.json
    manifest.json
    quality_report.json
  gold/
    qlib/
    quantgpt/
```

No production path is modified during rebuild.

## Daily Refresh Timing

Official page timing implies the daily rebuild should not treat all tables as simultaneously ready:

- `daily`: trading day 15:00 to 16:00
- `daily_basic`: trading day 15:00 to 17:00
- `cyq_perf`: around 18:00 to 19:00

Operational rule:

- if the daily routine wants a complete `research_daily` including chip fields, schedule the final assembly after 19:00 local market time
- if an earlier run is needed for staging, mark chip fields as pending instead of silently treating them as missing

## Acceptance Gates

Base dataset blockers:

- Calendar target date is consistent across all stages.
- Active universe snapshot is present.
- Raw daily bars pass uniqueness, non-null, price sanity, and coverage checks.
- `daily_basic` exists for active daily rows with no coverage gap versus `daily`.
- `adj_factor` exists and passes code-set integrity.
- `hfq_*` research prices exist for active rows targeted by Qlib/QuantGPT.
- `research_daily` is generated successfully with the documented 49-field schema.
- `research_daily` contains both raw price columns and backward-adjusted price columns.
- `research_daily` keeps source-unit metadata for `amount`, `*_mv`, `*_share`, moneyflow amount fields, and margin amount fields.
- PIT financial fields in `research_daily` have no future leakage.
- Required benchmark indices reach the target date.

Warnings, not first-pass blockers:

- Financing/margin missing for ineligible stocks.
- Chip data missing before the source start date.
- Moneyflow history gaps before the source-supported start date.
- Delisted stocks excluded by first rebuild policy.
