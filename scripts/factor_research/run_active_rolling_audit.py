#!/usr/bin/env python3
"""Run read-only Rolling v2 validation for active FXAlpha factor-library entries.

This is an audit/prep tool. It does not modify the factor registry, quality gate,
or imported factor values.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
QUANTGPT_ROOT = PROJECT_ROOT / "external" / "quantgpt"
if str(QUANTGPT_ROOT) not in sys.path:
    sys.path.insert(0, str(QUANTGPT_ROOT))

from quantgpt.rolling_validator import run_rolling_validation  # noqa: E402


@dataclass
class FactorRow:
    factor_id: str
    name: str
    expression: str
    category: str
    ic_mean: float | None
    icir: float | None
    rank_ic: float | None
    sharpe: float | None
    holding_period_days: int
    metadata: dict[str, Any]


def _load_config() -> dict[str, Any]:
    with (PROJECT_ROOT / "config.yaml").open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except Exception:
        return None
    return out if np.isfinite(out) else None


def _first_float(*values: Any) -> float | None:
    for value in values:
        out = _as_float(value)
        if out is not None:
            return out
    return None


def _load_active_factors(registry_db: Path) -> list[FactorRow]:
    conn = sqlite3.connect(str(registry_db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT factor_id, name, expression, category, ic_mean, icir, rank_ic,
               sharpe, holding_period_days, metadata
        FROM factors
        WHERE status='active'
        ORDER BY created_at, factor_id
        """
    ).fetchall()
    conn.close()

    out: list[FactorRow] = []
    for row in rows:
        metadata_raw = row["metadata"] or "{}"
        try:
            metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else dict(metadata_raw)
        except Exception:
            metadata = {}
        out.append(
            FactorRow(
                factor_id=str(row["factor_id"]),
                name=str(row["name"]),
                expression=str(row["expression"]),
                category=str(row["category"] or ""),
                ic_mean=_as_float(row["ic_mean"]),
                icir=_as_float(row["icir"]),
                rank_ic=_as_float(row["rank_ic"]),
                sharpe=_as_float(row["sharpe"]),
                holding_period_days=int(row["holding_period_days"] or 5),
                metadata=metadata,
            )
        )
    return out


def _load_close_data(stock_dir: Path, start_date: str, end_date: str, cache_path: Path) -> pd.Series:
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        return df.set_index(["stock_code", "trade_date"])["close"].sort_index()

    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    frames: list[pd.DataFrame] = []
    for path in sorted(stock_dir.glob("*.parquet")):
        try:
            df = pd.read_parquet(path, columns=["trade_date", "stock_code", "close"])
        except Exception:
            continue
        if df.empty:
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df[(df["trade_date"] >= start) & (df["trade_date"] <= end)]
        if df.empty:
            continue
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        frames.append(df[["stock_code", "trade_date", "close"]])

    if not frames:
        raise RuntimeError(f"No stock parquet close data loaded from {stock_dir}")

    closes = pd.concat(frames, ignore_index=True)
    closes["trade_date"] = pd.to_datetime(closes["trade_date"]).dt.strftime("%Y-%m-%d")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    closes.to_parquet(cache_path, index=False)
    return closes.set_index(["stock_code", "trade_date"])["close"].sort_index()


def _extract_component_scores(metadata: dict[str, Any]) -> dict[str, float | None]:
    metrics = metadata.get("metrics") if isinstance(metadata.get("metrics"), dict) else {}
    gate = metadata.get("gate_result") if isinstance(metadata.get("gate_result"), dict) else {}
    deep = metadata.get("deep_validation") if isinstance(metadata.get("deep_validation"), dict) else {}
    screening = metadata.get("screening") if isinstance(metadata.get("screening"), dict) else {}
    anti = (
        metadata.get("anti_overfit")
        if isinstance(metadata.get("anti_overfit"), dict)
        else metadata.get("anti_overfit_summary")
        if isinstance(metadata.get("anti_overfit_summary"), dict)
        else {}
    )
    adv = metadata.get("adversarial_validation") if isinstance(metadata.get("adversarial_validation"), dict) else {}
    novelty = (
        metadata.get("novelty_guard")
        if isinstance(metadata.get("novelty_guard"), dict)
        else deep.get("novelty_correlation")
        if isinstance(deep.get("novelty_correlation"), dict)
        else {}
    )
    return {
        "quick_score": _first_float(metrics.get("quick_score"), gate.get("quick_score"), screening.get("quick_score")),
        "current_deep_score": _first_float(metrics.get("deep_score"), gate.get("deep_score"), deep.get("deep_score")),
        "anti_overfit_score": _first_float(anti.get("score"), deep.get("anti_overfit_score")),
        "adversarial_score": _first_float(adv.get("score"), deep.get("adversarial_score")),
        "novelty_score": _first_float(novelty.get("novelty_score"), deep.get("novelty_score")),
    }


