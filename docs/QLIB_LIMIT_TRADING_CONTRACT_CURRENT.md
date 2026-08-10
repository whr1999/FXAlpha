# FXAlpha Qlib Limit Trading Contract

Updated: 2026-06-26

This document defines the production Qlib trading semantics for limit-up and
limit-down handling.

## Official Qlib Semantics

Primary implementation reference:

- https://github.com/microsoft/qlib/blob/main/qlib/backtest/exchange.py

Qlib `Exchange` supports:

- `deal_price` as a string expression, such as `close`, `$close`, `$open`, or `($open+$close)/2`
- `deal_price` as a two-item buy/sell price list
- `limit_threshold=None`
- `limit_threshold=<float>`, interpreted against `$change`
- `limit_threshold=[buy_limit_expr, sell_limit_expr]`

In expression mode, Qlib converts the two expressions into internal
`limit_buy` and `limit_sell` booleans. A true value means that direction is
limited and not tradable. Qlib then rejects an order when the stock is suspended
or the direction-specific limit boolean is true.

Qlib's CN region default is `limit_threshold=0.095` and `deal_price=close`.
That default is only a broad convenience. It is not precise enough for FXAlpha
production A-share backtests because main-board, STAR/ChiNext, Beijing exchange,
ST, no-limit listing days, and official rounding rules differ.

## FXAlpha Production Rule

Production uses Tushare `stk_limit` official prices as the trading constraint
source.

Raw data:

- `up_limit` and `down_limit` stay in raw price space in raw HDF.
- `limit_source_kind` is `official`, `structural_no_limit`, `missing`, or
  `index`.
- Non-index stock rows with `limit_source_kind=missing` fail the quality gate.

Qlib export:

- Canonical Qlib OHLC fields are adjusted prices.
- `up_limit/down_limit` are exported into the same adjusted price space for
  audit consistency.
- Raw Tushare limit prices are retained as `$raw_up_limit/$raw_down_limit`
  for audits; they must not be mixed with adjusted `$open/$close` in Qlib
  expressions.
- Boolean limit fields are generated before conversion from official prices and
  then carried into Qlib:
  - `$limit_buy`
  - `$limit_sell`
  - `$limit_buy_open`
  - `$limit_sell_open`
  - `$one_price_up_limit`
  - `$one_price_down_limit`
  - `$limit_turnover_ratio`
  - `$limit_low_liquidity`
  - `$limit_buy_open_sealed`
  - `$limit_sell_open_sealed`
  - `$limit_buy_mid_oc`
  - `$limit_sell_mid_oc`
  - `$limit_source_official`
  - `$limit_source_no_limit`
  - `$hit_up_limit_intraday`
  - `$hit_down_limit_intraday`

Formal model backtests keep the production default:

```yaml
exchange_kwargs:
  deal_price: open
  limit_threshold: ["$limit_buy_open_sealed", "$limit_sell_open_sealed"]
```

The fields mean:

- `$limit_buy_open = open >= up_limit - tick_tol`
- `$limit_sell_open = open <= down_limit + tick_tol`
- `$one_price_up_limit/$one_price_down_limit` require raw open/high/low/close
  to equal the official limit price within tick tolerance.
- `$limit_turnover_ratio` is Tushare `turnover_rate / 100`; it never falls back
  to absolute volume or amount fields.
- `$limit_buy_open_sealed/$limit_sell_open_sealed` are the formal Qlib
  no-trade fields: one-price limit plus tiny turnover rate.

The open touch fields are diagnostics.  They do not prove the order cannot be
filled when the board opens during the day or has meaningful turnover rate.

`$hit_up_limit_intraday` and `$hit_down_limit_intraday` are diagnostics only.
They must not be used as Qlib `limit_threshold` fields. A stock touching
`high == up_limit` intraday does not prove the open-price trade is impossible.

## Deal Price Variants

When the deal-price assumption changes, the limit fields must change with it.

| Deal price | Qlib expression | Required limit threshold |
| --- | --- | --- |
| `close` | `$close` | `["$limit_buy", "$limit_sell"]` |
| `open` | `$open` | `["$limit_buy_open_sealed", "$limit_sell_open_sealed"]` |
| `mid_oc` | `($open+$close)/2` | `["$limit_buy_mid_oc", "$limit_sell_mid_oc"]` |

`mid_oc` is a diagnostic execution assumption only. It approximates an average
of the opening and closing print and is not a volume-weighted or fill-probability
model. It must not be used to claim intraday tradability. It is useful for
stress-testing whether close-only execution is over-sensitive.

## Future Function Rules

- The limit decision for date `t` may use only date-`t` official limit prices and
  date-`t` execution price fields.
- Formal open-price model qrun must use native
  `qlib.workflow.record_temp.PortAnaRecord`. Qlib `TopkDropoutStrategy`
  already reads the prior trading step's signal with `shift=1`; pre-shifting
  `pred.pkl` creates double-lagged signals and is not allowed.
  date-`t` chosen deal price.
- Feature-set labels still use forward adjusted returns and remain separate
  from trading execution fields.
- `up_limit/down_limit`, `limit_source_kind`, and limit booleans are audit and
  execution metadata, not alpha features.

## Verification

Required checks after a data rebuild or trading-code change:

```bash
python3 -m pytest -q tests/test_tushare_limit_backfill.py tests/test_tushare_rebuild.py tests/test_tushare_daily.py tests/test_qlib_incremental_missing_instrument.py tests/test_qlib_trading_strategy.py tests/test_qlib_paper_execution.py
python3 cli.py data-status
python3 cli.py data-production-audit --full-scan
```

Sample Qlib provider checks must show:

- `data/qlib/stock_converter_meta.json.valid_field_count >= 52`
- `limit_buy_mid_oc.day.bin` and `limit_sell_mid_oc.day.bin` exist for stock
  instruments
- Qlib API can read `$limit_buy`, `$limit_sell`, `$limit_buy_mid_oc`,
  `$limit_sell_mid_oc`, `$limit_source_official`, and `$limit_source_no_limit`
