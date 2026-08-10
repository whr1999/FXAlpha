from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from domain.data_foundation.stock_metadata import load_stock_identity_map

AVG_TOP50_THRESHOLD = 0.05
P95_TOP50_THRESHOLD = 0.15
TOP_N = 50


def is_st_like_name(name: str | None) -> bool:
    text = str(name or "").strip().upper()
    return text.startswith("ST") or text.startswith("*ST") or "退市" in text


def _threshold_reason_suffix(value: float) -> str:
    return str(value).replace(".", "_").rstrip("0").rstrip("_")


def candidate_flipped_low_side(candidate: dict[str, Any]) -> bool:
    for value in (
        candidate.get("flipped"),
        (candidate.get("key_metrics") or {}).get("flipped") if isinstance(candidate.get("key_metrics"), dict) else None,
        (candidate.get("best_long_only_group_metrics") or {}).get("selected_group_is_flipped_low_side")
        if isinstance(candidate.get("best_long_only_group_metrics"), dict)
        else None,
        (candidate.get("backtest_summary") or {}).get("flipped") if isinstance(candidate.get("backtest_summary"), dict) else None,
        (candidate.get("params") or {}).get("flipped") if isinstance(candidate.get("params"), dict) else None,
    ):
        if value is not None:
            return bool(value)
    return False


def st_exposure_unavailable(reason: str, *, top_n: int = TOP_N) -> dict[str, Any]:
    return {
        "available": False,
        "passed": False,
        "reason": reason,
        "avg_top50_ratio": None,
        "p95_top50_ratio": None,
        "latest_top50_ratio": None,
        "sample_days": 0,
        "top_n": int(top_n),
        "thresholds": {
            "avg_top50_ratio": AVG_TOP50_THRESHOLD,
            "p95_top50_ratio": P95_TOP50_THRESHOLD,
        },
    }


def evaluate_st_exposure_from_factor_values(
    factor_values: pd.Series | pd.DataFrame,
    *,
    flipped_low_side: bool = False,
    name_map: dict[str, str] | None = None,
    top_n: int = TOP_N,
) -> dict[str, Any]:
    frame = _normalize_factor_values(factor_values)
    if frame.empty:
        return st_exposure_unavailable("st_exposure_factor_values_unavailable", top_n=top_n)
    mapping = name_map if name_map is not None else load_stock_identity_map()
    if not mapping:
        return st_exposure_unavailable("stock_identity_map_unavailable", top_n=top_n)

    frame["factor_value"] = pd.to_numeric(frame["factor_value"], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "stock_code", "factor_value"]).copy()
    if frame.empty:
        return st_exposure_unavailable("st_exposure_factor_values_unavailable", top_n=top_n)

    daily_rows: list[dict[str, Any]] = []
    hit_counts: dict[str, dict[str, Any]] = {}
    ascending = bool(flipped_low_side)
    for trade_date, day in frame.groupby("trade_date", sort=True):
        ranked = day.sort_values("factor_value", ascending=ascending).head(int(top_n)).copy()
        if ranked.empty:
            continue
        st_hits = []
        for _, row in ranked.iterrows():
            stock_code = str(row.get("stock_code") or "")
            security_name = _security_name(stock_code, mapping)
            if is_st_like_name(security_name):
                st_hits.append({"stock_code": stock_code, "security_name": security_name})
                entry = hit_counts.setdefault(
                    stock_code,
                    {"stock_code": stock_code, "security_name": security_name, "hit_days": 0},
                )
                entry["hit_days"] += 1
        ratio = len(st_hits) / max(len(ranked), 1)
        daily_rows.append(
            {
                "trade_date": str(pd.Timestamp(trade_date).date()),
                "top_count": int(len(ranked)),
                "st_count": int(len(st_hits)),
                "top50_ratio": round(float(ratio), 4),
                "st_hits": st_hits[:10],
            }
        )

    if not daily_rows:
        return st_exposure_unavailable("st_exposure_top50_unavailable", top_n=top_n)

    ratios = np.array([float(row["top50_ratio"]) for row in daily_rows], dtype=float)
    avg_ratio = float(np.mean(ratios))
    p95_ratio = float(np.percentile(ratios, 95))
    latest = daily_rows[-1]
    triggered = []
    if avg_ratio >= AVG_TOP50_THRESHOLD:
        triggered.append(f"avg_top50_ratio_ge_{_threshold_reason_suffix(AVG_TOP50_THRESHOLD)}")
    if p95_ratio >= P95_TOP50_THRESHOLD:
        triggered.append(f"p95_top50_ratio_ge_{_threshold_reason_suffix(P95_TOP50_THRESHOLD)}")
    passed = not triggered
    top_hits = sorted(hit_counts.values(), key=lambda item: (-int(item["hit_days"]), item["stock_code"]))[:20]
    return {
        "available": True,
        "passed": passed,
        "reason": "st_exposure_passed" if passed else "st_exposure_veto:" + ",".join(triggered),
        "avg_top50_ratio": round(avg_ratio, 4),
        "p95_top50_ratio": round(p95_ratio, 4),
        "latest_top50_ratio": latest["top50_ratio"],
        "latest_trade_date": latest["trade_date"],
        "sample_days": int(len(daily_rows)),
        "top_n": int(top_n),
        "long_only_side": "low_factor_values" if flipped_low_side else "high_factor_values",
        "selected_group_is_flipped_low_side": bool(flipped_low_side),
        "thresholds": {
            "avg_top50_ratio": AVG_TOP50_THRESHOLD,
            "p95_top50_ratio": P95_TOP50_THRESHOLD,
        },
        "top_st_hits": top_hits,
        "latest_st_hits": latest["st_hits"],
    }


