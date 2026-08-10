from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from domain.factor_research.factor_compute import (
    _bs_to_qlib,
    expected_trading_dates,
    _load_market_data,
)
from domain.factor_research.active_values_store import (
    active_values_store_summary,
    current_active_registry_fingerprint,
    load_active_values_manifest,
)
from .window_config import derive_unpurged_model_windows, resolve_model_end_date
from storage.factor_registry import FactorRegistry
from storage.paths import (
    ACTIVE_MODEL_FEATURE_SET_FILE,
    MODEL_ACTIVE_FEATURE_DIR,
    MODEL_ACTIVE_FEATURE_FILE,
    MODEL_ACTIVE_FEATURE_MANIFEST,
    MODEL_DEFAULT_END_DATE,
    MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    MODEL_DEFAULT_FORWARD_PERIOD,
    MODEL_DEFAULT_START_DATE,
    MODEL_DEFAULT_STATUS_FILTER,
    MODEL_FEATURE_SETS_ROOT,
)


logger = logging.getLogger(__name__)


FEATURE_SNAPSHOT_POLICY_VERSION = "qlib_feature_missing_v8_static_universe_labels"
LEGACY_FEATURE_SNAPSHOT_POLICY_VERSION = "legacy_feature_dropna_policy"
LABEL_FILTER_POLICY = "feature_index_static_universe_left_join_then_qlib_drop_unavailable_labels"
FEATURE_SPECIAL_FILL_POLICY_VERSION = "feature_structural_missing_fill_v2"
LEGACY_FEATURE_SPECIAL_FILL_POLICY_VERSION = "feature_semantic_missing_fill_v1"
FEATURE_MISSING_STRATEGY_QLIB_ONLY = "qlib_processor_only"
FEATURE_MISSING_STRATEGY_STRUCTURAL_ZERO_V2 = "structural_zero_v2"
FEATURE_MISSING_STRATEGY_SEMANTIC_V1 = "semantic_fill_v1"
FEATURE_MISSING_STRATEGY_DEFAULT = FEATURE_MISSING_STRATEGY_QLIB_ONLY
FEATURE_MISSING_STRATEGIES = {
    FEATURE_MISSING_STRATEGY_QLIB_ONLY,
    FEATURE_MISSING_STRATEGY_STRUCTURAL_ZERO_V2,
    FEATURE_MISSING_STRATEGY_SEMANTIC_V1,
}
ACTIVE_FEATURE_SNAPSHOT_CONTRACT_VERSION = "active_feature_snapshot_contract_v1"
ACTIVE_POINTER_UPDATE_POLICY = "all_active_default"
IMMUTABLE_SNAPSHOT_UPDATE_POLICY = "immutable_snapshot_only"
LABEL_PRICE_MODE = "qlib_calendar_adjusted_next_open_to_forward_open_from_quantgpt_adjusted_open"
LABEL_SOURCE_PRICE_FIELD = "open"
LABEL_ENTRY_SHIFT_DAYS = 1
LABEL_MODE_RAW_OPEN = "raw_open_return_v1"
LABEL_MODE_EXEC_OPEN_ENTRY_LIMIT_V1 = "exec_open_entry_limit_v1"
LABEL_MODE_DEFAULT = LABEL_MODE_RAW_OPEN
LABEL_MODES = {LABEL_MODE_RAW_OPEN, LABEL_MODE_EXEC_OPEN_ENTRY_LIMIT_V1}
LIMIT_TICK_TOL = 1e-6
FEATURE_MISSING_WARNING_RATIO = 0.30
FEATURE_MISSING_SEVERE_RATIO = 0.70
MARGIN_FIELD_TOKENS = {
    "margin",
    "margintrade",
    "margin_trade",
    "margin_trade_bal",
    "margin_balance",
    "margin_buy_amount",
    "short_balance",
    "borrow_money_bal",
    "purch_borrow_money",
    "sec_lending_bal",
    "rzye",
    "rzmre",
    "rqye",
}
PE_FIELD_TOKENS = {"pe", "pe_ttm", "pettm", "pe_ratio", "peratio"}