def _score_with_rolling(
    quick: float | None,
    anti: float | None,
    rolling: float | None,
    adversarial: float | None,
) -> float | None:
    if None in (quick, anti, rolling, adversarial):
        return None
    score = float(quick) * 0.55 + float(anti) * 0.15 + float(rolling) * 0.20 + float(adversarial) * 0.10
    return round(max(0.0, min(100.0, score)), 1)


def _severity(row: dict[str, Any]) -> str:
    if row.get("rolling_status") != "ok":
        return "missing"
    score = row.get("rolling_score")
    recent_6m_ic = row.get("rolling_6m_ic")
    if score is not None and score < 40:
        return "severe"
    if recent_6m_ic is not None and recent_6m_ic <= 0:
        return "severe"
    if score is not None and score < 55:
        return "weak"
    if "negative_incremental_period" in (row.get("rolling_risk_flags") or []):
        return "watch"
    if score is not None and score < 65:
        return "watch"
    return "ok"


def _build_factor_df(values: pd.Series, closes: pd.Series) -> pd.DataFrame:
    work = pd.DataFrame({"factor_value": pd.to_numeric(values, errors="coerce")})
    work = work.join(closes.rename("close"), how="left")
    work = work.reset_index()
    work["trade_date"] = pd.to_datetime(work["trade_date"])
    work = work.dropna(subset=["factor_value"])
    return work[["trade_date", "stock_code", "factor_value", "close"]]