def combined_novelty_st_guard(candidate: dict[str, Any]) -> dict[str, Any]:
    novelty = candidate.get("novelty_guard") if isinstance(candidate.get("novelty_guard"), dict) else {}
    st_guard = candidate.get("st_exposure_guard") if isinstance(candidate.get("st_exposure_guard"), dict) else {}
    novelty_allowed = novelty.get("allowed") is True
    st_passed = st_guard.get("passed") is True
    st_mode = str(st_guard.get("mode") or "hard").strip().lower()
    st_is_advisory = st_mode == "advisory"
    if novelty_allowed and st_passed:
        return {
            "allowed": True,
            "reason": "novelty_and_st_exposure_passed",
            "novelty_allowed": True,
            "st_exposure_passed": True,
            "st_exposure_mode": st_mode,
        }
    if novelty_allowed and st_is_advisory:
        return {
            "allowed": True,
            "reason": "novelty_passed_st_exposure_advisory",
            "novelty_allowed": True,
            "st_exposure_passed": False,
            "st_exposure_mode": "advisory",
            "advisory_tags": ["distress_proxy_exposure"],
        }
    if novelty_allowed:
        return {
            "allowed": False,
            "reason": st_guard.get("reason") or "st_exposure_guard_unavailable",
            "novelty_allowed": True,
            "st_exposure_passed": False,
            "st_exposure_mode": st_mode,
        }
    return {
        "allowed": False,
        "reason": novelty.get("reason") or "novelty_not_allowed",
        "novelty_allowed": False,
        "st_exposure_passed": st_passed if st_guard else None,
        "st_exposure_mode": st_mode if st_guard else None,
    }


def _normalize_factor_values(factor_values: pd.Series | pd.DataFrame) -> pd.DataFrame:
    if isinstance(factor_values, pd.Series):
        frame = factor_values.rename("factor_value").reset_index()
    else:
        frame = factor_values.copy()
        if isinstance(frame.index, pd.MultiIndex) or frame.index.name is not None:
            frame = frame.reset_index()
        if "factor_value" not in frame.columns:
            value_cols = [col for col in frame.columns if col not in {"trade_date", "stock_code"}]
            if len(value_cols) == 1:
                frame = frame.rename(columns={value_cols[0]: "factor_value"})
    required = {"trade_date", "stock_code", "factor_value"}
    if not required.issubset(frame.columns):
        return pd.DataFrame(columns=["trade_date", "stock_code", "factor_value"])
    return frame[["trade_date", "stock_code", "factor_value"]].copy()


def _security_name(stock_code: str, name_map: dict[str, str]) -> str:
    raw = str(stock_code or "").strip()
    if raw in name_map:
        return str(name_map.get(raw) or "")
    lower = raw.lower()
    if lower.startswith(("sh.", "sz.")):
        market, code = lower.split(".", 1)
        market_code = f"{code.zfill(6)}.{market.upper()}"
        return str(name_map.get(market_code) or name_map.get(f"{code.zfill(6)}{market}") or "")
    if lower.endswith("sz") or lower.endswith("sh"):
        market_code = f"{lower[:6]}.{lower[-2:].upper()}"
        return str(name_map.get(market_code) or name_map.get(lower) or "")
    return str(name_map.get(raw.upper()) or "")
