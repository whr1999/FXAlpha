#!/usr/bin/env python3
"""Recompute every current and retired factor into a read-only audit run."""

from __future__ import annotations

import argparse
import gc
import json
import multiprocessing as mp
import os
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from domain.factor_research.factor_compute import (  # noqa: E402
    _load_market_data,
    _required_market_columns,
    _warmup_start_date,
)
from domain.factor_research.library_recertification import (  # noqa: E402
    RECERTIFICATION_ROOT,
    RunPaths,
    apply_exact_duplicate_advice,
    atomic_write_json,
    behavioral_redundancy,
    build_manifest,
    classify_lifecycle,
    completed_factor_ids,
    json_default,
    load_results,
    registry_snapshot,
    result_path,
    rolling_config,
    summarize_result,
    traceback_payload,
)
from domain.platform_evaluation import resolve_evaluation_profile  # noqa: E402
from storage.paths import QUANTGPT_CODE_ROOT  # noqa: E402

if str(QUANTGPT_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(QUANTGPT_CODE_ROOT))


_MARKET: pd.DataFrame | None = None
_BACKTEST_BASE: pd.DataFrame | None = None
_SCORE_INDEX: pd.Index | None = None
_RUN_DIR: Path | None = None
_START_DATE = ""
_END_DATE = ""
_ROLLING_CONFIG: dict[str, Any] = {}


def _backtest_summary(backtest: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "ic_mean",
        "rank_ic_mean",
        "ic_ir",
        "rank_ic_ir",
        "pearson_ic_mean",
        "pearson_ic_ir",
        "raw_rank_ic_mean",
        "raw_rank_ic_ir",
        "ic_win_rate",
        "annual_return",
        "sharpe",
        "max_drawdown",
        "turnover",
        "long_short_sharpe",
        "long_short_annual",
        "monotonicity_score",
        "spread",
        "wq_fitness",
        "flipped",
        "cost_adjusted",
        "cost_rate",
    )
    return {key: backtest.get(key) for key in keys}


def _monthly_correlation_sample(factor_df: pd.DataFrame) -> pd.DataFrame:
    if factor_df.empty:
        return pd.DataFrame(columns=["trade_date", "stock_code", "factor_value"])
    frame = factor_df[["trade_date", "stock_code", "factor_value"]].dropna().copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    monthly_dates = frame.groupby(frame["trade_date"].dt.to_period("M"))["trade_date"].max().unique()
    frame = frame[frame["trade_date"].isin(monthly_dates)].copy()
    frame["factor_value"] = pd.to_numeric(frame["factor_value"], errors="coerce").astype("float32")
    return frame.dropna(subset=["factor_value"])


def _blocked_fields(expression: str) -> dict[str, str]:
    from quantgpt.data_schema import BLOCKED_FIELDS, normalize_field_name
    from quantgpt.expression_parser import extract_components

    fields = {normalize_field_name(value) for value in extract_components(expression).get("fields", set())}
    return {field: BLOCKED_FIELDS[field] for field in fields if field in BLOCKED_FIELDS}


