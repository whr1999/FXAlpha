from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


DEFAULT_LIMIT_PRICE_TOLERANCE = 1e-4
DEFAULT_SEALED_TURNOVER_RATIO_THRESHOLD = 0.0005


@dataclass(frozen=True)
class LimitExecutionColumns:
    open: str
    high: str
    low: str
    close: str
    up_limit: str
    down_limit: str
    turnover_rate: str | None = None


def _numeric(frame: pd.DataFrame, column: str | None) -> pd.Series:
    if column is None or column not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce")


def _valid_nonnegative(series: pd.Series) -> pd.Series:
    out = pd.to_numeric(series, errors="coerce")
    return out.where(np.isfinite(out) & out.ge(0))


def turnover_rate_ratio(frame: pd.DataFrame, columns: LimitExecutionColumns) -> pd.Series:
    """Return Tushare daily turnover rate as a dimensionless ratio.

    Tushare stores ``turnover_rate`` as percent.  The sealed-limit execution
    rule intentionally does not fall back to absolute volume or amount fields;
    if turnover rate is missing, the row is not marked sealed by this rule and
    the missing coverage should be handled by data quality diagnostics.
    """

    return _valid_nonnegative(_numeric(frame, columns.turnover_rate)) / 100.0


def open_sealed_limit_fields(
    frame: pd.DataFrame,
    columns: LimitExecutionColumns,
    *,
    official_mask: pd.Series | None = None,
    price_tolerance: float = DEFAULT_LIMIT_PRICE_TOLERANCE,
    turnover_ratio_threshold: float = DEFAULT_SEALED_TURNOVER_RATIO_THRESHOLD,
) -> dict[str, pd.Series]:
    """Compute open-execution sealed-limit fields in raw price space.

    ``limit_buy_open`` means the open price touched the official upper limit.
    It is not enough to reject a buy.  The production no-trade flag is stricter:
    the whole daily OHLC must be pinned to the official limit price and the
    turnover rate must be tiny.  Absolute amount/volume is intentionally not
    used here.
    """

    open_price = _numeric(frame, columns.open)
    high = _numeric(frame, columns.high)
    low = _numeric(frame, columns.low)
    close = _numeric(frame, columns.close)
    up_limit = _numeric(frame, columns.up_limit)
    down_limit = _numeric(frame, columns.down_limit)
    if official_mask is None:
        official = up_limit.notna() & down_limit.notna()
    else:
        official = pd.Series(official_mask, index=frame.index).fillna(False).astype(bool)

    one_price_up = (
        open_price.sub(up_limit).abs().le(price_tolerance)
        & high.sub(up_limit).abs().le(price_tolerance)
        & low.sub(up_limit).abs().le(price_tolerance)
        & close.sub(up_limit).abs().le(price_tolerance)
    ).fillna(False) & official

    one_price_down = (
        open_price.sub(down_limit).abs().le(price_tolerance)
        & high.sub(down_limit).abs().le(price_tolerance)
        & low.sub(down_limit).abs().le(price_tolerance)
        & close.sub(down_limit).abs().le(price_tolerance)
    ).fillna(False) & official

    turnover_ratio = turnover_rate_ratio(frame, columns)
    low_liquidity = turnover_ratio.notna() & turnover_ratio.le(float(turnover_ratio_threshold))

    return {
        "one_price_up_limit": one_price_up.astype("float32"),
        "one_price_down_limit": one_price_down.astype("float32"),
        "limit_turnover_ratio": turnover_ratio.astype("float32"),
        "limit_low_liquidity": low_liquidity.astype("float32"),
        "limit_buy_open_sealed": (one_price_up & low_liquidity).astype("float32"),
        "limit_sell_open_sealed": (one_price_down & low_liquidity).astype("float32"),
    }
