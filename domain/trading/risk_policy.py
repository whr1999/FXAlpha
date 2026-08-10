from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from storage.paths import (
    QUANTGPT_BENCHMARK_DIR,
    TRADING_RISK_LATEST_FILE,
    TRADING_RISK_POLICY_CONFIG_FILE,
)


RISK_POLICY_VERSION = "market_resonance_account_brake_v1"
BENCHMARK_FILES = {
    "hs300": "benchmark_hs300.parquet",
    "csi500": "benchmark_csi500.parquet",
    "csi1000": "benchmark_csi1000.parquet",
}


def default_risk_policy_config() -> dict[str, Any]:
    return {
        "version": RISK_POLICY_VERSION,
        "enabled": True,
        "mode": "enforced",
        "market": {
            "short_window": 20,
            "long_window": 60,
            "annualization": 238,
            "breadth_threshold": 1.0 / 3.0,
            "volatility_threshold": 0.18,
            "stress_cap": 0.75,
            "enter_days": 2,
            "exit_days": 3,
        },
        "account": {
            "drawdown_window": 60,
            "drawdown_threshold": 0.08,
            "brake_cap": 0.50,
        },
    }


def _deep_merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def normalize_risk_policy_config(value: dict[str, Any] | None) -> dict[str, Any]:
    merged = _deep_merge(default_risk_policy_config(), dict(value or {}))
    mode = str(merged.get("mode") or "enforced").strip().lower()
    if mode not in {"enforced", "shadow"}:
        raise ValueError("risk_policy_mode_must_be_enforced_or_shadow")
    config = {
        "version": RISK_POLICY_VERSION,
        "enabled": bool(merged.get("enabled", True)),
        "mode": mode,
    }
    market_source = dict(merged.get("market") or {})
    account_source = dict(merged.get("account") or {})
    market = {key: market_source[key] for key in default_risk_policy_config()["market"]}
    account = {key: account_source[key] for key in default_risk_policy_config()["account"]}
    market["short_window"] = int(market["short_window"])
    market["long_window"] = int(market["long_window"])
    market["annualization"] = int(market["annualization"])
    market["breadth_threshold"] = float(market["breadth_threshold"])
    market["volatility_threshold"] = float(market["volatility_threshold"])
    market["stress_cap"] = float(market["stress_cap"])
    market["enter_days"] = int(market["enter_days"])
    market["exit_days"] = int(market["exit_days"])
    account["drawdown_window"] = int(account["drawdown_window"])
    account["drawdown_threshold"] = float(account["drawdown_threshold"])
    account["brake_cap"] = float(account["brake_cap"])

    if not 5 <= market["short_window"] <= 60:
        raise ValueError("risk_policy_short_window_out_of_range:5..60")
    if not market["short_window"] < market["long_window"] <= 252:
        raise ValueError("risk_policy_long_window_out_of_range:short+1..252")
    if not 200 <= market["annualization"] <= 366:
        raise ValueError("risk_policy_annualization_out_of_range:200..366")
    if not 0.0 <= market["breadth_threshold"] <= 1.0:
        raise ValueError("risk_policy_breadth_threshold_out_of_range:0..1")
    if not 0.05 <= market["volatility_threshold"] <= 0.80:
        raise ValueError("risk_policy_volatility_threshold_out_of_range:0.05..0.80")
    if not 0.25 <= market["stress_cap"] <= 1.0:
        raise ValueError("risk_policy_stress_cap_out_of_range:0.25..1")
    if not 1 <= market["enter_days"] <= 10 or not 1 <= market["exit_days"] <= 20:
        raise ValueError("risk_policy_confirmation_days_out_of_range")
    if not 20 <= account["drawdown_window"] <= 252:
        raise ValueError("risk_policy_drawdown_window_out_of_range:20..252")
    if not 0.02 <= account["drawdown_threshold"] <= 0.30:
        raise ValueError("risk_policy_drawdown_threshold_out_of_range:0.02..0.30")
    if not 0.20 <= account["brake_cap"] <= market["stress_cap"]:
        raise ValueError("risk_policy_brake_cap_out_of_range:0.20..stress_cap")

    config["market"] = market
    config["account"] = account
    return config


