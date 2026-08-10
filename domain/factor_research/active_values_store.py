from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from domain.runtime_memory import release_process_memory
from domain.factor_research.factor_compute import (
    FACTOR_COMPUTE_SEMANTICS_VERSION,
    NON_ST_FILTER_COLUMNS,
    _compute_factor_from_market_df,
    _load_market_data,
    _qlib_to_bs_instrument,
    _required_market_columns,
    _trim_factor_output,
    _warmup_start_date,
)
from storage.factor_registry import FactorRegistry
from storage.paths import (
    FACTOR_ACTIVE_ADOPTED_VALUES_FILE,
    FACTOR_ACTIVE_ADOPTED_VALUES_MANIFEST,
    FACTOR_DEFAULT_UNIVERSE,
    FACTOR_DEFAULT_HOLDING_PERIOD,
    QUANTGPT_DATA_DIR,
    get_live_factor_research_config,
    get_live_factor_value_default_end_date,
    get_live_factor_value_default_start_date,
)


ACTIVE_VALUES_SCHEMA_VERSION = "active_adopted_factor_values_v3_static_non_st"
NON_ST_ACTIVE_UNIVERSES = {"tradable_non_st", "all_market_non_st"}
ACTIVE_VALUE_AUDIT_ANCHOR = {
    "factor_expression": "NetMfAmountMean10_CloseCost85Corr5_LowAmountRank20",
    "trade_date": "2026-05-29",
    "stock_code": "sz.000001",
    "baseline_date": "2026-07-16",
    "round_digits": 6,
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def parse_factor_metadata(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            return {}
    return metadata if isinstance(metadata, dict) else {}


def active_factor_records(
    *,
    holding_period_days: int | None = FACTOR_DEFAULT_HOLDING_PERIOD,
    registry: FactorRegistry | None = None,
) -> list[dict[str, Any]]:
    registry = registry or FactorRegistry()
    rows = registry.list_active(min_icir=-1e9, holding_period_days=holding_period_days)
    records: list[dict[str, Any]] = []
    for row in rows:
        metadata = parse_factor_metadata(row)
        data_path = str(metadata.get("data_path") or "")
        data_column = str(metadata.get("data_column") or "")
        path = Path(data_path) if data_path else None
        records.append(
            {
                "factor_id": str(row.get("factor_id") or ""),
                "name": str(row.get("name") or ""),
                "expression": str(row.get("expression") or ""),
                "holding_period_days": int(row.get("holding_period_days") or holding_period_days or 0),
                "data_path": data_path,
                "data_column": data_column,
                "data_mtime": path.stat().st_mtime if path and path.exists() else None,
                "data_size": path.stat().st_size if path and path.exists() else None,
            }
        )
    return records


@lru_cache(maxsize=8)
def quantgpt_stock_cache_signature(root: str | Path = QUANTGPT_DATA_DIR) -> dict[str, Any]:
    """Cheap source-data fingerprint for active factor values.

    Factor parquet freshness must move when the QuantGPT stock cache is rebuilt;
    otherwise a model feature snapshot can silently freeze values computed from
    an older price/fundamental universe.
    """
    root_path = Path(root)
    if not root_path.exists():
        return {
            "path": str(root_path),
            "exists": False,
            "file_count": 0,
            "total_size": 0,
            "max_mtime_ns": 0,
            "fingerprint": "",
        }

    parts: list[str] = []
    file_count = 0
    total_size = 0
    max_mtime_ns = 0
    latest_file = ""
    for path in sorted(root_path.glob("*.parquet")):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        file_count += 1
        total_size += int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)
        if mtime_ns > max_mtime_ns:
            max_mtime_ns = mtime_ns
            latest_file = str(path)
        parts.append(f"{path.name}|{stat.st_size}|{mtime_ns}")

    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16] if parts else ""
    return {
        "path": str(root_path),
        "exists": True,
        "file_count": file_count,
        "total_size": int(total_size),
        "max_mtime_ns": int(max_mtime_ns),
        "latest_file": latest_file,
        "fingerprint": digest,
    }


