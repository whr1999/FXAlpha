from __future__ import annotations

import pandas as pd

from domain.factor_research.st_exposure_guard import (
    evaluate_st_exposure_from_factor_values,
    is_st_like_name,
)


def _factor_frame(values_by_day: list[list[float]]) -> pd.DataFrame:
    rows = []
    for day_idx, values in enumerate(values_by_day, start=1):
        stocks = [f"{idx:06d}.SZ" for idx in range(1, len(values) + 1)]
        for stock, value in zip(stocks, values):
            rows.append({"trade_date": f"2024-01-{day_idx:02d}", "stock_code": stock, "factor_value": value})
    return pd.DataFrame(rows)


def test_is_st_like_name_matches_st_star_st_and_delisting():
    assert is_st_like_name("ST_MINGTIAN")
    assert is_st_like_name("*ST_MINGTIAN")
    assert is_st_like_name("\u9000\u5e02_MINGTIAN")
    assert not is_st_like_name("NORMAL_SHARE")


def test_st_guard_blocks_avg_top50_threshold_at_exact_5_percent():
    rows = []
    name_map = {}
    for day_idx in range(1, 21):
        st_count = 2 if day_idx <= 10 else 3
        for idx in range(1, 51):
            stock = f"{day_idx:02d}{idx:04d}.SZ"
            rows.append({"trade_date": f"2024-01-{day_idx:02d}", "stock_code": stock, "factor_value": idx})
            if idx > 50 - st_count:
                name_map[stock] = "*ST_HIGH"
    frame = pd.DataFrame(rows)

    guard = evaluate_st_exposure_from_factor_values(frame, name_map=name_map)

    assert guard["passed"] is False
    assert guard["avg_top50_ratio"] == 0.05
    assert "avg_top50_ratio_ge_0_05" in guard["reason"]


def test_st_guard_resolves_qlib_style_stock_codes():
    rows = []
    for day_idx in range(1, 11):
        for idx in range(1, 51):
            rows.append(
                {
                    "trade_date": f"2024-01-{day_idx:02d}",
                    "stock_code": f"sz.{idx:06d}",
                    "factor_value": idx,
                }
            )
    frame = pd.DataFrame(rows)
    name_map = {f"{idx:06d}.SZ": "*ST_HIGH" for idx in range(46, 51)}

    guard = evaluate_st_exposure_from_factor_values(frame, name_map=name_map)

    assert guard["passed"] is False
    assert guard["avg_top50_ratio"] == 0.1
    assert guard["top_st_hits"][0]["stock_code"] == "sz.000046"


def test_st_guard_blocks_p95_top50_threshold_at_15_percent():
    values = [list(range(50)) for _ in range(20)]
    frame = _factor_frame(values)
    name_map = {f"{idx:06d}.SZ": "*ST_HIGH" for idx in range(43, 51)}
    guard = evaluate_st_exposure_from_factor_values(frame, name_map=name_map)

    assert guard["passed"] is False
    assert guard["p95_top50_ratio"] == 0.16
    assert "p95_top50_ratio_ge_0_15" in guard["reason"]


def test_st_guard_uses_low_factor_side_when_flipped():
    frame = _factor_frame([list(range(100)) for _ in range(5)])
    name_map = {f"{idx:06d}.SZ": "*ST_LOW" for idx in range(1, 6)}

    high_side = evaluate_st_exposure_from_factor_values(frame, name_map=name_map, flipped_low_side=False)
    low_side = evaluate_st_exposure_from_factor_values(frame, name_map=name_map, flipped_low_side=True)

    assert high_side["passed"] is True
    assert low_side["passed"] is False
    assert low_side["long_only_side"] == "low_factor_values"