def _write_outputs(out_dir: Path, results: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, str]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"active_factor_rolling_audit_{stamp}.json"
    csv_path = out_dir / f"active_factor_rolling_audit_{stamp}.csv"
    md_path = out_dir / f"active_factor_rolling_audit_{stamp}.md"

    rolling_scores = [r["rolling_score"] for r in results if isinstance(r.get("rolling_score"), (int, float))]
    proposed_scores = [r["proposed_deep_score_with_rolling"] for r in results if isinstance(r.get("proposed_deep_score_with_rolling"), (int, float))]
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "active_count": len(results),
        "ok_count": sum(1 for r in results if r.get("rolling_status") == "ok"),
        "missing_count": sum(1 for r in results if r.get("rolling_status") != "ok"),
        "severity_counts": {key: sum(1 for r in results if r.get("rolling_severity") == key) for key in ["ok", "watch", "weak", "severe", "missing"]},
        "rolling_score_avg": round(float(np.mean(rolling_scores)), 2) if rolling_scores else None,
        "rolling_score_median": round(float(np.median(rolling_scores)), 2) if rolling_scores else None,
        "rolling_score_min": round(float(np.min(rolling_scores)), 2) if rolling_scores else None,
        "rolling_score_max": round(float(np.max(rolling_scores)), 2) if rolling_scores else None,
        "proposed_deep_score_avg": round(float(np.mean(proposed_scores)), 2) if proposed_scores else None,
        "proposed_deep_pass_count": sum(1 for v in proposed_scores if v >= 80.0),
        "config": config,
    }
    payload = {"summary": summary, "results": results}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    fieldnames = [
        "factor_id", "name", "category", "rolling_status", "rolling_score", "rolling_severity",
        "rolling_grade", "n_periods", "rolling_weighted_ic", "rolling_weighted_std", "rolling_robust_ic",
        "rolling_6m_ic", "rolling_12m_ic", "rolling_24m_ic", "rolling_48m_ic", "rolling_risk_flags",
        "quick_score", "current_deep_score", "anti_overfit_score", "adversarial_score",
        "novelty_score", "proposed_deep_score_with_rolling", "proposed_deep_delta",
        "factor_rows", "error",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    worst = sorted(
        [r for r in results if isinstance(r.get("rolling_score"), (int, float))],
        key=lambda r: r["rolling_score"],
    )[:12]
    best = sorted(
        [r for r in results if isinstance(r.get("rolling_score"), (int, float))],
        key=lambda r: r["rolling_score"],
        reverse=True,
    )[:8]
    lines = [
        "# Active Factor Rolling Audit",
        "",
        f"- Generated at: {summary['generated_at']}",
        f"- Active factors: {summary['active_count']}",
        f"- Rolling ok/missing: {summary['ok_count']} / {summary['missing_count']}",
        f"- Severity counts: {summary['severity_counts']}",
        f"- Rolling score avg/median/min/max: {summary['rolling_score_avg']} / {summary['rolling_score_median']} / {summary['rolling_score_min']} / {summary['rolling_score_max']}",
        f"- Proposed deep score pass count: {summary['proposed_deep_pass_count']} / {len(proposed_scores)}",
        "",
        "## Scoring Policy Used For Simulation",
        "",
        "`0.55*quick + 0.15*anti_overfit + 0.20*rolling + 0.10*adversarial`, capped to 0..100. Novelty remains an admission guard and adds no numeric points.",
        "",
        "## Worst Rolling Scores",
        "",
        "| factor_id | name | rolling | severity | 6m IC | robust IC | proposed_deep |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: |",
    ]
    for r in worst:
        lines.append(
            f"| {r['factor_id']} | {str(r['name'])[:48]} | {r.get('rolling_score')} | {r.get('rolling_severity')} | "
            f"{r.get('rolling_6m_ic')} | {r.get('rolling_robust_ic')} | {r.get('proposed_deep_score_with_rolling')} |"
        )
    lines.extend([
        "",
        "## Best Rolling Scores",
        "",
        "| factor_id | name | rolling | severity | 6m IC | robust IC | proposed_deep |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: |",
    ])
    for r in best:
        lines.append(
            f"| {r['factor_id']} | {str(r['name'])[:48]} | {r.get('rolling_score')} | {r.get('rolling_severity')} | "
            f"{r.get('rolling_6m_ic')} | {r.get('rolling_robust_ic')} | {r.get('proposed_deep_score_with_rolling')} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Limit number of active factors for smoke testing.")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "runtime" / "diagnostics" / "factor_research" / "rolling_audit"))
    args = parser.parse_args()

    cfg = _load_config()
    paths = cfg.get("paths", {}) or {}
    fr_cfg = cfg.get("factor_research", {}) or {}
    rolling_cfg = fr_cfg.get("rolling_validation", {}) or {}
    selection_start = str(fr_cfg.get("default_start_date"))
    selection_end = str(fr_cfg.get("default_end_date"))
    active_values_path = Path(paths.get("factor_data_root", PROJECT_ROOT / "data" / "factors")) / "active_adopted_factor_values.parquet"
    registry_db = Path(paths.get("factor_registry_db", PROJECT_ROOT / "data" / "factors" / "factor_registry.db"))
    stock_dir = Path(paths.get("quantgpt_data_dir", PROJECT_ROOT / "data" / "quantgpt" / "stocks"))
    out_dir = Path(args.output_dir)
    close_cache = out_dir / f"rolling_v2_close_{selection_start}_{selection_end}.parquet"

    factors = _load_active_factors(registry_db)
    if args.limit and args.limit > 0:
        factors = factors[: args.limit]
    expressions = [f.expression for f in factors]
    print(f"Loading active factor values: {active_values_path} columns={len(expressions)}", flush=True)
    values = pd.read_parquet(active_values_path, columns=expressions)
    values = values.sort_index()
    date_index = pd.to_datetime(values.index.get_level_values("trade_date"))
    values = values[(date_index >= pd.Timestamp(selection_start)) & (date_index <= pd.Timestamp(selection_end))]
    values.index = pd.MultiIndex.from_arrays(
        [
            values.index.get_level_values("stock_code").astype(str),
            pd.to_datetime(values.index.get_level_values("trade_date")).strftime("%Y-%m-%d"),
        ],
        names=["stock_code", "trade_date"],
    )
    print(f"Loading close data from {stock_dir}", flush=True)
    closes = _load_close_data(stock_dir, selection_start, selection_end, close_cache)
    closes = closes.reindex(values.index)

    run_config = {
        "selection_start_date": selection_start,
        "selection_end_date": selection_end,
        "rolling_schema_version": str(rolling_cfg.get("schema_version", "rolling_validation_v2")),
        "rolling_score_policy_version": str(rolling_cfg.get("score_policy_version", "rolling_ic_recency_robust_v1")),
        "max_history_months": int(rolling_cfg.get("max_history_months", 48)),
        "min_history_months": int(rolling_cfg.get("min_history_months", 24)),
        "period_weights": list(rolling_cfg.get("period_weights", [0.40, 0.25, 0.15, 0.12, 0.08])),
        "stability_penalty": float(rolling_cfg.get("stability_penalty", 0.25)),
        "rank_ic_full_score": float(rolling_cfg.get("rank_ic_full_score", 0.08)),
        "min_dates_per_6m": int(rolling_cfg.get("min_dates_per_6m", 60)),
        "trailing_horizons_months": list(rolling_cfg.get("trailing_horizons_months", [6, 12, 24, 36, 48])),
        "scoring_policy": "0.55*quick + 0.15*anti + 0.20*rolling + 0.10*adversarial; novelty is an admission guard only",
    }
    results: list[dict[str, Any]] = []
    for i, factor in enumerate(factors, 1):
        print(f"[{i}/{len(factors)}] {factor.factor_id} {factor.name}", flush=True)
        base_scores = _extract_component_scores(factor.metadata)
        row: dict[str, Any] = {
            "factor_id": factor.factor_id,
            "name": factor.name,
            "expression": factor.expression,
            "category": factor.category,
            "holding_period_days": factor.holding_period_days,
            **base_scores,
        }
        try:
            factor_df = _build_factor_df(values[factor.expression], closes)
            row["factor_rows"] = int(len(factor_df))
            rv = run_rolling_validation(
                factor_df,
                holding_period=factor.holding_period_days,
                run_anti_overfit=False,
                max_history_months=run_config["max_history_months"],
                min_history_months=run_config["min_history_months"],
                period_weights=run_config["period_weights"],
                stability_penalty=run_config["stability_penalty"],
                rank_ic_full_score=run_config["rank_ic_full_score"],
                min_dates_per_6m=run_config["min_dates_per_6m"],
                horizons=run_config["trailing_horizons_months"],
            )
            summary = rv.get("summary") if isinstance(rv.get("summary"), dict) else {}
            trailing = rv.get("trailing_horizons") if isinstance(rv.get("trailing_horizons"), dict) else {}
            row.update(
                {
                    "rolling_status": rv.get("status"),
                    "rolling_score": _as_float(rv.get("score")),
                    "rolling_grade": rv.get("grade"),
                    "n_periods": summary.get("n_periods"),
                    "rolling_weighted_ic": _as_float(rv.get("weighted_ic")),
                    "rolling_weighted_std": _as_float(rv.get("weighted_std")),
                    "rolling_robust_ic": _as_float(rv.get("robust_ic")),
                    "rolling_6m_ic": _as_float((trailing.get("6m") or {}).get("rank_ic")),
                    "rolling_12m_ic": _as_float((trailing.get("12m") or {}).get("rank_ic")),
                    "rolling_24m_ic": _as_float((trailing.get("24m") or {}).get("rank_ic")),
                    "rolling_48m_ic": _as_float((trailing.get("48m") or {}).get("rank_ic")),
                    "rolling_risk_flags": rv.get("risk_flags", []),
                    "incremental_periods": rv.get("incremental_periods", []),
                }
            )
            row["proposed_deep_score_with_rolling"] = _score_with_rolling(
                row.get("quick_score"),
                row.get("anti_overfit_score"),
                row.get("rolling_score"),
                row.get("adversarial_score"),
            )
            if row.get("current_deep_score") is not None and row.get("proposed_deep_score_with_rolling") is not None:
                row["proposed_deep_delta"] = round(row["proposed_deep_score_with_rolling"] - row["current_deep_score"], 1)
            else:
                row["proposed_deep_delta"] = None
            row["rolling_severity"] = _severity(row)
        except Exception as exc:
            row.update(
                {
                    "rolling_status": "error",
                    "rolling_score": None,
                    "rolling_severity": "missing",
                    "error": str(exc),
                    "proposed_deep_score_with_rolling": None,
                    "proposed_deep_delta": None,
                }
            )
        results.append(row)

    paths_out = _write_outputs(out_dir, results, run_config)
    print(json.dumps(paths_out, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
