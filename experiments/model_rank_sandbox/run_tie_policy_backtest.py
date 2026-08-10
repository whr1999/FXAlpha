#!/usr/bin/env python3
"""Compare explicit score-boundary policies without touching production state.

The runner reads an existing prediction artifact and point-in-time Qlib ROE,
builds daily equal-weight target portfolios, and sends every policy through the
same Qlib exchange/backtest assumptions.  Its only mutable target is
``runtime/research_sandbox/model_tie_policy``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from domain.model.qlib_direct import _ensure_qlib0627_path, _risk_value
from domain.model.training_contract import QLIB_REQUIRED_LIMIT_THRESHOLD
from storage.paths import QLIB_DATA_ROOT


_ensure_qlib0627_path()

import qlib
from qlib.backtest import backtest
from qlib.config import REG_CN
from qlib.contrib.evaluate import risk_analysis
from qlib.contrib.strategy.order_generator import OrderGenWOInteract
from qlib.contrib.strategy.signal_strategy import WeightStrategyBase
from qlib.data import D


SCHEMA_VERSION = "model_tie_policy_backtest_v1"
SANDBOX_ROOT = PROJECT_ROOT / "runtime" / "research_sandbox" / "model_tie_policy"
POLICIES = ("code_top20", "distinct_only", "include_all_ties", "roe_top20")


class PrecomputedTargetWeightStrategy(WeightStrategyBase):
    """Rebalance to the sparse target-weight signal supplied for each day."""

    def __init__(self, **kwargs: Any) -> None:
        # The non-interactive generator sizes the target from signal-day closes
        # and leaves execution to the next-day Qlib exchange.  Unlike the
        # interactive generator, it remains defined when an existing holding is
        # suspended and has no current open price.
        super().__init__(order_generator_cls_or_obj=OrderGenWOInteract, **kwargs)

    def generate_trade_decision(self, execute_result: Any = None) -> Any:
        # Qlib's daily TradeCalendarManager models a day as [day, next_day),
        # so even the final executable date needs one later timestamp as its
        # right endpoint.  Our provider intentionally has no future-calendar
        # file; when the final data date is executed, add only that interval
        # endpoint in memory.  This does not create an extra trading step or
        # supply any synthetic price/return data.
        calendar = self.trade_calendar
        calendar_index = calendar.start_index + calendar.get_trade_step()
        if calendar_index + 1 >= len(calendar._calendar):
            terminal = pd.Timestamp(calendar._calendar[calendar_index]) + pd.Timedelta(days=1)
            calendar._calendar = np.append(calendar._calendar, terminal)
        return super().generate_trade_decision(execute_result)

    def generate_target_weight_position(
        self,
        score: pd.Series | pd.DataFrame,
        current: Any,
        trade_start_time: pd.Timestamp,
        trade_end_time: pd.Timestamp,
    ) -> dict[str, float]:
        del current, trade_start_time, trade_end_time
        if isinstance(score, pd.DataFrame):
            if "target_weight" in score.columns:
                weight = score["target_weight"]
            else:
                weight = score.iloc[:, 0]
        else:
            weight = score
        numeric = pd.to_numeric(weight, errors="coerce").dropna()
        numeric = numeric[numeric > 0]
        total = float(numeric.sum())
        if not total or not math.isfinite(total):
            return {}
        return {str(instrument): float(value / total) for instrument, value in numeric.items()}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_output_dir(run_id: str) -> Path:
    clean = "".join(char for char in run_id if char.isalnum() or char in {"-", "_", "."})
    if not clean or clean != run_id:
        raise ValueError("run_id contains unsupported characters")
    root = SANDBOX_ROOT.resolve()
    output = (SANDBOX_ROOT / clean).resolve()
    if root not in output.parents:
        raise ValueError("sandbox output escaped allowed root")
    output.mkdir(parents=True, exist_ok=False)
    return output


def _prediction(path: Path, start_date: str | None, end_date: str | None) -> tuple[pd.Series, Path]:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    value = pd.read_pickle(path)
    score = value.iloc[:, 0] if isinstance(value, pd.DataFrame) else value
    if not isinstance(score.index, pd.MultiIndex) or set(score.index.names) != {"datetime", "instrument"}:
        raise ValueError("prediction must use MultiIndex(datetime,instrument)")
    score = pd.to_numeric(score, errors="coerce").rename("score").dropna().sort_index()
    dates = pd.to_datetime(score.index.get_level_values("datetime")).normalize()
    if start_date:
        score = score.loc[dates >= pd.Timestamp(start_date).normalize()]
        dates = pd.to_datetime(score.index.get_level_values("datetime")).normalize()
    if end_date:
        score = score.loc[dates <= pd.Timestamp(end_date).normalize()]
    if score.empty:
        raise ValueError("prediction slice is empty")
    return score, path


def _roe_frame(score: pd.Series) -> pd.DataFrame:
    dates = pd.to_datetime(score.index.get_level_values("datetime")).normalize()
    instruments = sorted(set(score.index.get_level_values("instrument").map(str)))
    frame = D.features(
        instruments,
        ["$roe"],
        start_time=str(pd.Timestamp(dates.min()).date()),
        end_time=str(pd.Timestamp(dates.max()).date()),
        freq="day",
        disk_cache=False,
    ).rename(columns={"$roe": "roe"})
    return score.to_frame().join(frame, how="left")


def _same_optional_number(left: Any, right: Any) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    if pd.isna(left) or pd.isna(right):
        return False
    return bool(float(left) == float(right))


def _daily_selection(frame: pd.DataFrame, *, topk: int) -> tuple[dict[str, pd.Series], pd.DataFrame]:
    selected_rows: dict[str, list[pd.Series]] = {policy: [] for policy in POLICIES}
    audits: list[dict[str, Any]] = []
    for signal_date, raw in frame.groupby(level="datetime", sort=True):
        daily = raw.reset_index().dropna(subset=["score"]).copy()
        code_ranked = daily.sort_values(
            ["score", "instrument"], ascending=[False, True], kind="mergesort"
        ).reset_index(drop=True)
        if len(code_ranked) < topk:
            raise ValueError(f"fewer than topk predictions on {signal_date}")
        boundary = float(code_ranked.iloc[topk - 1]["score"])
        next_score = float(code_ranked.iloc[topk]["score"]) if len(code_ranked) > topk else None
        boundary_tied = next_score is not None and boundary == next_score

        code_selected = code_ranked.head(topk)
        if boundary_tied:
            distinct_selected = code_ranked.loc[code_ranked["score"] > boundary]
        else:
            distinct_selected = code_selected
        inclusive_selected = code_ranked.loc[code_ranked["score"] >= boundary]

        roe_ranked = daily.sort_values(
            ["score", "roe"], ascending=[False, False], na_position="last", kind="mergesort"
        ).reset_index(drop=True)
        roe_residual_boundary_tie = False
        if len(roe_ranked) > topk:
            row20, row21 = roe_ranked.iloc[topk - 1], roe_ranked.iloc[topk]
            roe_residual_boundary_tie = bool(
                float(row20["score"]) == float(row21["score"])
                and _same_optional_number(row20["roe"], row21["roe"])
            )
        if roe_residual_boundary_tie:
            raise RuntimeError(f"ROE policy still tied at topk boundary on {signal_date}")
        roe_selected = roe_ranked.head(topk)

        selections = {
            "code_top20": code_selected,
            "distinct_only": distinct_selected,
            "include_all_ties": inclusive_selected,
            "roe_top20": roe_selected,
        }
        for policy, selected in selections.items():
            count = len(selected)
            if count <= 0:
                raise RuntimeError(f"empty selection for {policy} on {signal_date}")
            index = pd.MultiIndex.from_arrays(
                [pd.Index([pd.Timestamp(signal_date)] * count, name="datetime"), selected["instrument"].astype(str)],
                names=["datetime", "instrument"],
            )
            selected_rows[policy].append(pd.Series(1.0 / count, index=index, name="target_weight"))

        audits.append(
            {
                "signal_date": str(pd.Timestamp(signal_date).date()),
                "prediction_count": int(len(code_ranked)),
                "unique_score_count": int(code_ranked["score"].nunique()),
                "boundary_score": boundary,
                "strictly_above_boundary": int((code_ranked["score"] > boundary).sum()),
                "equal_to_boundary": int((code_ranked["score"] == boundary).sum()),
                "boundary_tied": boundary_tied,
                "code_top20_count": int(len(code_selected)),
                "distinct_only_count": int(len(distinct_selected)),
                "include_all_ties_count": int(len(inclusive_selected)),
                "roe_top20_count": int(len(roe_selected)),
                "roe_coverage_ratio": float(daily["roe"].notna().mean()),
                "roe_residual_boundary_tie": roe_residual_boundary_tie,
            }
        )
    return {
        policy: pd.concat(parts).sort_index() for policy, parts in selected_rows.items()
    }, pd.DataFrame(audits)


def _period_metrics(report: pd.DataFrame) -> dict[str, Any]:
    if report.empty:
        return {"day_count": 0}
    net_return = pd.to_numeric(report["return"], errors="coerce") - pd.to_numeric(report["cost"], errors="coerce")
    bench = pd.to_numeric(report.get("bench", 0.0), errors="coerce")
    excess = net_return - bench
    net_risk = risk_analysis(net_return)
    gross_risk = risk_analysis(pd.to_numeric(report["return"], errors="coerce"))
    excess_risk = risk_analysis(excess)
    return {
        "day_count": int(len(report)),
        "cumulative_net_return": float((1.0 + net_return.fillna(0.0)).prod() - 1.0),
        "cumulative_benchmark_return": float((1.0 + bench.fillna(0.0)).prod() - 1.0),
        "cumulative_excess_return": float((1.0 + excess.fillna(0.0)).prod() - 1.0),
        "net_strategy_annualized_ret": _risk_value(net_risk, "annualized_return"),
        "gross_strategy_annualized_ret": _risk_value(gross_risk, "annualized_return"),
        "excess_annualized_ret_with_cost": _risk_value(excess_risk, "annualized_return"),
        "excess_information_ratio_with_cost": _risk_value(excess_risk, "information_ratio"),
        "net_max_drawdown": _risk_value(net_risk, "max_drawdown"),
        "avg_turnover": float(pd.to_numeric(report.get("turnover"), errors="coerce").mean()),
        "avg_cost": float(pd.to_numeric(report.get("cost"), errors="coerce").mean()),
    }


def _holding_counts(positions: Any) -> pd.Series:
    rows: dict[pd.Timestamp, int] = {}
    if isinstance(positions, dict):
        iterator = positions.items()
    else:
        try:
            iterator = enumerate(positions)
        except TypeError:
            return pd.Series(dtype="int64")
    for key, position in iterator:
        try:
            rows[pd.Timestamp(key)] = int(len(position.get_stock_list()))
        except Exception:
            continue
    return pd.Series(rows, dtype="int64").sort_index()


def _run_policy(
    policy: str,
    target_signal: pd.Series,
    *,
    output_dir: Path,
    execution_start: str,
    execution_end: str,
) -> dict[str, Any]:
    policy_dir = output_dir / policy
    policy_dir.mkdir(parents=True, exist_ok=False)
    strategy_config = {
        "class": "PrecomputedTargetWeightStrategy",
        "module_path": "experiments.model_rank_sandbox.run_tie_policy_backtest",
        "kwargs": {"signal": target_signal},
    }
    executor_config = {
        "class": "SimulatorExecutor",
        "module_path": "qlib.backtest.executor",
        "kwargs": {
            "time_per_step": "day",
            "generate_portfolio_metrics": True,
            "verbose": False,
            "indicator_config": {"show_indicator": False},
        },
    }
    exchange_kwargs = {
        "freq": "day",
        "deal_price": "open",
        "open_cost": 0.0005,
        "close_cost": 0.0015,
        "min_cost": 5,
        "limit_threshold": tuple(QLIB_REQUIRED_LIMIT_THRESHOLD),
    }
    portfolio_metric_dict, _indicator_dict = backtest(
        start_time=execution_start,
        end_time=execution_end,
        executor=executor_config,
        strategy=strategy_config,
        account=100_000_000,
        benchmark="000300sh",
        exchange_kwargs=exchange_kwargs,
    )
    report, positions = portfolio_metric_dict["1day"]
    report.to_pickle(policy_dir / "report_normal_1day.pkl")
    target_signal.to_pickle(policy_dir / "target_weight_signal.pkl")
    counts = _holding_counts(positions)
    counts.rename("actual_holding_count").to_csv(policy_dir / "actual_holding_counts.csv", index_label="datetime")
    windows = {
        "full": report,
        "original_refit_test_through_20260630": report.loc[pd.to_datetime(report.index) <= pd.Timestamp("2026-06-30")],
        "post_refit_extension_from_20260701": report.loc[pd.to_datetime(report.index) >= pd.Timestamp("2026-07-01")],
    }
    summary = {
        "policy": policy,
        "metrics": {name: _period_metrics(value) for name, value in windows.items()},
        "actual_holding_count": {
            "days": int(len(counts)),
            "min": int(counts.min()) if len(counts) else None,
            "mean": float(counts.mean()) if len(counts) else None,
            "median": float(counts.median()) if len(counts) else None,
            "max": int(counts.max()) if len(counts) else None,
        },
        "artifacts": {
            "report": str(policy_dir / "report_normal_1day.pkl"),
            "target_weight_signal": str(policy_dir / "target_weight_signal.pkl"),
            "actual_holding_counts": str(policy_dir / "actual_holding_counts.csv"),
        },
    }
    _write_json(policy_dir / "summary.json", summary)
    return summary


def _overlap_summary(signals: dict[str, pd.Series], baseline: str = "code_top20") -> dict[str, Any]:
    base = signals[baseline]
    dates = sorted(set(base.index.get_level_values("datetime")))
    output: dict[str, Any] = {}
    for policy, signal in signals.items():
        if policy == baseline:
            continue
        overlaps: list[float] = []
        jaccards: list[float] = []
        for date in dates:
            base_set = set(base.xs(date, level="datetime").index.map(str))
            other_set = set(signal.xs(date, level="datetime").index.map(str))
            overlaps.append(len(base_set & other_set) / max(len(base_set), 1))
            jaccards.append(len(base_set & other_set) / max(len(base_set | other_set), 1))
        output[policy] = {
            "avg_baseline_top20_overlap_ratio": float(np.mean(overlaps)),
            "min_baseline_top20_overlap_ratio": float(np.min(overlaps)),
            "avg_jaccard": float(np.mean(jaccards)),
        }
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--model-run-id")
    source.add_argument("--prediction-path")
    parser.add_argument("--run-id", default=f"tie-policy-{datetime.now().strftime('%Y%m%dT%H%M%S')}")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--topk", type=int, default=20)
    args = parser.parse_args()

    output_dir = _safe_output_dir(args.run_id)
    qlib.init(
        provider_uri=str(QLIB_DATA_ROOT),
        region=REG_CN,
        auto_mount=False,
        expression_cache=None,
        dataset_cache=None,
    )
    if args.model_run_id:
        requested_prediction_path = (
            PROJECT_ROOT / "runtime" / "model" / "runs" / args.model_run_id / "pred.pkl"
        )
        prediction_source = "single_fitted_model_run"
    else:
        requested_prediction_path = Path(str(args.prediction_path))
        if not requested_prediction_path.is_absolute():
            requested_prediction_path = PROJECT_ROOT / requested_prediction_path
        prediction_source = "external_or_rolling_prediction_artifact"
    score, prediction_path = _prediction(requested_prediction_path, args.start_date, args.end_date)
    joined = _roe_frame(score)
    signals, selection_audit = _daily_selection(joined, topk=int(args.topk))
    selection_audit.to_csv(output_dir / "selection_audit.csv", index=False)

    signal_dates = pd.DatetimeIndex(pd.to_datetime(score.index.get_level_values("datetime"))).normalize().unique().sort_values()
    calendar = pd.DatetimeIndex(pd.to_datetime(D.calendar(freq="day"))).normalize().sort_values().unique()
    final_pos = int(calendar.searchsorted(signal_dates.max(), side="right"))
    if final_pos >= len(calendar):
        raise RuntimeError("no execution day exists after final signal date")
    execution_start = str(pd.Timestamp(signal_dates.min()).date())
    execution_end = str(pd.Timestamp(calendar[final_pos]).date())

    summaries: dict[str, Any] = {}
    for policy in POLICIES:
        print(f"[tie-policy] {policy} start", flush=True)
        summaries[policy] = _run_policy(
            policy,
            signals[policy],
            output_dir=output_dir,
            execution_start=execution_start,
            execution_end=execution_end,
        )
        print(f"[tie-policy] {policy} complete", flush=True)

    count_columns = [
        "code_top20_count",
        "distinct_only_count",
        "include_all_ties_count",
        "roe_top20_count",
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": args.run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "research_only": True,
        "production_writes": False,
        "model_run_id": args.model_run_id,
        "prediction": {
            "source_kind": prediction_source,
            "path": str(prediction_path),
            "sha256": _sha256(prediction_path),
            "row_count": int(len(score)),
            "date_count": int(len(signal_dates)),
            "start_date": str(pd.Timestamp(signal_dates.min()).date()),
            "end_date": str(pd.Timestamp(signal_dates.max()).date()),
        },
        "execution_window": {"start": execution_start, "end": execution_end},
        "topk": int(args.topk),
        "policy_contracts": {
            "code_top20": "score_desc_then_instrument_asc_top20_baseline",
            "distinct_only": "when_top20_boundary_tied_exclude_entire_boundary_score_group_else_top20",
            "include_all_ties": "include_every_stock_with_score_at_or_above_top20_boundary",
            "roe_top20": "score_desc_then_point_in_time_roe_desc_top20_residual_boundary_tie_blocks",
        },
        "roe": {
            "source": f"qlib_provider:{QLIB_DATA_ROOT}:$roe",
            "point_in_time_lineage": "fina_indicator_ann_date_backward_asof; generic_effective_date_priority_f_ann_date_then_ann_date_then_trade_date_then_end_date",
            "coverage_ratio": float(joined["roe"].notna().mean()),
            "residual_boundary_tie_days": int(selection_audit["roe_residual_boundary_tie"].sum()),
        },
        "selection": {
            "boundary_tied_days": int(selection_audit["boundary_tied"].sum()),
            "date_count": int(len(selection_audit)),
            "target_count_summary": selection_audit[count_columns].describe().to_dict(),
            "overlap_vs_code_top20": _overlap_summary(signals),
        },
        "execution_contract": {
            "engine": "Qlib SimulatorExecutor daily",
            "weights": "daily equal target weight",
            "order_sizing": "signal_day_close_via_OrderGenWOInteract",
            "deal_price": "open",
            "open_cost": 0.0005,
            "close_cost": 0.0015,
            "min_cost": 5,
            "limit_threshold": list(QLIB_REQUIRED_LIMIT_THRESHOLD),
            "benchmark": "000300sh",
            "account": 100_000_000,
            "last_signal_executed_next_trading_day": True,
            "terminal_calendar_endpoint": "synthetic_next_calendar_day_in_memory_only_no_price_or_return_data",
        },
        "results": summaries,
        "artifacts": {"selection_audit": str(output_dir / "selection_audit.csv")},
    }
    _write_json(output_dir / "summary.json", manifest)
    print(json.dumps(_jsonable({"status": "complete", "summary": str(output_dir / "summary.json")}), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
