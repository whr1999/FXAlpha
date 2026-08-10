from __future__ import annotations

from functools import lru_cache
from typing import Any

import pandas as pd

from storage.paths import (
    MODEL_DEFAULT_END_DATE,
    MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    MODEL_DEFAULT_FORWARD_PERIOD,
    MODEL_DEFAULT_N_DROP,
    MODEL_DEFAULT_TOPK,
    MODEL_DEFAULT_SELECTION_CUTOFF,
    MODEL_DEFAULT_START_DATE,
    MODEL_DEFAULT_TEST_MONTHS,
    MODEL_DEFAULT_VALID_MONTHS,
    QLIB_CALENDAR_FILE,
)


@lru_cache(maxsize=1)
def load_model_trading_calendar() -> pd.DatetimeIndex:
    if QLIB_CALENDAR_FILE.exists():
        dates: list[pd.Timestamp] = []
        for raw_line in QLIB_CALENDAR_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                dates.append(pd.Timestamp(line).normalize())
            except Exception:
                continue
        if dates:
            return pd.DatetimeIndex(dates).sort_values()
    today = pd.Timestamp.today().normalize()
    start = pd.Timestamp(MODEL_DEFAULT_START_DATE).normalize()
    return pd.bdate_range(start, today)


def latest_trading_day() -> str:
    calendar = load_model_trading_calendar()
    if len(calendar) == 0:
        return pd.Timestamp.today().normalize().strftime("%Y-%m-%d")
    return calendar[-1].strftime("%Y-%m-%d")


def resolve_trading_day_on_or_before(date_like: Any) -> str:
    calendar = load_model_trading_calendar()
    if len(calendar) == 0:
        return pd.Timestamp(date_like).normalize().strftime("%Y-%m-%d")
    target = pd.Timestamp(date_like).normalize()
    pos = int(calendar.searchsorted(target, side="right")) - 1
    if pos < 0:
        return calendar[0].strftime("%Y-%m-%d")
    return calendar[min(pos, len(calendar) - 1)].strftime("%Y-%m-%d")


def resolve_model_end_date(end_date: str | None = None) -> str:
    target = end_date or MODEL_DEFAULT_END_DATE or latest_trading_day()
    return resolve_trading_day_on_or_before(target)


def derive_unpurged_model_windows(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    valid_months: int | None = None,
    test_months: int | None = None,
) -> dict[str, str]:
    resolved_start = pd.Timestamp(start_date or MODEL_DEFAULT_START_DATE).normalize()
    resolved_end = pd.Timestamp(resolve_model_end_date(end_date))
    valid_months = int(valid_months or MODEL_DEFAULT_VALID_MONTHS)
    test_months = int(test_months or MODEL_DEFAULT_TEST_MONTHS)

    test_start = (resolved_end.replace(day=1) - pd.DateOffset(months=max(test_months - 1, 0))).normalize()
    valid_start = (test_start - pd.DateOffset(months=max(valid_months, 0))).normalize()
    train_end = (valid_start - pd.Timedelta(days=1)).normalize()
    valid_end = (test_start - pd.Timedelta(days=1)).normalize()

    return {
        "train_start": resolved_start.strftime("%Y-%m-%d"),
        "train_end": train_end.strftime("%Y-%m-%d"),
        "valid_start": valid_start.strftime("%Y-%m-%d"),
        "valid_end": valid_end.strftime("%Y-%m-%d"),
        "test_start": test_start.strftime("%Y-%m-%d"),
        "test_end": resolved_end.strftime("%Y-%m-%d"),
    }


def resolve_model_selection_cutoff(selection_cutoff: str | None = None) -> str:
    if selection_cutoff:
        return resolve_trading_day_on_or_before(selection_cutoff)
    if MODEL_DEFAULT_SELECTION_CUTOFF:
        return resolve_trading_day_on_or_before(MODEL_DEFAULT_SELECTION_CUTOFF)
    return resolve_trading_day_on_or_before(
        derive_unpurged_model_windows()["valid_end"]
    )


def model_evaluation_defaults() -> dict[str, Any]:
    windows = derive_unpurged_model_windows()
    return {
        "config_source": "config.yaml:model",
        "start_date": MODEL_DEFAULT_START_DATE,
        "target_end_date": MODEL_DEFAULT_END_DATE or latest_trading_day(),
        "resolved_end_date": windows["test_end"],
        "selection_cutoff": resolve_model_selection_cutoff(),
        "valid_months": MODEL_DEFAULT_VALID_MONTHS,
        "test_months": MODEL_DEFAULT_TEST_MONTHS,
        "label_forward_period": MODEL_DEFAULT_FORWARD_PERIOD,
        "factor_holding_period_days": MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
        "portfolio": {
            "topk": MODEL_DEFAULT_TOPK,
            "n_drop": MODEL_DEFAULT_N_DROP,
        },
        "segments": windows,
        "notes": [
            "end_date and selection_cutoff are aligned to the latest trading day on or before the configured natural date.",
            "actual Qlib fit_end and valid_end are further purged by label horizon inside the model/Qlib runner.",
        ],
    }