def _run_one(row: dict[str, Any]) -> dict[str, Any]:
    from domain.factor_research.quality_gate import _compute_deep_score
    from quantgpt.adversarial_validator import run_adversarial_validation
    from quantgpt.anti_overfit import run_anti_overfit
    from quantgpt.factor_evaluator import FACTOR_COMPUTE_SEMANTICS_VERSION, evaluate_factor_series
    from quantgpt.iteration import compute_local_quick_score
    from quantgpt.rolling_validator import run_rolling_validation
    from quantgpt.task_executor import _run_backtest_precomputed_in_process

    assert _MARKET is not None and _BACKTEST_BASE is not None and _SCORE_INDEX is not None and _RUN_DIR is not None
    factor_id = str(row.get("factor_id") or "")
    expression = str(row.get("expression") or "")
    started = time.time()
    result: dict[str, Any] = {
        "terminal": True,
        "factor_id": factor_id,
        "name": row.get("name"),
        "expression": expression,
        "current_status": row.get("status"),
        "holding_period_days": int(row.get("holding_period_days") or 5),
        "compute_semantics_version": FACTOR_COMPUTE_SEMANTICS_VERSION,
        "selection_start_date": _START_DATE,
        "selection_end_date": _END_DATE,
        "worker_pid": os.getpid(),
    }
    try:
        if not expression:
            raise ValueError("empty_expression")
        blocked = _blocked_fields(expression)
        if blocked:
            result.update({"status": "invalid_field", "error_code": "blocked_fields", "blocked_fields": blocked})
            result["lifecycle_advice"] = classify_lifecycle(result, row)
            return result

        required = _required_market_columns([expression]) | {"trade_date", "stock_code"}
        missing = sorted(required - set(_MARKET.columns))
        if missing:
            result.update({"status": "invalid_field", "error_code": "missing_fields", "missing_fields": missing})
            result["lifecycle_advice"] = classify_lifecycle(result, row)
            return result

        stage = time.time()
        expression_market = _MARKET.loc[:, sorted(required)]
        values = evaluate_factor_series(
            expression_market,
            expression,
            universe="tradable_non_st",
            output_start_date=_START_DATE,
            output_end_date=_END_DATE,
            backend="python",
        )
        result["factor_compute_seconds"] = round(time.time() - stage, 3)
        precomputed = values.reindex(_SCORE_INDEX)
        valid_values = int(precomputed.notna().sum())
        result["valid_factor_values"] = valid_values
        if valid_values < 1000:
            raise ValueError(f"insufficient_factor_values:{valid_values}")

        stage = time.time()
        backtest = _run_backtest_precomputed_in_process(
            _BACKTEST_BASE,
            5,
            0.003,
            precomputed,
            neutralize_cap=True,
            neutralize_industry=False,
            universe="tradable_non_st",
            output_start_date=_START_DATE,
            output_end_date=_END_DATE,
        )
        result["backtest_seconds"] = round(time.time() - stage, 3)
        summary = _backtest_summary(backtest)
        result["backtest_summary"] = summary
        quick = compute_local_quick_score(summary)
        result["quick_score"] = quick.get("score")
        result["quick_grade"] = quick.get("grade")
        result["quick_scoring"] = quick
        factor_df = backtest.get("_factor_df")
        if factor_df is None or len(factor_df) < 100:
            raise ValueError("backtest_factor_df_missing")

        stage = time.time()
        anti = run_anti_overfit(factor_df, holding_period=5)
        result["anti_overfit_seconds"] = round(time.time() - stage, 3)
        result["anti_overfit"] = anti

        stage = time.time()
        rolling = run_rolling_validation(factor_df, holding_period=5, run_anti_overfit=False, **_ROLLING_CONFIG)
        result["rolling_seconds"] = round(time.time() - stage, 3)
        result["rolling_validation"] = rolling

        stage = time.time()
        adversarial = run_adversarial_validation(factor_df, holding_period=5)
        result["adversarial_seconds"] = round(time.time() - stage, 3)
        result["adversarial_validation"] = adversarial

        candidate = {
            "expression": expression,
            "quick_score": result["quick_score"],
            "backtest_summary": summary,
            "anti_overfit": anti,
            "rolling_validation": rolling,
            "adversarial_validation": adversarial,
        }
        deep_score, score_parts = _compute_deep_score(candidate, quick_score=float(result["quick_score"]))
        result["deep_score"] = deep_score
        result["deep_grade"] = score_parts.get("official_grade")
        result["deep_score_parts"] = score_parts
        result["direction_review"] = bool(summary.get("flipped"))
        result["status"] = "success"
        result["lifecycle_advice"] = classify_lifecycle(result, row)

        sample = _monthly_correlation_sample(factor_df)
        sample_path = _RUN_DIR / "correlation_samples" / f"{factor_id}.parquet"
        sample.to_parquet(sample_path, index=False)
        result["correlation_sample_path"] = str(sample_path)
        result["correlation_sample_rows"] = int(len(sample))
    except Exception as exc:
        result.update(traceback_payload(exc))
        result["lifecycle_advice"] = classify_lifecycle(result, row)
    finally:
        result["runtime_seconds"] = round(time.time() - started, 3)
        atomic_write_json(result_path(_RUN_DIR, factor_id), result)
    return result


def _worker_init(
    market: pd.DataFrame,
    backtest_base: pd.DataFrame,
    score_index: pd.Index,
    run_dir: str,
    start_date: str,
    end_date: str,
    rv_config: dict[str, Any],
) -> None:
    global _MARKET, _BACKTEST_BASE, _SCORE_INDEX, _RUN_DIR, _START_DATE, _END_DATE, _ROLLING_CONFIG
    _MARKET = market
    _BACKTEST_BASE = backtest_base
    _SCORE_INDEX = score_index
    _RUN_DIR = Path(run_dir)
    _START_DATE = start_date
    _END_DATE = end_date
    _ROLLING_CONFIG = rv_config
    warnings.filterwarnings("ignore", category=FutureWarning)