def resolve_active_values_lineage(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    universe: str | None = None,
) -> dict[str, Any]:
    config = get_live_factor_research_config()
    resolved_universe = str(universe or config.get("default_universe") or FACTOR_DEFAULT_UNIVERSE).strip()
    resolved_start = start_date or get_live_factor_value_default_start_date()
    resolved_end = end_date or get_live_factor_value_default_end_date()
    if not resolved_universe:
        resolved_universe = "tradable_non_st"
    source_data_signature = quantgpt_stock_cache_signature()
    return {
        "resolved_universe": resolved_universe,
        "universe": resolved_universe,
        "value_start_date": resolved_start,
        "value_end_date": resolved_end,
        "filter_non_st_before_expression": False,
        "compute_semantics_version": FACTOR_COMPUTE_SEMANTICS_VERSION,
        "source_data_kind": "quantgpt_stock_cache",
        "source_data_fingerprint": source_data_signature.get("fingerprint") or "",
        "source_data_signature": source_data_signature,
    }


def active_registry_fingerprint(records: list[dict[str, Any]], lineage: dict[str, Any] | None = None) -> str:
    lineage = lineage or {}
    parts = [
        ACTIVE_VALUES_SCHEMA_VERSION,
        f"resolved_universe={lineage.get('resolved_universe') or lineage.get('universe') or ''}",
        f"value_start_date={lineage.get('value_start_date') or ''}",
        f"value_end_date={lineage.get('value_end_date') or ''}",
        f"filter_non_st_before_expression={bool(lineage.get('filter_non_st_before_expression'))}",
        f"compute_semantics_version={lineage.get('compute_semantics_version') or FACTOR_COMPUTE_SEMANTICS_VERSION}",
    ]
    for record in sorted(records, key=lambda item: str(item.get("factor_id") or "")):
        parts.append(
            "|".join(
                [
                    str(record.get("factor_id") or ""),
                    str(record.get("expression") or ""),
                    str(record.get("holding_period_days") or ""),
                    str(record.get("data_path") or ""),
                    str(record.get("data_column") or ""),
                    str(record.get("data_mtime") or ""),
                    str(record.get("data_size") or ""),
                ]
            )
        )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def current_active_registry_fingerprint(
    *,
    holding_period_days: int | None = FACTOR_DEFAULT_HOLDING_PERIOD,
    registry: FactorRegistry | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    universe: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    records = active_factor_records(holding_period_days=holding_period_days, registry=registry)
    lineage = resolve_active_values_lineage(start_date=start_date, end_date=end_date, universe=universe)
    return active_registry_fingerprint(records, lineage), records


def _value_series_from_factor_parquet(record: dict[str, Any]) -> tuple[pd.Series, dict[str, Any]]:
    data_path = str(record.get("data_path") or "")
    if not data_path:
        raise RuntimeError(f"{record.get('factor_id')}: missing data_path")
    path = Path(data_path)
    if not path.exists():
        raise RuntimeError(f"{record.get('factor_id')}: missing parquet {path}")
    df = pd.read_parquet(path)
    if df.empty:
        raise RuntimeError(f"{record.get('factor_id')}: empty parquet {path}")

    if isinstance(df.columns, pd.MultiIndex):
        flat_columns = [str(col[-1]) for col in df.columns]
        df = df.copy()
        df.columns = flat_columns
    value_columns = [
        str(col)
        for col in df.columns
        if str(col) not in {"datetime", "instrument", "trade_date", "stock_code", "ts_code"}
    ]
    expected = str(record.get("data_column") or "")
    if expected and expected in df.columns:
        value_col = expected
    elif len(value_columns) == 1:
        value_col = value_columns[0]
    else:
        raise RuntimeError(f"{record.get('factor_id')}: cannot identify value column in {path}")

    frame = df[[value_col]].copy()
    if isinstance(frame.index, pd.MultiIndex):
        names = list(frame.index.names)
        if "datetime" in names and "instrument" in names:
            frame = frame.reset_index()
        elif "trade_date" in names and "stock_code" in names:
            series = frame[value_col].copy()
            series.index = series.index.set_names(["stock_code", "trade_date"])
            return series.astype("float32"), _coverage_summary(series)
    if {"datetime", "instrument"} <= set(frame.columns):
        frame["trade_date"] = pd.to_datetime(frame["datetime"]).dt.normalize()
        frame["stock_code"] = frame["instrument"].map(_qlib_to_bs_instrument)
    elif {"trade_date", "stock_code"} <= set(frame.columns):
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    else:
        raise RuntimeError(f"{record.get('factor_id')}: missing datetime/instrument index in {path}")
    frame = frame[["stock_code", "trade_date", value_col]].dropna(subset=[value_col])
    series = frame.set_index(["stock_code", "trade_date"])[value_col].sort_index()
    return series.astype("float32"), _coverage_summary(series)


def _coverage_summary(series: pd.Series) -> dict[str, Any]:
    if series.empty:
        return {"non_null": 0, "unique_dates": 0, "unique_stocks": 0}
    index = series.index
    return {
        "non_null": int(series.notna().sum()),
        "unique_dates": int(index.get_level_values("trade_date").nunique()) if "trade_date" in index.names else None,
        "unique_stocks": int(index.get_level_values("stock_code").nunique()) if "stock_code" in index.names else None,
    }


def _write_factor_parquet(record: dict[str, Any], factor_df: pd.DataFrame, run_id: str) -> dict[str, Any]:
    data_path = str(record.get("data_path") or "")
    data_column = str(record.get("data_column") or "")
    if not data_path:
        raise RuntimeError(f"{record.get('factor_id')}: missing data_path")
    if not data_column:
        raise RuntimeError(f"{record.get('factor_id')}: missing data_column")
    path = Path(data_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = factor_df.copy()
    out.columns = pd.MultiIndex.from_product([["feature"], [data_column]])
    tmp_path = path.with_name(f".tmp.{run_id}.{path.name}")
    out.to_parquet(tmp_path, engine="pyarrow")
    os.replace(tmp_path, path)
    stat = path.stat()
    updated = dict(record)
    updated["data_mtime"] = stat.st_mtime
    updated["data_size"] = stat.st_size
    return updated


def _series_from_computed_factor(
    record: dict[str, Any],
    market_df: pd.DataFrame,
    *,
    start_date: str,
    end_date: str,
    run_id: str,
    persist_factor_parquet: bool,
) -> tuple[pd.Series, dict[str, Any], dict[str, Any]]:
    expression = str(record.get("expression") or "")
    if not expression:
        raise RuntimeError(f"{record.get('factor_id')}: missing expression")
    factor_df = _compute_factor_from_market_df(market_df, expression)
    factor_df = _trim_factor_output(factor_df, start_date, end_date)
    if factor_df.empty:
        raise RuntimeError(f"{record.get('factor_id')}: no usable computed values")
    value_col = factor_df.columns[0]
    adopted = factor_df.reset_index().rename(columns={"datetime": "trade_date"})
    adopted["stock_code"] = adopted["instrument"].map(_qlib_to_bs_instrument)
    adopted["trade_date"] = pd.to_datetime(adopted["trade_date"]).dt.normalize()
    adopted = adopted[["stock_code", "trade_date", value_col]].dropna(subset=[value_col])
    series = adopted.set_index(["stock_code", "trade_date"])[value_col].sort_index().astype("float32")
    updated_record = _write_factor_parquet(record, factor_df, run_id) if persist_factor_parquet else dict(record)
    return series, _coverage_summary(series), updated_record


def _build_audit_anchor(values: pd.DataFrame, lineage: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    anchor = dict(ACTIVE_VALUE_AUDIT_ANCHOR)
    target = str(anchor["factor_expression"])
    matched_record = next(
        (
            record
            for record in records
            if target
            in {
                str(record.get("factor_id") or ""),
                str(record.get("name") or ""),
                str(record.get("expression") or ""),
                str(record.get("data_column") or ""),
            }
        ),
        None,
    )
    expression = str((matched_record or {}).get("expression") or target)
    date = pd.Timestamp(anchor["trade_date"]).normalize()
    key = (anchor["stock_code"], date)
    stored_value = None
    if expression in values.columns and key in values.index:
        raw = values.loc[key, expression]
        if isinstance(raw, pd.Series):
            raw = raw.iloc[0] if not raw.empty else None
        if pd.notna(raw):
            stored_value = round(float(raw), int(anchor["round_digits"]))
    anchor.update(
        {
            "resolved_universe": lineage.get("resolved_universe"),
            "matched_factor_id": (matched_record or {}).get("factor_id"),
            "matched_factor_name": (matched_record or {}).get("name"),
            "matched_expression": expression if matched_record else None,
            "stored_value": stored_value,
            "passed": lineage.get("resolved_universe") in NON_ST_ACTIVE_UNIVERSES and stored_value is not None,
        }
    )
    return anchor


def _atomic_write_parquet(df: pd.DataFrame, path: Path, run_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".tmp.{run_id}.{path.name}")
    df.to_parquet(tmp_path, engine="pyarrow")
    os.replace(tmp_path, path)


def _atomic_write_json(payload: dict[str, Any], path: Path, run_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".tmp.{run_id}.{path.name}")
    tmp_path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _multiindex_date_bounds(index: pd.Index) -> tuple[pd.Timestamp | None, pd.Timestamp | None, int]:
    if not isinstance(index, pd.MultiIndex) or len(index) == 0:
        return None, None, 0
    if "trade_date" in index.names:
        raw_dates = index.get_level_values("trade_date")
    elif "datetime" in index.names:
        raw_dates = index.get_level_values("datetime")
    else:
        raw_dates = index.get_level_values(-1)
    dates = pd.to_datetime(raw_dates).normalize()
    return dates.min(), dates.max(), int(dates.nunique())


def _parquet_date_bounds(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"start_date": "", "end_date": "", "rows": 0, "unique_dates": 0}
    # ``pd.read_parquet(columns=[])`` still rebuilds every pandas index column.
    # The production active-values store has millions of rows, so a status read
    # used to materialize the full stock-code/date MultiIndex merely to obtain
    # four small metadata values. Read only the date index one row group at a
    # time instead; row count comes directly from immutable Parquet metadata.
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(path)
    date_column = next(
        (name for name in ("trade_date", "datetime") if name in parquet.schema_arrow.names),
        None,
    )
    if date_column is None:
        # Compatibility fallback for an old or external artifact whose pandas
        # index field has a non-standard name.
        df = pd.read_parquet(path, columns=[])
        start, end, unique_dates = _multiindex_date_bounds(df.index)
        rows = int(len(df))
    else:
        observed_dates: set[pd.Timestamp] = set()
        for row_group in range(parquet.metadata.num_row_groups):
            values = parquet.read_row_group(row_group, columns=[date_column]).column(date_column)
            for raw in pc.unique(values).to_pylist():
                if raw is not None:
                    observed_dates.add(pd.Timestamp(raw).normalize())
        start = min(observed_dates) if observed_dates else None
        end = max(observed_dates) if observed_dates else None
        unique_dates = len(observed_dates)
        rows = int(parquet.metadata.num_rows)
    return {
        "start_date": str(start.date()) if start is not None else "",
        "end_date": str(end.date()) if end is not None else "",
        "rows": rows,
        "unique_dates": unique_dates,
    }


def build_active_values_store(
    *,
    holding_period_days: int | None = FACTOR_DEFAULT_HOLDING_PERIOD,
    output_path: Path = FACTOR_ACTIVE_ADOPTED_VALUES_FILE,
    manifest_path: Path = FACTOR_ACTIVE_ADOPTED_VALUES_MANIFEST,
    sync_quantgpt: bool = True,
    registry: FactorRegistry | None = None,
    run_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    universe: str | None = None,
    source_mode: str = "parquet",
    persist_factor_parquet: bool = True,
) -> dict[str, Any]:
    run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    lineage = resolve_active_values_lineage(start_date=start_date, end_date=end_date, universe=universe)
    resolved_start_date = str(lineage["value_start_date"])
    resolved_end_date = str(lineage["value_end_date"])
    resolved_universe = str(lineage["resolved_universe"])
    records = active_factor_records(holding_period_days=holding_period_days, registry=registry)
    if not records:
        raise RuntimeError("no active factors available for active-value store")

    expressions = [record["expression"] for record in records]
    data_paths = [record["data_path"] for record in records if record.get("data_path")]
    data_columns = [record["data_column"] for record in records if record.get("data_column")]
    duplicate_expressions = sorted({item for item in expressions if expressions.count(item) > 1})
    duplicate_data_paths = sorted({item for item in data_paths if data_paths.count(item) > 1})
    duplicate_data_columns = sorted({item for item in data_columns if data_columns.count(item) > 1})
    if duplicate_expressions or duplicate_data_paths or duplicate_data_columns:
        raise RuntimeError(
            "active factor pointer collision: "
            f"duplicate_expressions={duplicate_expressions[:3]}, "
            f"duplicate_data_paths={duplicate_data_paths[:3]}, "
            f"duplicate_data_columns={duplicate_data_columns[:3]}"
        )

    columns: list[pd.Series] = []
    factor_summaries: list[dict[str, Any]] = []
    built_records: list[dict[str, Any]] = []
    market_df: pd.DataFrame | None = None
    if source_mode == "compute":
        load_start_date = _warmup_start_date(resolved_start_date)
        required_columns = _required_market_columns(expressions)
        if resolved_universe in NON_ST_ACTIVE_UNIVERSES:
            required_columns = set(required_columns) | NON_ST_FILTER_COLUMNS
        market_df = _load_market_data(
            start_date=load_start_date,
            end_date=resolved_end_date,
            required_columns=required_columns,
            filter_non_st=False,
        )
        if market_df.empty:
            raise RuntimeError(
                f"no market data loaded for active-value store universe={resolved_universe} "
                f"window={resolved_start_date}..{resolved_end_date}"
            )
    for record in records:
        if source_mode == "compute":
            assert market_df is not None
            series, coverage, built_record = _series_from_computed_factor(
                record,
                market_df,
                start_date=resolved_start_date,
                end_date=resolved_end_date,
                run_id=run_id,
                persist_factor_parquet=persist_factor_parquet,
            )
        elif source_mode == "parquet":
            series, coverage = _value_series_from_factor_parquet(record)
            built_record = dict(record)
        else:
            raise RuntimeError(f"unsupported active-value source_mode={source_mode}")
        if series.empty:
            raise RuntimeError(f"{record.get('factor_id')}: no usable values")
        series.name = record["expression"]
        columns.append(series)
        factor_summaries.append({**built_record, "coverage": coverage})
        built_records.append(built_record)

    values = pd.concat(columns, axis=1, join="outer").sort_index()
    # Per-factor parquet files may legitimately contain a newer tail from a
    # later feature refresh.  The active wide store is a pinned value-window
    # asset, so never let those rows leak past the resolved task contract.
    value_dates = pd.to_datetime(values.index.get_level_values("trade_date")).normalize()
    window_mask = (
        (value_dates >= pd.Timestamp(resolved_start_date).normalize())
        & (value_dates <= pd.Timestamp(resolved_end_date).normalize())
    )
    values = values.loc[window_mask]
    if values.empty:
        raise RuntimeError("active value store is empty after concatenation")
    if len(values.columns) != len(records):
        raise RuntimeError(f"active value column mismatch: {len(values.columns)} != {len(records)}")
    actual_start, actual_end, actual_unique_dates = _multiindex_date_bounds(values.index)
    if actual_end is None:
        raise RuntimeError("active value store has no date index coverage")
    if actual_end < pd.Timestamp(resolved_end_date).normalize():
        raise RuntimeError(
            "active value store coverage is short of requested end date; "
            f"actual_end_date={actual_end.date()}, requested_end_date={resolved_end_date}, "
            f"source_mode={source_mode}. Run active-values tail refresh before rebuilding model features."
        )

    fingerprint = active_registry_fingerprint(built_records, lineage)
    audit_anchor = _build_audit_anchor(values, lineage, built_records)
    anchor_expression = str(audit_anchor.get("matched_expression") or audit_anchor.get("factor_expression") or "")
    if (
        resolved_universe in NON_ST_ACTIVE_UNIVERSES
        and anchor_expression in values.columns
        and audit_anchor.get("passed") is not True
    ):
        raise RuntimeError(f"active value audit anchor failed: {audit_anchor}")
    manifest = {
        "schema_version": ACTIVE_VALUES_SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": run_id,
        "holding_period_days": holding_period_days,
        "source_mode": source_mode,
        "resolved_universe": resolved_universe,
        "universe": resolved_universe,
        "value_start_date": resolved_start_date,
        "value_end_date": resolved_end_date,
        "actual_start_date": str(actual_start.date()) if actual_start is not None else "",
        "actual_end_date": str(actual_end.date()) if actual_end is not None else "",
        "actual_unique_dates": actual_unique_dates,
        "output_window_enforced": True,
        "filter_non_st_before_expression": bool(lineage["filter_non_st_before_expression"]),
        "compute_semantics_version": FACTOR_COMPUTE_SEMANTICS_VERSION,
        "source_data_kind": lineage.get("source_data_kind"),
        "source_data_fingerprint": lineage.get("source_data_fingerprint"),
        "source_data_signature": lineage.get("source_data_signature"),
        "registry_fingerprint": fingerprint,
        "factor_count": len(records),
        "column_count": len(values.columns),
        "shape": [int(values.shape[0]), int(values.shape[1])],
        "path": str(output_path),
        "quantgpt_path": str(output_path),
        "quantgpt_sync_mode": "shared_canonical_path",
        "audit_anchor": audit_anchor,
        "factor_records": factor_summaries,
        "integrity": {
            "active_count": len(records),
            "column_count_matches": len(values.columns) == len(records),
            "duplicate_expressions": duplicate_expressions,
            "duplicate_data_paths": duplicate_data_paths,
            "duplicate_data_columns": duplicate_data_columns,
            "missing_expressions": sorted(set(expressions) - set(values.columns)),
            "extra_columns": sorted(set(values.columns) - set(expressions)),
        },
    }

    _atomic_write_parquet(values, output_path, run_id)
    _atomic_write_json(manifest, manifest_path, run_id)
    del values
    del columns
    release_process_memory("active_values_store_build_completed")
    return manifest


def load_active_values_manifest(path: Path = FACTOR_ACTIVE_ADOPTED_VALUES_MANIFEST) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def active_values_store_summary(
    *,
    holding_period_days: int | None = FACTOR_DEFAULT_HOLDING_PERIOD,
    output_path: Path = FACTOR_ACTIVE_ADOPTED_VALUES_FILE,
    manifest_path: Path = FACTOR_ACTIVE_ADOPTED_VALUES_MANIFEST,
    registry: FactorRegistry | None = None,
) -> dict[str, Any]:
    lineage = resolve_active_values_lineage()
    fingerprint, records = current_active_registry_fingerprint(
        holding_period_days=holding_period_days,
        registry=registry,
    )
    manifest = load_active_values_manifest(manifest_path) or {}
    actual_coverage = _parquet_date_bounds(output_path)
    manifest_fingerprint = str(manifest.get("registry_fingerprint") or "")
    manifest_universe = str(manifest.get("resolved_universe") or manifest.get("universe") or "")
    manifest_start = str(manifest.get("value_start_date") or "")
    manifest_end = str(manifest.get("value_end_date") or "")
    manifest_semantics = str(manifest.get("compute_semantics_version") or "")
    manifest_source_fingerprint = str(manifest.get("source_data_fingerprint") or "")
    current_source_fingerprint = str(lineage.get("source_data_fingerprint") or "")
    source_data_untracked = bool(current_source_fingerprint and not manifest_source_fingerprint)
    source_data_mismatch = bool(
        manifest_source_fingerprint
        and current_source_fingerprint
        and manifest_source_fingerprint != current_source_fingerprint
    )
    lineage_mismatch = bool(
        manifest_universe != str(lineage["resolved_universe"])
        or manifest_start != str(lineage["value_start_date"])
        or manifest_end != str(lineage["value_end_date"])
        or bool(manifest.get("filter_non_st_before_expression")) != bool(lineage["filter_non_st_before_expression"])
        or manifest_semantics != FACTOR_COMPUTE_SEMANTICS_VERSION
    )
    stale_reasons: list[str] = []
    exists = output_path.exists()
    manifest_exists = manifest_path.exists()
    if not exists:
        stale_reasons.append("active_values_file_missing")
    if not manifest_exists:
        stale_reasons.append("active_values_manifest_missing")
    if manifest_exists and not manifest_fingerprint:
        stale_reasons.append("manifest_registry_fingerprint_missing")
    if manifest_fingerprint and manifest_fingerprint != fingerprint:
        stale_reasons.append("registry_fingerprint_mismatch")
    if lineage_mismatch:
        stale_reasons.append("lineage_mismatch")
    actual_end = str(actual_coverage.get("end_date") or "")
    if exists and actual_end and actual_end < str(lineage["value_end_date"]):
        stale_reasons.append("active_values_actual_end_date_short")
    stale_reason = stale_reasons[0] if stale_reasons else ""
    stale_message = ""
    if manifest_fingerprint and manifest_fingerprint != fingerprint:
        stale_message = f"active values stale because registry changed from {manifest_fingerprint} to {fingerprint}"
    elif stale_reason:
        stale_message = stale_reason
    if "active_values_actual_end_date_short" in stale_reasons:
        stale_message = (
            f"active values stale because actual parquet coverage ends at {actual_end} "
            f"before required {lineage['value_end_date']}; run source_mode=tail"
        )
    return {
        "path": str(output_path),
        "manifest_path": str(manifest_path),
        "exists": exists,
        "manifest_exists": manifest_exists,
        "factor_count": manifest.get("factor_count"),
        "column_count": manifest.get("column_count"),
        "latest_generated_at": manifest.get("generated_at"),
        "resolved_universe": manifest_universe or lineage["resolved_universe"],
        "universe": manifest_universe or lineage["resolved_universe"],
        "value_start_date": manifest_start or lineage["value_start_date"],
        "value_end_date": manifest_end or lineage["value_end_date"],
        "actual_start_date": actual_coverage.get("start_date") or manifest.get("actual_start_date") or "",
        "actual_end_date": actual_coverage.get("end_date") or manifest.get("actual_end_date") or "",
        "actual_rows": actual_coverage.get("rows") or 0,
        "actual_unique_dates": actual_coverage.get("unique_dates") or manifest.get("actual_unique_dates") or 0,
        "filter_non_st_before_expression": manifest.get(
            "filter_non_st_before_expression",
            lineage["filter_non_st_before_expression"],
        ),
        "compute_semantics_version": manifest_semantics or lineage["compute_semantics_version"],
        "source_data_kind": manifest.get("source_data_kind") or lineage.get("source_data_kind"),
        "source_data_fingerprint": manifest_source_fingerprint,
        "current_source_data_fingerprint": current_source_fingerprint,
        "source_data_signature": manifest.get("source_data_signature") or {},
        "current_source_data_signature": lineage.get("source_data_signature") or {},
        "source_data_untracked": source_data_untracked,
        "source_data_mismatch": source_data_mismatch,
        "audit_anchor": manifest.get("audit_anchor", {}),
        "registry_fingerprint": fingerprint,
        "current_registry_fingerprint": fingerprint,
        "requested_registry_fingerprint": fingerprint,
        "built_registry_fingerprint": manifest_fingerprint,
        "manifest_registry_fingerprint": manifest_fingerprint,
        "active_count": len(records),
        "stale": bool(stale_reasons),
        "stale_reason": stale_reason,
        "stale_message": stale_message,
        "stale_reasons": stale_reasons,
        "active_values_status": "stale" if stale_reasons else "ready",
        "safe_to_freeze_feature_set": not bool(stale_reasons),
        "model_snapshot_refresh_required": bool(stale_reasons),
        "lineage_mismatch": lineage_mismatch,
        "last_error": "",
    }