def _write_parquet_chunked(df: pd.DataFrame, path: Path, *, chunk_size: int = 100_000) -> None:
    """Write large feature matrices without materializing one giant Arrow table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    writer: pq.ParquetWriter | None = None
    try:
        for start in range(0, len(df), chunk_size):
            chunk = df.iloc[start : start + chunk_size]
            table = pa.Table.from_pandas(chunk, preserve_index=True)
            if writer is None:
                writer = pq.ParquetWriter(str(path), table.schema)
            writer.write_table(table)
    finally:
        if writer is not None:
            writer.close()


def generate_feature_set_id() -> str:
    return f"fs-{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _factor_selection_mode(*, status_filter: str, factor_ids: list[str] | None) -> str:
    if factor_ids is not None:
        return "factor_ids"
    if status_filter == MODEL_DEFAULT_STATUS_FILTER:
        return "all_active"
    return f"status_filter:{status_filter}"


def _normalize_factor_metadata(factor: dict) -> dict[str, Any]:
    metadata = factor.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    return metadata


def _build_label_frame(
    start_date: str,
    end_date: str,
    forward_period: int,
    label_mode: str = LABEL_MODE_DEFAULT,
) -> pd.DataFrame:
    if label_mode not in LABEL_MODES:
        raise ValueError(f"unsupported label_mode={label_mode}; supported={sorted(LABEL_MODES)}")
    required = {"trade_date", "stock_code", LABEL_SOURCE_PRICE_FIELD}
    if label_mode == LABEL_MODE_EXEC_OPEN_ENTRY_LIMIT_V1:
        required |= {"up_limit", "backward_factor"}
    market_df = _load_market_data(
        start_date=start_date,
        end_date=end_date,
        required_columns=required,
        filter_non_st=False,
    )
    if market_df.empty:
        raise RuntimeError("no market data loaded for LABEL0 build")

    missing = sorted(required - set(market_df.columns))
    if missing:
        raise RuntimeError(f"market data missing required LABEL0 adjusted-open columns: {missing}")

    base = market_df[["trade_date", "stock_code", LABEL_SOURCE_PRICE_FIELD]].copy()
    base["datetime"] = pd.to_datetime(base["trade_date"])
    base["instrument"] = base["stock_code"].map(_bs_to_qlib)
    # _load_market_data reads QuantGPT stock parquet, where open is already the
    # adjusted research price exported for factor research. Do not apply the
    # explicit backward_factor again here.
    base["label_price"] = base[LABEL_SOURCE_PRICE_FIELD]
    base_columns = ["datetime", "instrument", "label_price"]
    if label_mode == LABEL_MODE_EXEC_OPEN_ENTRY_LIMIT_V1:
        factor = pd.to_numeric(market_df["backward_factor"], errors="coerce")
        adjusted_open = pd.to_numeric(market_df[LABEL_SOURCE_PRICE_FIELD], errors="coerce")
        base["raw_entry_price"] = adjusted_open / factor.where(factor > 0)
        base["up_limit"] = pd.to_numeric(market_df["up_limit"], errors="coerce")
        base_columns += ["raw_entry_price", "up_limit"]
    base = base[base_columns].dropna(subset=["datetime", "instrument", "label_price"])
    base = base[base["label_price"] > 0]
    if label_mode == LABEL_MODE_EXEC_OPEN_ENTRY_LIMIT_V1:
        label = _calendar_aligned_entry_limit_exec_label_frame(
            base,
            start_date=start_date,
            end_date=end_date,
            forward_period=forward_period,
            entry_shift_days=LABEL_ENTRY_SHIFT_DAYS,
        )
    else:
        label = _calendar_aligned_label_frame(
            base,
            start_date=start_date,
            end_date=end_date,
            forward_period=forward_period,
            entry_shift_days=LABEL_ENTRY_SHIFT_DAYS,
        )
    label.columns = pd.MultiIndex.from_product([["label"], ["LABEL0"]])
    return label


def _calendar_aligned_label_frame(
    base: pd.DataFrame,
    *,
    start_date: str,
    end_date: str,
    forward_period: int,
    entry_shift_days: int = 0,
) -> pd.DataFrame:
    """Build LABEL0 on the same market-calendar offset semantics as Qlib."""
    if base.empty:
        return pd.DataFrame(columns=["LABEL0"])
    calendar = pd.DatetimeIndex(pd.to_datetime(expected_trading_dates(start_date, end_date))).sort_values()
    if calendar.empty:
        calendar = pd.DatetimeIndex(pd.to_datetime(sorted(base["datetime"].unique()))).sort_values()
    instruments = pd.Index(base["instrument"].dropna().astype(str).unique()).sort_values()
    price = (
        base.set_index(["datetime", "instrument"])["label_price"]
        .sort_index()
        .astype("float64")
    )
    full_index = pd.MultiIndex.from_product([calendar, instruments], names=["datetime", "instrument"])
    price = price.reindex(full_index)
    grouped_price = price.groupby(level="instrument", sort=False)
    entry_price = grouped_price.shift(-entry_shift_days) if entry_shift_days else price
    exit_price = grouped_price.shift(-(entry_shift_days + forward_period))
    label = (exit_price / entry_price - 1.0).dropna().to_frame("LABEL0")
    return label.sort_index()


def _calendar_aligned_entry_limit_exec_label_frame(
    base: pd.DataFrame,
    *,
    start_date: str,
    end_date: str,
    forward_period: int,
    entry_shift_days: int = 0,
    tick_tol: float = LIMIT_TICK_TOL,
) -> pd.DataFrame:
    """Build an experimental executable label that zeroes open-limit buy misses.

    Returns still use adjusted open prices.  The entry-limit check uses raw
    price space: QuantGPT adjusted open divided by backward_factor versus the
    official raw up_limit.
    """
    raw_label = _calendar_aligned_label_frame(
        base,
        start_date=start_date,
        end_date=end_date,
        forward_period=forward_period,
        entry_shift_days=entry_shift_days,
    )
    if raw_label.empty:
        return raw_label

    calendar = pd.DatetimeIndex(pd.to_datetime(expected_trading_dates(start_date, end_date))).sort_values()
    if calendar.empty:
        calendar = pd.DatetimeIndex(pd.to_datetime(sorted(base["datetime"].unique()))).sort_values()
    instruments = pd.Index(base["instrument"].dropna().astype(str).unique()).sort_values()
    full_index = pd.MultiIndex.from_product([calendar, instruments], names=["datetime", "instrument"])
    entry_raw_open = (
        base.set_index(["datetime", "instrument"])["raw_entry_price"]
        .sort_index()
        .astype("float64")
        .reindex(full_index)
    )
    up_limit = (
        base.set_index(["datetime", "instrument"])["up_limit"]
        .sort_index()
        .astype("float64")
        .reindex(full_index)
    )
    grouped_raw_open = entry_raw_open.groupby(level="instrument", sort=False)
    grouped_up_limit = up_limit.groupby(level="instrument", sort=False)
    next_raw_open = grouped_raw_open.shift(-entry_shift_days) if entry_shift_days else entry_raw_open
    next_up_limit = grouped_up_limit.shift(-entry_shift_days) if entry_shift_days else up_limit
    entry_blocked = next_raw_open.notna() & next_up_limit.notna() & (next_raw_open >= next_up_limit - tick_tol)

    label = raw_label.copy()
    blocked_on_labeled_rows = entry_blocked.reindex(label.index).fillna(False)
    label.loc[blocked_on_labeled_rows, "LABEL0"] = 0.0
    return label.sort_index()


def _feature_alias(factor_id: str, metadata: dict[str, Any]) -> str:
    raw = metadata.get("data_column") or factor_id
    safe = "".join(ch if ch.isalnum() else "_" for ch in str(raw)).strip("_")
    return safe[:80] or factor_id


def _factor_search_blob(factor: dict[str, Any], metadata: dict[str, Any]) -> str:
    parts = [
        factor.get("factor_id", ""),
        factor.get("name", ""),
        factor.get("expression", ""),
        metadata.get("data_column", ""),
        metadata.get("raw_expression", ""),
        json.dumps(metadata.get("field_context", {}), sort_keys=True, ensure_ascii=False),
    ]
    return " ".join(str(part or "") for part in parts).lower()


def _contains_any_token(blob: str, tokens: set[str]) -> bool:
    compact = "".join(ch for ch in blob if ch.isalnum())
    normalized = "_" + "".join(ch if ch.isalnum() else "_" for ch in blob).strip("_") + "_"
    words = set(item for item in re.split(r"[^a-z0-9]+", blob.lower()) if item)
    for token in tokens:
        token_l = token.lower()
        if len(token_l) <= 2:
            if token_l in words:
                return True
            continue
        if token_l in compact or f"_{token_l}_" in normalized:
            return True
    return False


def _semantic_missing_policy_candidate(factor: dict[str, Any], metadata: dict[str, Any]) -> str:
    blob = _factor_search_blob(factor, metadata)
    if _contains_any_token(blob, PE_FIELD_TOKENS):
        return "pe_cross_sectional_floor"
    if _contains_any_token(blob, MARGIN_FIELD_TOKENS):
        return "margin_structural_zero"
    return "qlib_processor_neutral_fill"


def _cross_sectional_floor_fill(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    valid = numeric.dropna()
    if valid.empty:
        return numeric
    global_spread = float(valid.max() - valid.min())
    global_eps = max(abs(global_spread) * 0.001, 1e-9)
    global_floor = float(valid.min()) - global_eps
    date_min = numeric.groupby(level="datetime").transform("min")
    date_max = numeric.groupby(level="datetime").transform("max")
    spread = (date_max - date_min).abs()
    eps = (spread * 0.001).where(spread > 0, 1e-9)
    fill_values = (date_min - eps).fillna(global_floor)
    return numeric.where(numeric.notna(), fill_values)


def _feature_missing_policy_label(feature_missing_strategy: str) -> str:
    if feature_missing_strategy == FEATURE_MISSING_STRATEGY_QLIB_ONLY:
        return "label_drop_feature_nan_preserved_then_qlib_processors"
    if feature_missing_strategy == FEATURE_MISSING_STRATEGY_STRUCTURAL_ZERO_V2:
        return "label_drop_feature_structural_zero_then_qlib_processors"
    if feature_missing_strategy == FEATURE_MISSING_STRATEGY_SEMANTIC_V1:
        return "label_drop_feature_semantic_fill_then_qlib_processors"
    return f"label_drop_feature_unknown_strategy:{feature_missing_strategy}"


def _feature_special_fill_policy_version(feature_missing_strategy: str) -> str:
    if feature_missing_strategy == FEATURE_MISSING_STRATEGY_SEMANTIC_V1:
        return LEGACY_FEATURE_SPECIAL_FILL_POLICY_VERSION
    return FEATURE_SPECIAL_FILL_POLICY_VERSION


def _apply_feature_missing_fill_policy(
    combined: pd.DataFrame,
    factor_records: list[dict[str, Any]],
    *,
    feature_missing_strategy: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    if feature_missing_strategy not in FEATURE_MISSING_STRATEGIES:
        raise ValueError(
            f"unsupported feature_missing_strategy={feature_missing_strategy}; "
            f"supported={sorted(FEATURE_MISSING_STRATEGIES)}"
        )
    report: list[dict[str, Any]] = []
    for record in factor_records:
        alias = record.get("data_column")
        col = ("feature", alias)
        if not alias or col not in combined.columns:
            continue
        semantic_policy = str(record.get("semantic_missing_policy_candidate") or "qlib_processor_neutral_fill")
        before = int(combined[col].isna().sum())
        filled = 0
        policy = "qlib_processor_neutral_fill"
        if (
            feature_missing_strategy == FEATURE_MISSING_STRATEGY_STRUCTURAL_ZERO_V2
            and semantic_policy == "margin_structural_zero"
        ):
            policy = "margin_structural_zero"
        elif feature_missing_strategy == FEATURE_MISSING_STRATEGY_SEMANTIC_V1:
            policy = semantic_policy
        if before and policy == "margin_structural_zero":
            combined[col] = pd.to_numeric(combined[col], errors="coerce").fillna(0.0)
            filled = before - int(combined[col].isna().sum())
        elif before and feature_missing_strategy == FEATURE_MISSING_STRATEGY_SEMANTIC_V1 and policy == "pe_cross_sectional_floor":
            combined[col] = _cross_sectional_floor_fill(combined[col])
            filled = before - int(combined[col].isna().sum())
        record["missing_fill_policy"] = policy
        record["missing_fill_count"] = filled
        if policy != "qlib_processor_neutral_fill" or semantic_policy != "qlib_processor_neutral_fill" or before:
            report.append(
                {
                    "factor_id": record.get("factor_id", ""),
                    "feature_column": str(alias),
                    "policy": policy,
                    "semantic_policy_candidate": semantic_policy,
                    "missing_before": before,
                    "filled_count": filled,
                    "missing_after": int(combined[col].isna().sum()),
                }
            )
    return combined, report


def _date_mask(index: pd.MultiIndex, start: str, end: str) -> pd.Series:
    dates = pd.to_datetime(index.get_level_values("datetime")).normalize()
    return pd.Series((dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end)), index=index)


def _feature_missing_report(
    combined: pd.DataFrame,
    feature_cols: list[tuple[str, str]],
    *,
    start_date: str,
    end_date: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    total = int(len(combined))
    if total == 0:
        return {
            "total_cells": 0,
            "missing_cells": 0,
            "missing_ratio": 0.0,
            "warning_count": 0,
            "severe_warning_count": 0,
        }, []

    windows = derive_unpurged_model_windows(start_date=start_date, end_date=end_date)
    segments = {
        "train": (windows["train_start"], windows["train_end"]),
        "valid": (windows["valid_start"], windows["valid_end"]),
        "test": (windows["test_start"], windows["test_end"]),
    }
    report: list[dict[str, Any]] = []
    missing_cells = 0
    total_cells = total * len(feature_cols)
    warning_count = 0
    severe_count = 0

    for col in feature_cols:
        series = combined[col]
        non_null_count = int(series.notna().sum())
        missing_count = int(total - non_null_count)
        missing_cells += missing_count
        missing_ratio = float(missing_count / total) if total else 0.0
        level = "ok"
        if missing_ratio > FEATURE_MISSING_SEVERE_RATIO:
            level = "severe_warning"
            severe_count += 1
        elif missing_ratio > FEATURE_MISSING_WARNING_RATIO:
            level = "warning"
            warning_count += 1

        segment_missing_ratio: dict[str, float | None] = {}
        for name, (seg_start, seg_end) in segments.items():
            mask = _date_mask(combined.index, seg_start, seg_end)
            seg_series = series[mask.values]
            segment_missing_ratio[name] = (
                float(seg_series.isna().sum() / len(seg_series)) if len(seg_series) else None
            )

        report.append(
            {
                "feature_column": str(col[1]),
                "non_null_count": non_null_count,
                "missing_count": missing_count,
                "missing_ratio": missing_ratio,
                "segment_missing_ratio": segment_missing_ratio,
                "coverage_level": level,
            }
        )

    summary = {
        "total_cells": int(total_cells),
        "missing_cells": int(missing_cells),
        "missing_ratio": float(missing_cells / total_cells) if total_cells else 0.0,
        "warning_threshold": FEATURE_MISSING_WARNING_RATIO,
        "severe_warning_threshold": FEATURE_MISSING_SEVERE_RATIO,
        "warning_count": warning_count,
        "severe_warning_count": severe_count,
        "policy": "audit_only_no_auto_exclusion",
    }
    return summary, report


def _normalize_feature_set_manifest(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(manifest, dict):
        return None
    normalized = dict(manifest)
    requested_end_date = normalized.get("requested_end_date") or normalized.get("end_date") or normalized.get("latest_date")
    resolved_end_date = (
        normalized.get("resolved_end_date")
        or normalized.get("actual_end_date")
        or normalized.get("latest_date")
        or normalized.get("end_date")
    )
    normalized["requested_end_date"] = requested_end_date
    normalized["resolved_end_date"] = resolved_end_date
    normalized["end_date"] = normalized.get("end_date") or resolved_end_date
    normalized["latest_date"] = normalized.get("latest_date") or normalized.get("actual_end_date") or resolved_end_date
    normalized["feature_file"] = normalized.get("feature_file") or normalized.get("combined_factors_file") or ""
    normalized["combined_factors_file"] = normalized.get("combined_factors_file") or normalized.get("feature_file") or ""
    return normalized


def compute_feature_set_fingerprint(
    *,
    feature_set_id: str,
    factor_records: list[dict[str, Any]],
    combined_factors_file: str | Path,
    label_forward_period: int,
    factor_holding_period_days: int = MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    feature_missing_strategy: str = FEATURE_MISSING_STRATEGY_DEFAULT,
    label_mode: str = LABEL_MODE_DEFAULT,
) -> str:
    parts = [
        f"feature_set_id={feature_set_id}",
        f"feature_snapshot_policy_version={FEATURE_SNAPSHOT_POLICY_VERSION}",
        f"label_forward_period={label_forward_period}",
        f"label_price_mode={LABEL_PRICE_MODE}",
        f"label_source_price_field={LABEL_SOURCE_PRICE_FIELD}",
        f"label_mode={label_mode}",
        f"label_entry_shift_days={LABEL_ENTRY_SHIFT_DAYS}",
        f"label_exit_shift_days={LABEL_ENTRY_SHIFT_DAYS + int(label_forward_period)}",
        "label_execution_deal_price=open",
        "label_return_mode=next_open_to_forward_open",
        f"factor_holding_period_days={factor_holding_period_days}",
        f"feature_missing_strategy={feature_missing_strategy}",
        f"combined_factors_file={combined_factors_file}",
    ]
    for record in sorted(factor_records, key=lambda item: item["factor_id"]):
        parts.append(
            "|".join(
                [
                    record["factor_id"],
                    str(record.get("data_path", "")),
                    str(record.get("data_mtime", "")),
                    str(record.get("data_size", "")),
                ]
            )
        )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def build_active_feature_set(
    *,
    feature_set_id: str | None = None,
    status_filter: str = MODEL_DEFAULT_STATUS_FILTER,
    start_date: str = MODEL_DEFAULT_START_DATE,
    end_date: str | None = None,
    label_forward_period: int = MODEL_DEFAULT_FORWARD_PERIOD,
    factor_holding_period_days: int = MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    factor_ids: list[str] | None = None,
    feature_missing_strategy: str = FEATURE_MISSING_STRATEGY_DEFAULT,
    label_mode: str = LABEL_MODE_DEFAULT,
    update_active: bool | None = None,
) -> dict[str, Any]:
    if feature_missing_strategy not in FEATURE_MISSING_STRATEGIES:
        raise ValueError(
            f"unsupported feature_missing_strategy={feature_missing_strategy}; "
            f"supported={sorted(FEATURE_MISSING_STRATEGIES)}"
        )
    if label_mode not in LABEL_MODES:
        raise ValueError(f"unsupported label_mode={label_mode}; supported={sorted(LABEL_MODES)}")
    explicit_factor_selection = factor_ids is not None
    if update_active is None:
        update_active = (not explicit_factor_selection) and status_filter == MODEL_DEFAULT_STATUS_FILTER

    feature_set_id = feature_set_id or generate_feature_set_id()
    factor_selection_mode = _factor_selection_mode(status_filter=status_filter, factor_ids=factor_ids)
    requested_end_date = end_date or MODEL_DEFAULT_END_DATE or datetime.now().strftime("%Y-%m-%d")
    resolved_end_date = resolve_model_end_date(end_date)

    registry = FactorRegistry()
    if status_filter == "active":
        factors, _ = registry.list_all(
            status="active",
            limit=10000,
            offset=0,
            holding_period_days=factor_holding_period_days,
        )
    else:
        factors, _ = registry.list_all(status=status_filter, limit=10000, offset=0, holding_period_days=factor_holding_period_days)

    if not factors:
        raise RuntimeError(f"no factors found for status={status_filter}")
    if factor_ids is not None:
        wanted = {str(item) for item in factor_ids}
        factors = [factor for factor in factors if str(factor.get("factor_id") or "") in wanted]
        missing = sorted(wanted - {str(factor.get("factor_id") or "") for factor in factors})
        if missing:
            raise RuntimeError(f"requested factor_ids not found for status={status_filter}: {missing[:5]}")
        if not factors:
            raise RuntimeError("no factors selected for requested factor_ids")

    active_values_summary: dict[str, Any] = {}
    if status_filter == "active":
        active_values_summary = active_values_store_summary(
            holding_period_days=factor_holding_period_days,
            registry=registry,
        )
        if active_values_summary.get("stale"):
            raise RuntimeError(
                "active values store is stale; refresh /factor/active-values before freezing model features "
                f"(registry_fingerprint={active_values_summary.get('registry_fingerprint')}, "
                f"manifest_registry_fingerprint={active_values_summary.get('manifest_registry_fingerprint')}, "
                f"universe={active_values_summary.get('resolved_universe')})"
            )

    label = _build_label_frame(
        start_date=start_date,
        end_date=resolved_end_date,
        forward_period=label_forward_period,
        label_mode=label_mode,
    )

    feature_frames: list[pd.DataFrame] = []
    factor_records: list[dict[str, Any]] = []
    seen_aliases: set[str] = set()

    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(resolved_end_date)

    for factor in factors:
        metadata = _normalize_factor_metadata(factor)
        data_path_raw = metadata.get("data_path", "")
        if not data_path_raw:
            logger.warning("skip factor %s: missing data_path", factor["factor_id"])
            continue

        data_path = Path(data_path_raw)
        if not data_path.exists():
            logger.warning("skip factor %s: missing parquet %s", factor["factor_id"], data_path)
            continue

        df = pd.read_parquet(data_path)
        if not df.empty:
            dates = df.index.get_level_values("datetime")
            df = df[(dates >= start_ts) & (dates <= end_ts)].copy()
        if df.empty:
            logger.warning(
                "skip factor %s: no values in snapshot window %s..%s",
                factor["factor_id"],
                start_date,
                resolved_end_date,
            )
            continue
        alias = _feature_alias(factor["factor_id"], metadata)
        if alias in seen_aliases:
            alias = f"{alias}_{factor['factor_id'][-6:]}"
        seen_aliases.add(alias)

        df.columns = pd.MultiIndex.from_product([["feature"], [alias]])
        feature_frames.append(df)
        semantic_missing_policy = _semantic_missing_policy_candidate(factor, metadata)
        factor_records.append(
            {
                "factor_id": factor["factor_id"],
                "name": factor.get("name", ""),
                "expression": factor.get("expression", ""),
                "data_path": str(data_path),
                "data_column": alias,
                "holding_period_days": factor.get("holding_period_days"),
                "ic_mean": factor.get("ic_mean"),
                "icir": factor.get("icir"),
                "semantic_missing_policy_candidate": semantic_missing_policy,
                "missing_fill_policy": "qlib_processor_neutral_fill",
                "missing_fill_count": 0,
                "data_mtime": data_path.stat().st_mtime,
                "data_size": data_path.stat().st_size,
            }
        )

    if not factor_records:
        raise RuntimeError("no factor parquet files could be loaded")

    label_sample_count = int(label.shape[0])
    combined_features = pd.concat(feature_frames, axis=1, join="outer").sort_index()
    combined = combined_features.join(label, how="left")
    label_cols = [col for col in combined.columns if col[0] == "label"]
    label_available_sample_count = (
        int(combined[label_cols].notna().all(axis=1).sum())
        if label_cols
        else 0
    )
    label_missing_sample_count = int(len(combined) - label_available_sample_count) if label_cols else int(len(combined))

    feature_cols = [col for col in combined.columns if col[0] == "feature"]
    raw_feature_missing_summary, raw_feature_coverage_report = _feature_missing_report(
        combined,
        feature_cols,
        start_date=start_date,
        end_date=resolved_end_date,
    )
    combined, semantic_missing_audit_report = _apply_feature_missing_fill_policy(
        combined,
        factor_records,
        feature_missing_strategy=feature_missing_strategy,
    )
    feature_imputation_report = [
        item for item in semantic_missing_audit_report if int(item.get("filled_count") or 0) > 0
    ]
    post_snapshot_feature_missing_summary, feature_coverage_report = _feature_missing_report(
        combined,
        feature_cols,
        start_date=start_date,
        end_date=resolved_end_date,
    )

    if combined.empty:
        raise RuntimeError("combined feature set is empty after joining labels and features")

    feature_set_dir = MODEL_FEATURE_SETS_ROOT / feature_set_id
    feature_set_dir.mkdir(parents=True, exist_ok=True)
    combined_path = feature_set_dir / "combined_factors_df.parquet"
    manifest_path = feature_set_dir / "manifest.json"

    _write_parquet_chunked(combined, combined_path)

    feature_dates = combined.index.get_level_values("datetime")
    actual_start_date = str(pd.Timestamp(feature_dates.min()).date())
    actual_end_date = str(pd.Timestamp(feature_dates.max()).date())
    fingerprint = compute_feature_set_fingerprint(
        feature_set_id=feature_set_id,
        factor_records=factor_records,
        combined_factors_file=combined_path,
        label_forward_period=label_forward_period,
        factor_holding_period_days=factor_holding_period_days,
        feature_missing_strategy=feature_missing_strategy,
        label_mode=label_mode,
    )
    active_factor_registry_fingerprint, _ = current_active_registry_fingerprint(
        holding_period_days=factor_holding_period_days,
        registry=registry,
    )
    active_values_manifest = load_active_values_manifest() or {}
    active_values_lineage = {
        "schema_version": active_values_manifest.get("schema_version"),
        "generated_at": active_values_manifest.get("generated_at"),
        "resolved_universe": active_values_manifest.get("resolved_universe") or active_values_manifest.get("universe"),
        "universe": active_values_manifest.get("resolved_universe") or active_values_manifest.get("universe"),
        "value_start_date": active_values_manifest.get("value_start_date"),
        "value_end_date": active_values_manifest.get("value_end_date"),
        "filter_non_st_before_expression": active_values_manifest.get("filter_non_st_before_expression"),
        "compute_semantics_version": active_values_manifest.get("compute_semantics_version"),
        "source_data_kind": active_values_manifest.get("source_data_kind"),
        "source_data_fingerprint": active_values_manifest.get("source_data_fingerprint"),
        "source_data_signature": active_values_manifest.get("source_data_signature"),
        "registry_fingerprint": active_values_manifest.get("registry_fingerprint"),
        "audit_anchor": active_values_manifest.get("audit_anchor"),
        "stale": active_values_summary.get("stale"),
        "manifest_path": active_values_summary.get("manifest_path"),
        "active_values_path": active_values_summary.get("path"),
    }
    manifest = {
        "feature_set_id": feature_set_id,
        "active_feature_snapshot_contract_version": ACTIVE_FEATURE_SNAPSHOT_CONTRACT_VERSION,
        "feature_snapshot_policy_version": FEATURE_SNAPSHOT_POLICY_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status_filter": status_filter,
        "factor_ids": [str(factor.get("factor_id") or "") for factor in factors],
        "factor_selection_mode": factor_selection_mode,
        "start_date": start_date,
        "end_date": resolved_end_date,
        "requested_end_date": requested_end_date,
        "resolved_end_date": resolved_end_date,
        "label_forward_period": label_forward_period,
        "holding_period_days": factor_holding_period_days,
        "factor_holding_period_days": factor_holding_period_days,
        "label_price_mode": LABEL_PRICE_MODE,
        "label_mode": label_mode,
        "label_source_price_field": LABEL_SOURCE_PRICE_FIELD,
        "label_entry_shift_days": LABEL_ENTRY_SHIFT_DAYS,
        "label_exit_shift_days": LABEL_ENTRY_SHIFT_DAYS + label_forward_period,
        "label_execution_deal_price": "open",
        "label_return_mode": "next_open_to_forward_open",
        "label_adjustment_field": None,
        "label_uses_adjusted_price": True,
        "combined_factors_file": str(combined_path),
        "feature_file": str(combined_path),
        "updates_active_feature_pointer": bool(update_active),
        "active_pointer_update_policy": ACTIVE_POINTER_UPDATE_POLICY if update_active else IMMUTABLE_SNAPSHOT_UPDATE_POLICY,
        "feature_set_fingerprint": fingerprint,
        "active_factor_registry_fingerprint": active_factor_registry_fingerprint,
        "active_values_lineage": active_values_lineage,
        "shape": list(combined.shape),
        "sample_count": int(combined.shape[0]),
        "label_sample_count": label_sample_count,
        "label_available_sample_count": label_available_sample_count,
        "label_missing_sample_count": label_missing_sample_count,
        "post_label_drop_sample_count": int(combined.shape[0]),
        "label_filter_policy": LABEL_FILTER_POLICY,
        "feature_special_fill_policy_version": _feature_special_fill_policy_version(feature_missing_strategy),
        "feature_missing_strategy": feature_missing_strategy,
        "prefill_applied": bool(feature_imputation_report),
        "feature_missing_policy": _feature_missing_policy_label(feature_missing_strategy),
        "raw_feature_missing_summary": raw_feature_missing_summary,
        "raw_feature_coverage_report": raw_feature_coverage_report,
        "semantic_missing_audit_report": semantic_missing_audit_report,
        "feature_imputation_report": feature_imputation_report,
        "post_snapshot_feature_missing_summary": post_snapshot_feature_missing_summary,
        "feature_missing_summary": post_snapshot_feature_missing_summary,
        "feature_coverage_report": feature_coverage_report,
        "actual_start_date": actual_start_date,
        "actual_end_date": actual_end_date,
        "latest_date": actual_end_date,
        "factor_count": len(factor_records),
        "feature_count": len(feature_cols),
        "factor_records": factor_records,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    if update_active:
        MODEL_ACTIVE_FEATURE_DIR.mkdir(parents=True, exist_ok=True)
        _write_parquet_chunked(combined, MODEL_ACTIVE_FEATURE_FILE)
        MODEL_ACTIVE_FEATURE_MANIFEST.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        ACTIVE_MODEL_FEATURE_SET_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE_MODEL_FEATURE_SET_FILE.write_text(
            json.dumps(
                {
                    "feature_set_id": feature_set_id,
                    "active_feature_snapshot_contract_version": ACTIVE_FEATURE_SNAPSHOT_CONTRACT_VERSION,
                    "feature_snapshot_policy_version": FEATURE_SNAPSHOT_POLICY_VERSION,
                    "generated_at": manifest["generated_at"],
                    "status_filter": status_filter,
                    "factor_selection_mode": factor_selection_mode,
                    "start_date": start_date,
                    "end_date": resolved_end_date,
                    "requested_end_date": requested_end_date,
                    "resolved_end_date": resolved_end_date,
                    "label_forward_period": label_forward_period,
                    "holding_period_days": factor_holding_period_days,
                    "factor_holding_period_days": factor_holding_period_days,
                    "label_price_mode": LABEL_PRICE_MODE,
                    "label_mode": label_mode,
                    "label_source_price_field": LABEL_SOURCE_PRICE_FIELD,
                    "label_entry_shift_days": LABEL_ENTRY_SHIFT_DAYS,
                    "label_exit_shift_days": LABEL_ENTRY_SHIFT_DAYS + label_forward_period,
                    "label_execution_deal_price": "open",
                    "label_return_mode": "next_open_to_forward_open",
                    "label_adjustment_field": None,
                    "label_uses_adjusted_price": True,
                    "updates_active_feature_pointer": True,
                    "active_pointer_update_policy": ACTIVE_POINTER_UPDATE_POLICY,
                    "feature_set_fingerprint": fingerprint,
                    "active_factor_registry_fingerprint": active_factor_registry_fingerprint,
                    "active_values_lineage": active_values_lineage,
                    "combined_factors_file": str(MODEL_ACTIVE_FEATURE_FILE),
                    "feature_file": str(MODEL_ACTIVE_FEATURE_FILE),
                    "manifest_file": str(MODEL_ACTIVE_FEATURE_MANIFEST),
                    "sample_count": int(combined.shape[0]),
                    "label_sample_count": label_sample_count,
                    "label_available_sample_count": label_available_sample_count,
                    "label_missing_sample_count": label_missing_sample_count,
                    "post_label_drop_sample_count": int(combined.shape[0]),
                    "label_filter_policy": LABEL_FILTER_POLICY,
                    "feature_special_fill_policy_version": _feature_special_fill_policy_version(feature_missing_strategy),
                    "feature_missing_strategy": feature_missing_strategy,
                    "prefill_applied": bool(feature_imputation_report),
                    "feature_missing_policy": _feature_missing_policy_label(feature_missing_strategy),
                    "raw_feature_missing_summary": raw_feature_missing_summary,
                    "semantic_missing_audit_report": semantic_missing_audit_report,
                    "feature_imputation_report": feature_imputation_report,
                    "post_snapshot_feature_missing_summary": post_snapshot_feature_missing_summary,
                    "feature_missing_summary": post_snapshot_feature_missing_summary,
                    "shape": list(combined.shape),
                    "actual_start_date": actual_start_date,
                    "actual_end_date": actual_end_date,
                    "latest_date": actual_end_date,
                    "factor_count": len(factor_records),
                    "feature_count": len(feature_cols),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    logger.info(
        "built feature set %s: %d rows, %d features -> %s (update_active=%s)",
        feature_set_id,
        len(combined),
        len(feature_cols),
        combined_path,
        bool(update_active),
    )
    return manifest


def load_active_feature_set_manifest() -> dict[str, Any] | None:
    if MODEL_ACTIVE_FEATURE_MANIFEST.exists():
        try:
            return _normalize_feature_set_manifest(json.loads(MODEL_ACTIVE_FEATURE_MANIFEST.read_text(encoding="utf-8")))
        except Exception:
            return None
    if ACTIVE_MODEL_FEATURE_SET_FILE.exists():
        try:
            return _normalize_feature_set_manifest(json.loads(ACTIVE_MODEL_FEATURE_SET_FILE.read_text(encoding="utf-8")))
        except Exception:
            return None
    return None


def load_feature_set_manifest(feature_set_id: str) -> dict[str, Any] | None:
    manifest_path = MODEL_FEATURE_SETS_ROOT / feature_set_id / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        return _normalize_feature_set_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _active_values_lineage_key(lineage: dict[str, Any] | None) -> dict[str, Any]:
    """Small readiness key: prove the snapshot used the current active values."""
    lineage = lineage if isinstance(lineage, dict) else {}
    anchor = lineage.get("audit_anchor") if isinstance(lineage.get("audit_anchor"), dict) else {}
    return {
        "registry_fingerprint": str(lineage.get("registry_fingerprint") or ""),
        "resolved_universe": str(lineage.get("resolved_universe") or lineage.get("universe") or ""),
        "source_data_fingerprint": str(lineage.get("source_data_fingerprint") or ""),
        "audit_anchor_passed": anchor.get("passed") is True,
    }


def active_feature_snapshot_contract_status(
    manifest: dict[str, Any] | None,
    *,
    active_values_summary: dict[str, Any] | None = None,
    current_registry_fingerprint: str = "",
) -> dict[str, Any]:
    """Validate a frozen model feature snapshot without forcing a feature set.

    Model research must support different feature sets. The hard contract is
    therefore snapshot integrity and explicit lineage, not "all active only".
    All-active snapshots receive an extra freshness check elsewhere.
    """

    manifest = manifest or {}
    active_values_summary = active_values_summary or {}
    violations: list[str] = []
    warnings: list[str] = []
    selection_mode = str(manifest.get("factor_selection_mode") or "") if manifest else ""
    status_filter = str(manifest.get("status_filter") or "") if manifest else ""
    updates_active_pointer = manifest.get("updates_active_feature_pointer") is True if manifest else False
    is_all_active_default = bool(selection_mode == "all_active" and status_filter == MODEL_DEFAULT_STATUS_FILTER)

    if not manifest:
        violations.append("missing_active_feature_snapshot")
    else:
        if str(manifest.get("feature_snapshot_policy_version") or LEGACY_FEATURE_SNAPSHOT_POLICY_VERSION) != FEATURE_SNAPSHOT_POLICY_VERSION:
            violations.append("feature_snapshot_policy_mismatch")
        if str(manifest.get("feature_missing_strategy") or "legacy_unspecified") != FEATURE_MISSING_STRATEGY_DEFAULT:
            warnings.append("feature_missing_strategy_not_production_default")
        if bool(manifest.get("prefill_applied")):
            warnings.append("pre_qlib_feature_prefill_applied")
        if int(manifest.get("label_forward_period") or 0) != MODEL_DEFAULT_FORWARD_PERIOD:
            warnings.append("label_forward_period_not_production_default")
        if str(manifest.get("label_price_mode") or "") != LABEL_PRICE_MODE:
            violations.append("label_price_mode_mismatch")
        if str(manifest.get("label_source_price_field") or "") != LABEL_SOURCE_PRICE_FIELD:
            violations.append("label_source_price_field_mismatch")
        if int(manifest.get("label_entry_shift_days") or 0) != LABEL_ENTRY_SHIFT_DAYS:
            violations.append("label_entry_shift_days_mismatch")
        if int(manifest.get("label_exit_shift_days") or 0) != LABEL_ENTRY_SHIFT_DAYS + MODEL_DEFAULT_FORWARD_PERIOD:
            violations.append("label_exit_shift_days_mismatch")
        if str(manifest.get("label_execution_deal_price") or "") != "open":
            violations.append("label_execution_deal_price_mismatch")
        if str(manifest.get("label_return_mode") or "") != "next_open_to_forward_open":
            violations.append("label_return_mode_mismatch")
        factor_holding_period = int(
            manifest.get("factor_holding_period_days", manifest.get("holding_period_days") or 0) or 0
        )
        if factor_holding_period != MODEL_DEFAULT_FACTOR_HOLDING_PERIOD:
            warnings.append("factor_holding_period_not_production_default")
        if not str(manifest.get("combined_factors_file") or manifest.get("feature_file") or ""):
            violations.append("missing_combined_factors_file")
        if int(manifest.get("factor_count") or 0) <= 0:
            violations.append("empty_factor_set")
        if int(manifest.get("feature_count") or 0) <= 0:
            violations.append("empty_feature_set")
        if not isinstance(manifest.get("active_values_lineage"), dict):
            warnings.append("missing_active_values_lineage")
        if not updates_active_pointer:
            warnings.append("immutable_snapshot_not_default_active_pointer")

        current_active_count = int(active_values_summary.get("active_count") or 0)
        snapshot_factor_count = int(manifest.get("factor_count") or 0)
        if is_all_active_default and current_active_count and snapshot_factor_count != current_active_count:
            violations.append("active_factor_count_mismatch")

        snapshot_registry_fingerprint = str(manifest.get("active_factor_registry_fingerprint") or "")
        if is_all_active_default and current_registry_fingerprint and snapshot_registry_fingerprint != current_registry_fingerprint:
            violations.append("active_factor_registry_fingerprint_mismatch")

        if not is_all_active_default and current_registry_fingerprint and snapshot_registry_fingerprint != current_registry_fingerprint:
            warnings.append("snapshot_lineage_differs_from_current_active_registry")

    return {
        "version": ACTIVE_FEATURE_SNAPSHOT_CONTRACT_VERSION,
        "ok": not violations,
        "violations": violations,
        "warnings": warnings,
        "selection_mode": selection_mode,
        "status_filter": status_filter,
        "updates_active_feature_pointer": updates_active_pointer,
        "is_all_active_default": is_all_active_default,
        "default_pointer_policy": ACTIVE_POINTER_UPDATE_POLICY,
        "immutable_snapshot_policy": IMMUTABLE_SNAPSHOT_UPDATE_POLICY,
        "required_feature_snapshot_policy_version": FEATURE_SNAPSHOT_POLICY_VERSION,
        "required_feature_missing_strategy": FEATURE_MISSING_STRATEGY_DEFAULT,
        "required_label_forward_period": MODEL_DEFAULT_FORWARD_PERIOD,
        "required_label_price_mode": LABEL_PRICE_MODE,
        "required_label_source_price_field": LABEL_SOURCE_PRICE_FIELD,
        "required_label_entry_shift_days": LABEL_ENTRY_SHIFT_DAYS,
        "required_label_exit_shift_days": LABEL_ENTRY_SHIFT_DAYS + MODEL_DEFAULT_FORWARD_PERIOD,
        "required_label_execution_deal_price": "open",
        "required_label_return_mode": "next_open_to_forward_open",
        "required_factor_holding_period_days": MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
        "current_active_factor_count": int(active_values_summary.get("active_count") or 0),
        "active_feature_factor_count": int(manifest.get("factor_count") or 0) if manifest else 0,
    }


def active_feature_snapshot_staleness(
    manifest: dict[str, Any] | None = None,
    *,
    holding_period_days: int = MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    registry: FactorRegistry | None = None,
    active_values_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = manifest or load_active_feature_set_manifest() or {}
    active_values_summary = active_values_summary or active_values_store_summary(
        holding_period_days=holding_period_days,
        registry=registry,
    )
    snapshot_registry_fingerprint = str(manifest.get("active_factor_registry_fingerprint") or "")
    snapshot_policy_version = str(manifest.get("feature_snapshot_policy_version") or LEGACY_FEATURE_SNAPSHOT_POLICY_VERSION)
    snapshot_lineage = manifest.get("active_values_lineage") if isinstance(manifest.get("active_values_lineage"), dict) else {}
    current_lineage = {
        "registry_fingerprint": active_values_summary.get("manifest_registry_fingerprint")
        or active_values_summary.get("registry_fingerprint"),
        "resolved_universe": active_values_summary.get("resolved_universe") or active_values_summary.get("universe"),
        "source_data_fingerprint": active_values_summary.get("current_source_data_fingerprint")
        or active_values_summary.get("source_data_fingerprint"),
        "audit_anchor": active_values_summary.get("audit_anchor", {}),
    }
    current_key = _active_values_lineage_key(current_lineage)
    snapshot_key = _active_values_lineage_key(snapshot_lineage)
    contract_status = active_feature_snapshot_contract_status(
        manifest,
        active_values_summary=active_values_summary,
        current_registry_fingerprint=current_key["registry_fingerprint"],
    )
    enforce_current_active_lineage = bool(
        contract_status.get("is_all_active_default")
        or (
            manifest.get("updates_active_feature_pointer") is True
            and str(manifest.get("status_filter") or "") == MODEL_DEFAULT_STATUS_FILTER
        )
    )
    stale_reasons: list[str] = []
    if not manifest:
        stale_reasons.append("missing_active_feature_snapshot")
    if enforce_current_active_lineage and active_values_summary.get("stale"):
        stale_reasons.append("active_values_store_stale")
    lineage_without_source_current = dict(current_key)
    lineage_without_source_snapshot = dict(snapshot_key)
    lineage_without_source_current.pop("source_data_fingerprint", None)
    lineage_without_source_snapshot.pop("source_data_fingerprint", None)
    source_data_mismatch = bool(
        snapshot_key.get("source_data_fingerprint")
        and current_key.get("source_data_fingerprint")
        and snapshot_key.get("source_data_fingerprint") != current_key.get("source_data_fingerprint")
    )
    source_data_untracked = bool(active_values_summary.get("source_data_untracked"))
    if enforce_current_active_lineage and lineage_without_source_snapshot != lineage_without_source_current:
        stale_reasons.append("active_values_lineage_mismatch")
    if enforce_current_active_lineage and current_key["audit_anchor_passed"] is not True:
        stale_reasons.append("active_values_audit_anchor_not_passed")
    stale_reasons.extend(contract_status["violations"])
    stale_reasons = list(dict.fromkeys(stale_reasons))
    stale = bool(stale_reasons)
    active_feature_factor_count = int(manifest.get("factor_count") or 0) if manifest else 0
    policy_mismatch = str(snapshot_policy_version) != FEATURE_SNAPSHOT_POLICY_VERSION

    return {
        "stale": stale,
        "stale_reason": stale_reasons[0] if stale_reasons else "",
        "stale_reasons": stale_reasons,
        "factor_registry_fingerprint": current_key["registry_fingerprint"],
        "active_factor_count": int(active_values_summary.get("active_count") or 0),
        "active_feature_fingerprint": snapshot_registry_fingerprint,
        "current_active_factor_count": int(active_values_summary.get("active_count") or 0),
        "active_feature_factor_count": active_feature_factor_count,
        "active_values_registry_fingerprint": current_key["registry_fingerprint"],
        "active_feature_registry_fingerprint": snapshot_registry_fingerprint,
        "required_action": "fxalpha_model_feature_snapshot" if stale else "",
        "trigger_owner": "model_side",
        "model_snapshot_refresh_required": stale,
        "snapshot_policy_version": snapshot_policy_version if manifest else None,
        "current_policy_version": FEATURE_SNAPSHOT_POLICY_VERSION,
        "policy_mismatch": policy_mismatch,
        "source_data_mismatch": source_data_mismatch,
        "source_data_untracked": source_data_untracked,
        "active_feature_contract": contract_status,
        "active_values_summary": active_values_summary,
        "active_values_lineage": snapshot_lineage,
        "lineage_check": {
            "current": current_key,
            "snapshot": snapshot_key,
        },
    }


def assert_active_feature_snapshot_fresh(
    manifest: dict[str, Any] | None,
    *,
    holding_period_days: int = MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
) -> dict[str, Any]:
    staleness = active_feature_snapshot_staleness(
        manifest,
        holding_period_days=holding_period_days,
    )
    if staleness["stale"]:
        reason = str(staleness.get("stale_reason") or "unknown")
        raise RuntimeError(
            "active model feature snapshot is stale; refresh fxalpha_model_feature_snapshot before training "
            f"(reason={reason})"
        )
    return staleness