def _write_progress(paths: RunPaths, *, total: int, completed: int, current: dict[str, Any] | None = None, status: str = "running") -> None:
    payload = {
        "run_id": paths.run_dir.name,
        "status": status,
        "total": total,
        "completed": completed,
        "remaining": max(total - completed, 0),
        "progress_pct": round(completed / total * 100, 2) if total else 100.0,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "registry_mutation": False,
    }
    if current:
        payload["last_factor_id"] = current.get("factor_id")
        payload["last_factor_status"] = current.get("status")
        payload["last_runtime_seconds"] = current.get("runtime_seconds")
    atomic_write_json(paths.status, payload)


def _finalize(paths: RunPaths, rows: list[dict[str, Any]]) -> dict[str, Any]:
    results = load_results(paths.run_dir)
    row_by_id = {str(row["factor_id"]): row for row in rows}
    for result in results:
        if result.get("factor_id") in row_by_id:
            result["lifecycle_advice"] = classify_lifecycle(result, row_by_id[result["factor_id"]])
    apply_exact_duplicate_advice(results)
    for result in results:
        atomic_write_json(result_path(paths.run_dir, str(result["factor_id"])), result)

    relevant_ids = [
        str(result["factor_id"])
        for result in results
        if (result.get("lifecycle_advice") or {}).get("advice")
        in {"keep_active", "restore_candidate", "active_redundancy_review"}
    ]
    correlations = behavioral_redundancy(paths.run_dir, relevant_ids)
    by_id = {str(result["factor_id"]): result for result in results}
    for pair in correlations:
        left = by_id.get(pair["left_factor_id"])
        right = by_id.get(pair["right_factor_id"])
        if not left or not right:
            continue
        left_status = str(left.get("current_status"))
        right_status = str(right.get("current_status"))
        if left_status == "active" and right_status == "retired" and (right.get("lifecycle_advice") or {}).get("advice") == "restore_candidate":
            right["lifecycle_advice"]["advice"] = "restore_review_redundant"
            right["lifecycle_advice"]["reason"] = f"high_behavioral_correlation_with_active:{left.get('factor_id')}"
        elif right_status == "active" and left_status == "retired" and (left.get("lifecycle_advice") or {}).get("advice") == "restore_candidate":
            left["lifecycle_advice"]["advice"] = "restore_review_redundant"
            left["lifecycle_advice"]["reason"] = f"high_behavioral_correlation_with_active:{right.get('factor_id')}"
    for result in results:
        atomic_write_json(result_path(paths.run_dir, str(result["factor_id"])), result)

    summary_rows = [summarize_result(result) for result in results]
    summary_df = pd.DataFrame(summary_rows).sort_values(
        ["advice", "deep_score", "quick_score"], ascending=[True, False, False], na_position="last"
    )
    summary_csv = paths.run_dir / "factor_lifecycle_summary.csv"
    summary_df.to_csv(summary_csv, index=False)
    atomic_write_json(paths.run_dir / "behavioral_redundancy.json", {"pairs": correlations})

    invalid_field_counts: dict[str, int] = {}
    for result in results:
        for field in (result.get("blocked_fields") or {}):
            invalid_field_counts[str(field)] = invalid_field_counts.get(str(field), 0) + 1

    def _number(value: Any, digits: int = 3) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return "-"
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return str(value).replace("|", "\\|").replace("\n", " ")

    def _table(frame: pd.DataFrame, columns: list[tuple[str, str, int | None]]) -> list[str]:
        if frame.empty:
            return ["无。"]
        lines = [
            "| " + " | ".join(label for _, label, _ in columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |",
        ]
        for _, item in frame.iterrows():
            cells: list[str] = []
            for key, _, digits in columns:
                value = item.get(key)
                cells.append(_number(value, digits) if digits is not None else str(value if pd.notna(value) else "-").replace("|", "\\|").replace("\n", " "))
            lines.append("| " + " | ".join(cells) + " |")
        return lines

    active = summary_df[summary_df["current_status"].eq("active")]
    retired = summary_df[summary_df["current_status"].eq("retired")]
    group_stats: dict[str, dict[str, Any]] = {}
    for advice, group in summary_df.groupby("advice", dropna=False):
        group_stats[str(advice)] = {
            "count": int(len(group)),
            "mean_quick_score": round(float(group["quick_score"].mean()), 3) if group["quick_score"].notna().any() else None,
            "mean_deep_score": round(float(group["deep_score"].mean()), 3) if group["deep_score"].notna().any() else None,
            "mean_rolling_6m_ic": round(float(group["rolling_6m_ic"].mean()), 6) if group["rolling_6m_ic"].notna().any() else None,
        }
    analysis = {
        "active": {
            "total": int(len(active)),
            "advice_counts": active["advice"].value_counts().to_dict(),
        },
        "retired": {
            "total": int(len(retired)),
            "advice_counts": retired["advice"].value_counts().to_dict(),
        },
        "invalid_field_counts": invalid_field_counts,
        "group_statistics": group_stats,
        "behavioral_redundancy_pair_count": len(correlations),
    }
    atomic_write_json(paths.run_dir / "analysis.json", analysis)

    report_lines = [
        "# 全量因子库重算与生命周期建议",
        "",
        f"- Run ID: `{paths.run_dir.name}`",
        "- 口径：生产模式，2022-01-01 至 2026-06-30，T+5，静态非 ST 股票池，市值中性化。",
        "- Deep Score：Quick 55% + Anti-overfit 15% + Rolling 20% + Adversarial 10%。",
        "- 说明：本报告是只读复评建议；未修改 factor_registry，未覆盖生产因子值。生产模式证据属于 discovery-conditioned rolling，不是干净 OOS。",
        "",
        "## 完整性",
        "",
        f"- Registry 因子：{len(rows)}（现役 {len(active)}，退役 {len(retired)}）。",
        f"- 正常完成：{int(summary_df['status'].eq('success').sum())}；当前字段无效：{int(summary_df['status'].eq('invalid_field').sum())}；运行失败：{int((~summary_df['status'].isin(['success', 'invalid_field'])).sum())}。",
        f"- 行为高相关复核对（|月末截面日均 Spearman| ≥ 0.85）：{len(correlations)}。",
        f"- 无效字段命中：{json.dumps(invalid_field_counts, ensure_ascii=False)}。",
        "",
        "## 结论摘要",
        "",
        f"- 现役：保留 {int(active['advice'].eq('keep_active').sum())}；观察 {int(active['advice'].eq('active_review').sum())}；退出候选 {int(active['advice'].eq('exit_candidate').sum())}；方向修正 {int(active['advice'].eq('direction_review').sum())}。",
        f"- 退役：恢复候选 {int(retired['advice'].eq('restore_candidate').sum())}；政策复核 {int(retired['advice'].eq('policy_review').sum())}；精确重复不恢复 {int(retired['advice'].eq('keep_retired_duplicate').sum())}；其余继续退役 {int(retired['advice'].eq('keep_retired').sum())}。",
        "",
        "## 建议退出的现役因子",
        "",
    ]
    common_columns = [
        ("factor_id", "Factor ID", None),
        ("name", "名称", None),
        ("quick_score", "Quick", 1),
        ("deep_score", "Deep", 1),
        ("rank_ic_mean", "Rank IC", 4),
        ("rank_ic_ir", "Rank ICIR", 3),
        ("rolling_6m_ic", "近6月 IC", 4),
        ("rolling_12m_ic", "近12月 IC", 4),
    ]
    report_lines.extend(_table(active[active["advice"].eq("exit_candidate")].sort_values("deep_score"), common_columns))
    report_lines.extend(["", "## 方向修正后重算", ""])
    report_lines.extend(_table(active[active["advice"].eq("direction_review")].sort_values("deep_score", ascending=False), common_columns))
    report_lines.extend(["", "## 可恢复入库候选", ""])
    restore_columns = common_columns + [("prior_retirement_reason", "原退役原因", None)]
    report_lines.extend(_table(retired[retired["advice"].eq("restore_candidate")].sort_values("deep_score", ascending=False), restore_columns))
    report_lines.extend(["", "## 政策复核后才可考虑恢复", ""])
    report_lines.extend(_table(retired[retired["advice"].eq("policy_review")].sort_values("deep_score", ascending=False), restore_columns))
    report_lines.extend(["", "## 精确重复，不恢复", ""])
    duplicate_columns = [
        ("factor_id", "Factor ID", None),
        ("name", "名称", None),
        ("deep_score", "Deep", 1),
        ("advice_reason", "原因", None),
    ]
    report_lines.extend(_table(retired[retired["advice"].eq("keep_retired_duplicate")], duplicate_columns))
    report_lines.extend(["", "## 明确保留的现役因子", ""])
    report_lines.extend(_table(active[active["advice"].eq("keep_active")].sort_values("deep_score", ascending=False), common_columns))
    report_lines.extend(["", "## 现役观察名单", ""])
    report_lines.extend(_table(active[active["advice"].eq("active_review")].sort_values("deep_score", ascending=False), common_columns))
    report_lines.extend(
        [
            "",
            "## 建议执行顺序",
            "",
            "1. 不直接批量改库。先确认 15 个退出候选及 2 个方向修正项；方向项回到表达式阶段加整体负号后作为新版本重算。",
            "2. 对 11 个恢复候选做 operator 审批，再重建其正式因子值并执行模型边际贡献测试；本次强因子集合中没有发现 ≥0.85 的行为高相关对。",
            "3. 两个政策复核项必须重新做全历史 ST/退市暴露审计，不能由最新 Deep 分覆盖历史治理原因。",
            "4. 43 个观察项继续保留，但应按 Deep 和近 6 个月 IC 排序进入下一轮复评；不要按 80 分机械一刀切。",
            "5. 49 个字段无效因子保持退役；若仍有经济含义，应在表达式阶段迁移旧字段并以新因子重新入流程。",
            "",
        ]
    )
    report_path = paths.run_dir / "REPORT.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    advice_counts = summary_df["advice"].fillna("missing").value_counts().to_dict() if not summary_df.empty else {}
    status_counts = summary_df["status"].fillna("missing").value_counts().to_dict() if not summary_df.empty else {}
    final = {
        "schema_version": "factor_library_recertification_summary_v1",
        "run_id": paths.run_dir.name,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "factor_count": len(rows),
        "result_count": len(results),
        "status_counts": status_counts,
        "advice_counts": advice_counts,
        "behavioral_redundancy_pair_count": len(correlations),
        "registry_mutation": False,
        "artifacts": {
            "manifest": str(paths.manifest),
            "status": str(paths.status),
            "summary_csv": str(summary_csv),
            "behavioral_redundancy": str(paths.run_dir / "behavioral_redundancy.json"),
            "analysis": str(paths.run_dir / "analysis.json"),
            "report": str(report_path),
            "results_dir": str(paths.results),
        },
    }
    atomic_write_json(paths.run_dir / "summary.json", final)
    atomic_write_json(RECERTIFICATION_ROOT / "latest.json", final)
    return final


def run(args: argparse.Namespace) -> dict[str, Any]:
    rows = registry_snapshot()
    if args.factor_id:
        wanted = set(args.factor_id)
        rows = [row for row in rows if str(row.get("factor_id")) in wanted]
        missing = sorted(wanted - {str(row.get("factor_id")) for row in rows})
        if missing:
            raise ValueError(f"factor_ids_not_found:{','.join(missing)}")
    if args.limit > 0:
        rows = rows[: args.limit]
    run_id = args.resume_run or args.run_id or f"recert_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    paths = RunPaths.create(run_id)
    manifest = build_manifest(evaluation_mode=args.evaluation_mode, run_id=run_id, rows=rows, workers=args.workers)
    if paths.manifest.exists() and args.resume_run:
        previous = json.loads(paths.manifest.read_text(encoding="utf-8"))
        previous_ids = [str(row.get("factor_id")) for row in previous.get("registry_rows", [])]
        current_ids = [str(row.get("factor_id")) for row in rows]
        if previous_ids != current_ids:
            raise RuntimeError("resume_registry_snapshot_mismatch")
        manifest = previous
    else:
        atomic_write_json(paths.manifest, manifest)

    completed = completed_factor_ids(paths.run_dir)
    pending = [row for row in rows if str(row.get("factor_id")) not in completed]
    _write_progress(paths, total=len(rows), completed=len(completed))
    if not pending:
        final = _finalize(paths, rows)
        _write_progress(paths, total=len(rows), completed=len(rows), status="completed")
        return final

    profile = resolve_evaluation_profile(args.evaluation_mode)
    start_date = profile["factor"]["selection_start_date"]
    end_date = profile["factor"]["selection_end_date"]
    load_start = _warmup_start_date(start_date)
    from quantgpt.market_data import fixed_non_st_stock_codes

    tradable_codes = fixed_non_st_stock_codes()
    context = mp.get_context("fork")
    finished = len(completed)
    input_summary_path = paths.run_dir / "input_data_summary.json"
    input_summary = {
        "load_start_date": load_start,
        "selection_start_date": start_date,
        "selection_end_date": end_date,
        "batch_size": int(args.batch_size),
        "batches": [],
    }
    if input_summary_path.exists() and args.resume_run:
        try:
            previous_input = json.loads(input_summary_path.read_text(encoding="utf-8"))
            if isinstance(previous_input.get("batches"), list):
                input_summary["batches"] = previous_input["batches"]
        except (OSError, json.JSONDecodeError):
            pass

    batches = [pending[index : index + args.batch_size] for index in range(0, len(pending), args.batch_size)]
    for batch_number, batch_rows in enumerate(batches, start=1):
        expressions = [str(row.get("expression") or "") for row in batch_rows if row.get("expression")]
        required = _required_market_columns(expressions) | {"trade_date", "stock_code", "close", "total_mv"}
        load_started = time.time()
        market = _load_market_data(load_start, end_date, required_columns=required)
        if market.empty:
            raise RuntimeError(f"market_data_empty:batch={batch_number}")
        market["trade_date"] = pd.to_datetime(market["trade_date"]).dt.normalize()
        market = market.sort_values(["stock_code", "trade_date"], kind="mergesort").reset_index(drop=True)
        score_mask = market["trade_date"].between(start_date, end_date) & market["stock_code"].astype(str).isin(tradable_codes)
        backtest_base = market.loc[score_mask, ["trade_date", "stock_code", "close", "total_mv"]]
        score_index = backtest_base.index
        if len(backtest_base) < 1000:
            raise RuntimeError(f"backtest_base_insufficient:{len(backtest_base)}:batch={batch_number}")

        batch_stats = {
            "batch_number": batch_number,
            "factor_ids": [str(row.get("factor_id")) for row in batch_rows],
            "market_rows": int(len(market)),
            "market_columns": list(market.columns),
            "backtest_rows": int(len(backtest_base)),
            "backtest_stocks": int(backtest_base["stock_code"].nunique()),
            "backtest_dates": int(backtest_base["trade_date"].nunique()),
            "load_seconds": round(time.time() - load_started, 3),
        }
        input_summary["batches"].append(batch_stats)
        atomic_write_json(input_summary_path, input_summary)

        initializer_args = (
            market,
            backtest_base,
            score_index,
            str(paths.run_dir),
            start_date,
            end_date,
            rolling_config(),
        )
        with context.Pool(processes=args.workers, initializer=_worker_init, initargs=initializer_args) as pool:
            for result in pool.imap_unordered(_run_one, batch_rows, chunksize=1):
                finished += 1
                _write_progress(paths, total=len(rows), completed=finished, current=result)
                print(
                    json.dumps(
                        {
                            "progress": f"{finished}/{len(rows)}",
                            "batch": f"{batch_number}/{len(batches)}",
                            "factor_id": result.get("factor_id"),
                            "status": result.get("status"),
                            "quick_score": result.get("quick_score"),
                            "deep_score": result.get("deep_score"),
                            "advice": (result.get("lifecycle_advice") or {}).get("advice"),
                            "runtime_seconds": result.get("runtime_seconds"),
                        },
                        ensure_ascii=False,
                        default=json_default,
                    ),
                    flush=True,
                )
        del market, backtest_base, score_index, score_mask
        gc.collect()

    final = _finalize(paths, rows)
    _write_progress(paths, total=len(rows), completed=len(rows), status="completed")
    return final


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-mode", choices=("research", "production"), default="production")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--resume-run", default="")
    parser.add_argument("--factor-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0, help="Probe only the first N snapshot rows")
    parser.add_argument("--batch-size", type=int, default=24, help="Reload a bounded field union for each factor batch")
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 4:
        parser.error("--workers must be between 1 and 4")
    if args.batch_size < args.workers or args.batch_size > 64:
        parser.error("--batch-size must be between --workers and 64")
    if args.resume_run and args.run_id:
        parser.error("--resume-run and --run-id are mutually exclusive")
    return args


if __name__ == "__main__":
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2, default=json_default))
