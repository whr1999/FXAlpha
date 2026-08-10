from __future__ import annotations

"""Operator-only sampled comparison of two raw HDF datasets.

This is intentionally outside ``domain``: it is a diagnostic command, not a
production data-foundation dependency.
"""

import argparse
import json
import random
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def compare_raw_datasets(
    left_hdf5: str | Path,
    right_hdf5: str | Path,
    *,
    start_date: str = "2023-06-01",
    sample_size: int = 100,
    seed: int = 20260603,
) -> dict[str, Any]:
    left_hdf5 = Path(left_hdf5).expanduser()
    right_hdf5 = Path(right_hdf5).expanduser()
    left = pd.read_hdf(left_hdf5, "daily").reset_index()
    right = pd.read_hdf(right_hdf5, "daily").reset_index()
    for frame in (left, right):
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame["code"] = frame["code"].astype(str)
        frame[:] = frame.where(pd.notna(frame), np.nan)

    left = left[(left["trade_date"] >= start_date) & left["code"].str.endswith((".SH", ".SZ"))]
    right = right[(right["trade_date"] >= start_date) & right["code"].str.endswith((".SH", ".SZ"))]
    common_codes = sorted(set(left["code"]).intersection(right["code"]))
    sample_codes = random.Random(seed).sample(common_codes, min(sample_size, len(common_codes)))
    left = left[left["code"].isin(sample_codes)]
    right = right[right["code"].isin(sample_codes)]
    keys = ["trade_date", "code"]
    common_cols = sorted((set(left.columns) & set(right.columns)) - set(keys))
    merged = left[keys + common_cols].merge(
        right[keys + common_cols],
        on=keys,
        how="outer",
        suffixes=("_left", "_right"),
        indicator=True,
    )

    field_diffs: dict[str, dict[str, Any]] = {}
    for column in common_cols:
        left_values = merged[f"{column}_left"]
        right_values = merged[f"{column}_right"]
        if pd.api.types.is_numeric_dtype(left_values) and pd.api.types.is_numeric_dtype(right_values):
            equal = (left_values.isna() & right_values.isna()) | np.isclose(
                left_values.astype(float),
                right_values.astype(float),
                rtol=1e-8,
                atol=1e-8,
                equal_nan=False,
            )
        else:
            equal = left_values.astype("string").fillna("__NA__").eq(
                right_values.astype("string").fillna("__NA__")
            )
        diff = merged.loc[~equal, keys + [f"{column}_left", f"{column}_right"]].copy()
        if not diff.empty:
            diff["trade_date"] = diff["trade_date"].astype(str).str[:10]
            field_diffs[column] = {"diff_count": int(len(diff)), "sample": diff.head(10).to_dict(orient="records")}

    return {
        "left_hdf5": str(left_hdf5),
        "right_hdf5": str(right_hdf5),
        "start_date": start_date,
        "sample_size": len(sample_codes),
        "sample_codes": sample_codes,
        "row_status": merged["_merge"].value_counts(dropna=False).to_dict(),
        "diff_field_count": len(field_diffs),
        "field_diffs": field_diffs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare sampled raw HDF5 rows between two datasets.")
    parser.add_argument("left_hdf5")
    parser.add_argument("right_hdf5")
    parser.add_argument("--start-date", default="2023-06-01")
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260603)
    parser.add_argument("--output-json")
    args = parser.parse_args()
    result = compare_raw_datasets(
        args.left_hdf5,
        args.right_hdf5,
        start_date=args.start_date,
        sample_size=args.sample_size,
        seed=args.seed,
    )
    if args.output_json:
        Path(args.output_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