def risk_policy_config_hash(config: dict[str, Any] | None = None) -> str:
    normalized = normalize_risk_policy_config(config)
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]


def load_risk_policy_config(path: Path | None = None) -> dict[str, Any]:
    path = path or TRADING_RISK_POLICY_CONFIG_FILE
    if not path.exists():
        return normalize_risk_policy_config(None)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return normalize_risk_policy_config(payload)


def save_risk_policy_config(value: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    path = path or TRADING_RISK_POLICY_CONFIG_FILE
    config = normalize_risk_policy_config(value)
    payload = {
        **config,
        "config_hash": risk_policy_config_hash(config),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return payload


def update_risk_policy_config(changes: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    current = load_risk_policy_config(path)
    allowed = {key: value for key, value in dict(changes or {}).items() if key in {"enabled", "mode", "market", "account"}}
    return save_risk_policy_config(_deep_merge(current, allowed), path)


def _benchmark_close_frame(as_of_date: str, benchmark_dir: Path | None = None) -> pd.DataFrame:
    root = benchmark_dir or QUANTGPT_BENCHMARK_DIR
    series: dict[str, pd.Series] = {}
    target = pd.Timestamp(as_of_date).normalize()
    for name, filename in BENCHMARK_FILES.items():
        path = root / filename
        if not path.exists():
            raise RuntimeError(f"risk_market_benchmark_missing:{path}")
        frame = pd.read_parquet(path, columns=["trade_date", "close"])
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        close = frame.dropna(subset=["trade_date", "close"]).drop_duplicates("trade_date", keep="last")
        close = close.loc[(close["trade_date"] <= target) & (close["close"] > 0)].set_index("trade_date")["close"]
        series[name] = close.sort_index()
    closes = pd.DataFrame(series).dropna().sort_index()
    if closes.empty or closes.index[-1] != target:
        latest = str(closes.index[-1].date()) if not closes.empty else "none"
        raise RuntimeError(f"risk_market_data_not_ready:signal_date={as_of_date}:latest_common={latest}")
    return closes


def _confirmed_stress(raw: pd.Series, enter_days: int, exit_days: int) -> pd.Series:
    active = False
    stress_streak = 0
    recovery_streak = 0
    values: list[bool] = []
    for raw_value in raw.fillna(False).astype(bool):
        if raw_value:
            stress_streak += 1
            recovery_streak = 0
            if not active and stress_streak >= enter_days:
                active = True
        else:
            recovery_streak += 1
            stress_streak = 0
            if active and recovery_streak >= exit_days:
                active = False
        values.append(active)
    return pd.Series(values, index=raw.index, dtype=bool)


def _latest_streak(values: pd.Series, expected: bool) -> int:
    count = 0
    for value in reversed(values.fillna(False).astype(bool).tolist()):
        if value != expected:
            break
        count += 1
    return count


def _market_feature_frame(
    signal_date: str,
    config: dict[str, Any],
    benchmark_dir: Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    market = config["market"]
    short_window = int(market["short_window"])
    long_window = int(market["long_window"])
    closes = _benchmark_close_frame(signal_date, benchmark_dir)
    if len(closes) < long_window + 1:
        raise RuntimeError(f"risk_market_history_insufficient:{len(closes)}<{long_window + 1}")
    log_close = closes.map(math.log)
    short_returns = log_close - log_close.shift(short_window)
    long_returns = log_close - log_close.shift(long_window)
    breadth_short = short_returns.gt(0).mean(axis=1)
    breadth_long = long_returns.gt(0).mean(axis=1)
    composite_return = log_close.diff().mean(axis=1)
    vol_short = composite_return.rolling(short_window).std() * math.sqrt(float(market["annualization"]))
    vol_long = composite_return.rolling(long_window).std() * math.sqrt(float(market["annualization"]))
    risk_vol = pd.concat([vol_short, vol_long], axis=1).max(axis=1)
    raw = (
        (breadth_short <= float(market["breadth_threshold"]) + 1e-12)
        & (breadth_long <= float(market["breadth_threshold"]) + 1e-12)
        & (risk_vol >= float(market["volatility_threshold"]))
    )
    confirmed = _confirmed_stress(raw, int(market["enter_days"]), int(market["exit_days"]))
    features = pd.DataFrame(
        {
            "breadth_short": breadth_short,
            "breadth_long": breadth_long,
            "volatility_short": vol_short,
            "volatility_long": vol_long,
            "risk_volatility": risk_vol,
            "raw_stress": raw,
            "market_stress": confirmed,
        }
    )
    features["market_cap"] = confirmed.map(
        lambda active: float(market["stress_cap"] if active else 1.0)
    )
    return features, closes


def _market_decision(signal_date: str, config: dict[str, Any], benchmark_dir: Path | None = None) -> dict[str, Any]:
    features, closes = _market_feature_frame(signal_date, config, benchmark_dir)
    market = config["market"]
    short_window = int(market["short_window"])
    long_window = int(market["long_window"])
    log_close = closes.map(math.log)
    short_returns = log_close - log_close.shift(short_window)
    long_returns = log_close - log_close.shift(long_window)
    date = closes.index[-1]
    row = features.loc[date]
    stress = bool(row["market_stress"])
    index_rows = {}
    for name in closes.columns:
        index_rows[name] = {
            "close": float(closes.loc[date, name]),
            "return_short": float(short_returns.loc[date, name]),
            "return_long": float(long_returns.loc[date, name]),
        }
    return {
        "as_of_date": str(date.date()),
        "source": "quantgpt_benchmark_parquet",
        "benchmarks": index_rows,
        "breadth_short": float(row["breadth_short"]),
        "breadth_long": float(row["breadth_long"]),
        "volatility_short": float(row["volatility_short"]),
        "volatility_long": float(row["volatility_long"]),
        "risk_volatility": float(row["risk_volatility"]),
        "raw_stress": bool(row["raw_stress"]),
        "raw_stress_streak": _latest_streak(features["raw_stress"], True),
        "raw_recovery_streak": _latest_streak(features["raw_stress"], False),
        "market_stress": stress,
        "cap": float(row["market_cap"]),
    }


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_risk_policy_history(
    *,
    signal_date: str,
    account_history: list[dict[str, Any]],
    recommendation_history: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    history_days: int = 160,
    benchmark_dir: Path | None = None,
) -> dict[str, Any]:
    """Reconstruct no-lookahead chart series with the production policy formula."""
    resolved = normalize_risk_policy_config(config or load_risk_policy_config())
    features, _ = _market_feature_frame(signal_date, resolved, benchmark_dir)
    features = features.tail(max(20, min(int(history_days), 520))).copy()
    market_rows: list[dict[str, Any]] = []
    for date, row in features.iterrows():
        market_rows.append(
            {
                "date": str(date.date()),
                "breadth_short": _finite_or_none(row["breadth_short"]),
                "breadth_long": _finite_or_none(row["breadth_long"]),
                "volatility_short": _finite_or_none(row["volatility_short"]),
                "volatility_long": _finite_or_none(row["volatility_long"]),
                "risk_volatility": _finite_or_none(row["risk_volatility"]),
                "raw_stress": bool(row["raw_stress"]),
                "market_stress": bool(row["market_stress"]),
                "market_cap": float(row["market_cap"]),
            }
        )

    account_cfg = resolved["account"]
    target = pd.Timestamp(signal_date).normalize()
    account_values: dict[pd.Timestamp, float] = {}
    for item in account_history:
        date_value = item.get("trade_date") or item.get("as_of_date")
        nav = _finite_or_none(item.get("account_value"))
        if not date_value or nav is None or nav <= 0:
            continue
        date = pd.Timestamp(date_value).normalize()
        if date <= target:
            account_values[date] = nav
    account_series = pd.Series(account_values, dtype=float).sort_index()
    rolling_high = account_series.rolling(int(account_cfg["drawdown_window"]), min_periods=1).max()
    drawdown = account_series / rolling_high - 1.0
    account_rows = [
        {
            "date": str(date.date()),
            "account_value": float(account_series.loc[date]),
            "rolling_high": float(rolling_high.loc[date]),
            "drawdown": float(drawdown.loc[date]),
        }
        for date in account_series.index
    ]

    model_by_date: dict[pd.Timestamp, float] = {}
    for item in sorted(
        recommendation_history,
        key=lambda value: (str(value.get("signal_date") or ""), str(value.get("created_at") or "")),
    ):
        date_value = item.get("signal_date")
        metrics = item.get("metrics") or {}
        frozen = metrics.get("risk_policy") or {}
        model_cap = _finite_or_none(
            frozen.get("model_cap", metrics.get("model_target_stock_exposure", metrics.get("target_stock_exposure")))
        )
        if not date_value or model_cap is None:
            continue
        date = pd.Timestamp(date_value).normalize()
        if date <= target:
            model_by_date[date] = min(max(model_cap, 0.0), 1.0)

    model_series = pd.Series(model_by_date, dtype=float).sort_index()
    calendar = features.index
    model_daily = model_series.reindex(calendar).ffill()
    nav_daily = account_series.reindex(calendar).ffill()
    high_daily = rolling_high.reindex(calendar).ffill()
    drawdown_daily = drawdown.reindex(calendar).ffill()
    cap_rows: list[dict[str, Any]] = []
    enforced = bool(resolved["enabled"] and resolved["mode"] == "enforced")
    for date in calendar:
        model_cap = _finite_or_none(model_daily.loc[date])
        if model_cap is None:
            continue
        market_cap = float(features.loc[date, "market_cap"])
        account_drawdown = _finite_or_none(drawdown_daily.loc[date])
        brake_active = bool(
            features.loc[date, "market_stress"]
            and account_drawdown is not None
            and account_drawdown <= -float(account_cfg["drawdown_threshold"])
        )
        account_cap = float(account_cfg["brake_cap"] if brake_active else 1.0)
        proposed = min(model_cap, market_cap, account_cap)
        final_cap = proposed if enforced else model_cap
        layers = {"model": model_cap, "market": market_cap, "account": account_cap}
        cap_rows.append(
            {
                "date": str(date.date()),
                "model_cap": model_cap,
                "market_cap": market_cap,
                "account_cap": account_cap,
                "final_cap": final_cap,
                "binding_layer": min(layers, key=layers.get),
                "brake_active": brake_active,
                "account_value": _finite_or_none(nav_daily.loc[date]),
                "rolling_high": _finite_or_none(high_daily.loc[date]),
                "drawdown": account_drawdown,
            }
        )

    return {
        "as_of_date": str(target.date()),
        "market": market_rows,
        "account": account_rows,
        "caps": cap_rows,
        "thresholds": {
            "breadth": float(resolved["market"]["breadth_threshold"]),
            "volatility": float(resolved["market"]["volatility_threshold"]),
            "drawdown": -float(resolved["account"]["drawdown_threshold"]),
        },
        "method": "reconstructed_asof_no_lookahead",
        "service": "services.trading_service.trading_risk_policy_status",
        "calculator": f"domain.trading.risk_policy:{RISK_POLICY_VERSION}",
    }


def _account_decision(
    signal_date: str,
    account_history: list[dict[str, Any]],
    current_state: dict[str, Any],
    market_stress: bool,
    config: dict[str, Any],
) -> dict[str, Any]:
    account = config["account"]
    target = pd.Timestamp(signal_date).normalize()
    values: dict[pd.Timestamp, float] = {}
    for row in account_history:
        date_value = row.get("trade_date") or row.get("as_of_date")
        value = row.get("account_value")
        if not date_value or value is None:
            continue
        date = pd.Timestamp(date_value).normalize()
        if date <= target and float(value) > 0:
            values[date] = float(value)
    state_date_value = current_state.get("as_of_date") or current_state.get("trade_date")
    state_value = current_state.get("account_value")
    if state_date_value and state_value is not None:
        state_date = pd.Timestamp(state_date_value).normalize()
        if state_date <= target and float(state_value) > 0:
            values[state_date] = float(state_value)
    ordered = sorted(values.items())[-int(account["drawdown_window"]):]
    latest_nav = ordered[-1][1] if ordered else float(current_state.get("account_value") or 0.0)
    rolling_high = max((value for _, value in ordered), default=latest_nav)
    drawdown = latest_nav / rolling_high - 1.0 if rolling_high > 0 else 0.0
    braking = bool(market_stress and drawdown <= -float(account["drawdown_threshold"]))
    return {
        "as_of_date": str(ordered[-1][0].date()) if ordered else str(current_state.get("as_of_date") or ""),
        "history_days": len(ordered),
        "drawdown_window": int(account["drawdown_window"]),
        "latest_nav": float(latest_nav),
        "rolling_high": float(rolling_high),
        "drawdown": float(drawdown),
        "brake_active": braking,
        "cap": float(account["brake_cap"] if braking else 1.0),
    }


def evaluate_risk_policy(
    *,
    signal_date: str,
    model_cap: float,
    account_history: list[dict[str, Any]],
    current_state: dict[str, Any],
    config: dict[str, Any] | None = None,
    benchmark_dir: Path | None = None,
) -> dict[str, Any]:
    resolved = normalize_risk_policy_config(config or load_risk_policy_config())
    model_cap = min(max(float(model_cap), 0.0), 1.0)
    market = _market_decision(signal_date, resolved, benchmark_dir)
    account = _account_decision(signal_date, account_history, current_state, bool(market["market_stress"]), resolved)
    proposed_cap = min(model_cap, float(market["cap"]), float(account["cap"]))
    enforced = bool(resolved["enabled"] and resolved["mode"] == "enforced")
    final_cap = proposed_cap if enforced else model_cap
    caps = {"model": model_cap, "market": float(market["cap"]), "account": float(account["cap"])}
    binding_layer = min(caps, key=caps.get)
    return {
        "version": RISK_POLICY_VERSION,
        "config_hash": risk_policy_config_hash(resolved),
        "enabled": bool(resolved["enabled"]),
        "mode": resolved["mode"],
        "enforced": enforced,
        "signal_date": str(pd.Timestamp(signal_date).date()),
        "model_cap": model_cap,
        "market_cap": float(market["cap"]),
        "account_cap": float(account["cap"]),
        "proposed_final_stock_cap": proposed_cap,
        "final_stock_cap": final_cap,
        "final_cash_weight": 1.0 - final_cap,
        "binding_layer": binding_layer,
        "scale_factor": final_cap / model_cap if model_cap > 0 else 1.0,
        "market": market,
        "account": account,
        "config": resolved,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def apply_risk_policy(
    target_df: pd.DataFrame,
    *,
    signal_date: str,
    total_capital: float,
    account_history: list[dict[str, Any]],
    current_state: dict[str, Any],
    config: dict[str, Any] | None = None,
    benchmark_dir: Path | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    target = target_df.copy()
    weights = pd.to_numeric(target.get("target_weight", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    model_cap = float(weights.sum())
    decision = evaluate_risk_policy(
        signal_date=signal_date,
        model_cap=model_cap,
        account_history=account_history,
        current_state=current_state,
        config=config,
        benchmark_dir=benchmark_dir,
    )
    factor = float(decision["scale_factor"])
    if not target.empty and factor < 1.0 - 1e-12:
        target["target_weight"] = weights * factor
        if "target_value" in target.columns:
            target["target_value"] = target["target_weight"] * float(total_capital)
    decision["target_count"] = int(len(target))
    decision["weight_sum_after"] = float(pd.to_numeric(target.get("target_weight"), errors="coerce").fillna(0.0).sum()) if not target.empty else 0.0
    return target, decision


def write_latest_risk_decision(decision: dict[str, Any], path: Path | None = None) -> None:
    path = path or TRADING_RISK_LATEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decision, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
