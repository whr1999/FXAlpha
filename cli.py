from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storage.paths import (
    FACTOR_DEFAULT_COST_RATE,
    FACTOR_DEFAULT_END_DATE,
    FACTOR_DEFAULT_HOLDING_PERIOD,
    FACTOR_DEFAULT_START_DATE,
    FACTOR_DEFAULT_TOP_FRAC,
    FACTOR_DEFAULT_UNIVERSE,
    FACTOR_VALUE_DEFAULT_END_DATE,
    FACTOR_VALUE_DEFAULT_START_DATE,
    MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    MODEL_DEFAULT_FORWARD_PERIOD,
    MODEL_DEFAULT_START_DATE,
    MODEL_DEFAULT_STATUS_FILTER,
    MODEL_DEFAULT_TOPK,
)


def _daily_ops_summary(payload: dict) -> dict:
    outputs = payload.get("outputs") or {}
    keys = [
        "status",
        "decision_status",
        "waiting_reason",
        "blockers",
        "data_update_result",
        "data_update_reason",
        "data_latest_date_before",
        "data_latest_date_after",
        "data_quality_summary",
        "qlib_latest",
        "production_model_id",
        "production_model_run_id",
        "prediction_status",
        "latest_recommendation_id",
        "latest_recommendation_signal_date",
        "latest_recommendation_execution_date",
        "pending_count",
        "pending_summary",
        "trade_action",
        "execution_result",
        "latest_qlib_paper_execution_status",
        "paper_ledger_path",
        "blocked_reason",
        "commands_run",
    ]
    return {
        "ok": payload.get("ok"),
        "err": payload.get("err", ""),
        "outputs": {key: outputs.get(key) for key in keys},
        "warnings": payload.get("warnings", []),
        "generated_at": payload.get("generated_at"),
    }


def cmd_data_status(args) -> None:
    from services.data_foundation_service import data_status

    result = data_status()
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_data_daily_preflight(args) -> None:
    from services.data_foundation_service import data_daily_preflight

    result = data_daily_preflight(target_date=args.target_date)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_data_stage_update(args) -> None:
    from services.data_foundation_service import data_stage_update

    result = data_stage_update(target_date=args.target_date, dry_run=bool(args.dry_run))
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_data_promote_staged(args) -> None:
    from services.data_foundation_service import data_promote_staged

    result = data_promote_staged(
        package_id=args.package_id,
        latest=bool(args.latest),
        wait_idle=bool(args.wait_idle),
        timeout_minutes=args.timeout_minutes,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_data_daily_routine(args) -> None:
    from services.data_foundation_service import data_daily_routine

    result = data_daily_routine(
        target_date=args.target_date,
        wait_idle=not bool(args.no_wait_idle),
        timeout_minutes=args.timeout_minutes,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))
    if not result.ok:
        raise SystemExit(1)


def cmd_data_production_audit(args) -> None:
    from services.data_foundation_service import data_production_audit

    result = data_production_audit(
        replace_from_date=args.replace_from_date,
        full_scan=bool(args.full_scan),
        deep_sample_count=args.deep_sample_count,
        write_report=bool(args.write_report),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_data_tushare_preflight(args) -> None:
    from services.data_foundation_service import data_tushare_preflight

    result = data_tushare_preflight(
        start_date=args.start_date,
        cutoff_date=args.cutoff_date,
        pad_trading_days=args.pad_trading_days,
        max_trade_days=args.max_trade_days,
        max_codes=args.max_codes,
        proxy_mode=args.proxy_mode,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_data_tushare_network(args) -> None:
    from services.data_foundation_service import data_tushare_network

    if bool(args.repair_routes) and not bool(args.execute):
        raise SystemExit("--repair-routes requires --execute")
    result = data_tushare_network(
        verify_http=not bool(args.no_http_probe),
        repair_routes=bool(args.repair_routes and args.execute),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_data_tushare_full_rebuild(args) -> None:
    from services.data_foundation_service import data_tushare_full_rebuild

    result = data_tushare_full_rebuild(
        start_date=args.start_date,
        cutoff_date=args.cutoff_date,
        pad_trading_days=args.pad_trading_days,
        package_id=args.package_id,
        resume=not bool(args.no_resume),
        dry_run=bool(args.dry_run),
        max_trade_days=args.max_trade_days,
        max_codes=args.max_codes,
        proxy_mode=args.proxy_mode,
        trade_date_chunk_size=args.trade_date_chunk_size,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_data_tushare_full_rebuild_status(args) -> None:
    from services.data_foundation_service import data_tushare_full_rebuild_status

    result = data_tushare_full_rebuild_status(package_id=args.package_id, latest=not bool(args.no_latest))
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_data_tushare_prepare_production(args) -> None:
    from services.data_foundation_service import data_tushare_prepare_production

    result = data_tushare_prepare_production(
        package_id=args.package_id,
        latest=bool(args.latest),
        force=bool(args.force),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_data_tushare_status_backfill(args) -> None:
    from services.data_foundation_service import data_tushare_status_backfill

    result = data_tushare_status_backfill(
        package_id=args.package_id,
        proxy_mode=args.proxy_mode,
        fetch_live=not bool(args.no_fetch_live),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_data_tushare_limit_backfill(args) -> None:
    from services.data_foundation_service import data_tushare_limit_backfill

    result = data_tushare_limit_backfill(
        package_id=args.package_id,
        proxy_mode=args.proxy_mode,
        fetch_live=not bool(args.no_fetch_live),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_data_tushare_promote_staged(args) -> None:
    from services.data_foundation_service import data_tushare_promote_staged

    result = data_tushare_promote_staged(
        package_id=args.package_id,
        latest=bool(args.latest),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_daily_ops_routine(args) -> None:
    _retired_trading_write_command("daily-ops-routine", "data-daily-routine followed by paper-fleet-run")


def cmd_factor_research(args) -> None:
    from services.factor_research_service import factor_research_run

    result = factor_research_run(
        direction=args.direction,
        universe=args.universe,
        n_candidates=args.n_candidates,
        n_rounds=args.n_rounds,
        target_adopted=args.target_adopted,
        qgpt_url=args.qgpt_url,
        mcp_url=args.mcp_url,
        max_agent_steps=args.max_agent_steps,
        start_date=args.start_date,
        end_date=args.end_date,
        holding_period=args.holding_period,
        benchmark=args.benchmark,
        top_frac=args.top_frac,
        cost_rate=args.cost_rate,
        rebalance_anchor=args.rebalance_anchor,
        universe_date=args.universe_date,
        seed_count=args.seed_count,
        seed_max_concurrent=args.seed_max_concurrent,
        max_direction_attempts=args.max_direction_attempts,
        max_stagnation_rounds=args.max_stagnation_rounds,
        auto_sessions=args.auto_sessions,
        seed_batch_rounds=args.seed_batch_rounds,
        seed_batch_max_candidates=args.seed_batch_max_candidates,
        dry_run=bool(args.dry_run),
        submit_wq=bool(args.submit_wq),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_factor_status(args) -> None:
    from services.factor_research_service import factor_research_status

    result = factor_research_status()
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def _factor_orch_http(method: str, path: str, body: dict | None = None, *, api_url: str) -> dict:
    url = f"{str(api_url or 'http://127.0.0.1:18081').rstrip('/')}{path}"
    payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if method != "GET" else None
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"FXAlpha API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"FXAlpha API is unavailable at {api_url}. Start fxalpha-factor-stack.target first: {exc}"
        ) from exc


def cmd_factor_orch(args) -> None:
    action = args.factor_orch_command
    if action == "status":
        result = _factor_orch_http("GET", "/factor/research/control", api_url=args.api_url)
    elif action == "start":
        body = {
            "orchestration_mode": "orchestrator",
            "direction": args.direction,
            "target_adopted": args.target_adopted,
            "n_candidates": args.n_candidates,
            "n_rounds": args.n_rounds,
        }
        result = _factor_orch_http("POST", "/factor/research/start", body, api_url=args.api_url)
    elif action in {"pause", "resume", "stop"}:
        result = _factor_orch_http(
            "POST",
            f"/factor/research/{action}",
            {"run_id": args.run_id, "reason": f"cli_operator_{action}"},
            api_url=args.api_url,
        )
    elif action == "guidance":
        result = _factor_orch_http(
            "POST",
            "/factor/research/guidance",
            {"run_id": args.run_id, "message": args.message, "author": "cli_operator"},
            api_url=args.api_url,
        )
    else:
        raise SystemExit("factor-orch requires one of: status, start, pause, resume, stop, guidance")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def cmd_factor_seed_mine(args) -> None:
    from services.factor_research_service import factor_seed_mine

    result = factor_seed_mine(
        target_new=args.target_new,
        universe=args.universe,
        start_date=args.start_date,
        end_date=args.end_date,
        holding_period=args.holding_period,
        max_candidates=args.max_candidates,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_factor_reset(args) -> None:
    from services.factor_research_service import factor_research_reset

    result = factor_research_reset(clear_model_features=not bool(args.keep_model_features))
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))




def cmd_factor_submit_wq(args) -> None:
    from services.factor_research_service import factor_submit_wq_active

    result = factor_submit_wq_active(universe=args.universe, min_icir=args.min_icir)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_factor_wq_status(args) -> None:
    from services.factor_research_service import factor_wq_status

    result = factor_wq_status()
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_factor_nonst_migration_plan(args) -> None:
    from services.factor_non_st_migration_service import factor_non_st_migration_plan

    result = factor_non_st_migration_plan(
        limit=args.limit,
        offset=args.offset,
        run_id=args.run_id,
        holding_period_days=args.holding_period_days,
        selection_start_date=args.selection_start_date,
        selection_end_date=args.selection_end_date,
        value_start_date=args.value_start_date,
        value_end_date=args.value_end_date,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_factor_nonst_migration_execute(args) -> None:
    from services.factor_non_st_migration_service import factor_non_st_migration_execute

    result = factor_non_st_migration_execute(
        run_id=args.run_id,
        confirm=args.confirm,
        refresh_model=not bool(args.skip_model_refresh),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_factors(args) -> None:
    from services.factor_research_service import factor_registry_list

    result = factor_registry_list(
        status=args.status,
        category=args.category or "all",
        min_icir=args.min_icir or 0.0,
        sort_by=args.sort,
        limit=args.limit,
        offset=args.offset,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_serve_api(args) -> None:
    from api_server import start_api

    start_api(host=args.host, port=args.port)


def cmd_model_feature_refresh(args) -> None:
    from services.model_service import model_tool_feature_snapshot

    result = model_tool_feature_snapshot(
        feature_set_id=args.feature_set_id,
        status_filter=args.status_filter,
        start_date=args.start_date,
        end_date=args.end_date,
        label_forward_period=args.label_forward_period,
        factor_holding_period_days=args.factor_holding_period_days,
        feature_missing_strategy=args.feature_missing_strategy,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_model_status(args) -> None:
    from services.model_service import model_status

    result = model_status()
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_model_runs(args) -> None:
    from services.model_service import model_runs

    result = model_runs()
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_model_registry(args) -> None:
    from services.model_service import model_registry

    result = model_registry(status=args.status, include_archived=args.status in {"all", "archived"})
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_model_production_status(args) -> None:
    from services.model_service import model_production

    result = model_production()
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_model_promote(args) -> None:
    from services.model_service import model_promote

    result = model_promote(
        model_id=args.model_id,
        model_run_id=args.model_run_id,
        execute_qlib=not bool(args.skip_refit),
        dry_run=bool(args.dry_run),
        manual_override_reason=args.manual_override_reason,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_pred_status(args) -> None:
    from services.prediction_service import pred_status

    result = pred_status(model_id=args.model_id, model_run_id=args.model_run_id)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_pred_update(args) -> None:
    from services.prediction_service import pred_update

    result = pred_update(model_id=args.model_id, model_run_id=args.model_run_id, to_date=args.to_date, from_date=args.from_date, dry_run=bool(args.dry_run))
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_score_export(args) -> None:
    from services.prediction_service import score_export

    result = score_export(model_id=args.model_id, model_run_id=args.model_run_id, as_of_date=args.as_of_date, topk=args.topk)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_target_build(args) -> None:
    from services.prediction_service import target_build

    result = target_build(model_id=args.model_id, model_run_id=args.model_run_id, topk=args.topk, weighting=args.weighting, total_capital=args.total_capital)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def _retired_trading_write_command(command: str, replacement: str) -> None:
    print(json.dumps({
        "ok": False,
        "err": "legacy_trading_write_command_retired",
        "inputs": {"command": command},
        "outputs": {
            "replacement": replacement,
            "detail": "Production paper-account writes are only accepted through the fleet/replay state machine.",
        },
        "warnings": [],
    }, indent=2, ensure_ascii=False))
    raise SystemExit(2)


def cmd_trade_paper(args) -> None:
    _retired_trading_write_command("trade-paper", "paper-fleet-run")


def cmd_trade_status(args) -> None:
    from services.trading_service import trading_status

    result = trading_status(model_id=args.model_id, model_run_id=args.model_run_id)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_trade_daily_preflight(args) -> None:
    from services.trading_service import trading_daily_preflight

    result = trading_daily_preflight(
        model_id=args.model_id,
        model_run_id=args.model_run_id,
        signal_date=args.signal_date,
        topk=args.topk,
        total_capital=args.total_capital,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_trade_recommend(args) -> None:
    _retired_trading_write_command("trade-recommend", "paper-fleet-run")


def cmd_trade_execute_pending(args) -> None:
    _retired_trading_write_command("trade-execute-pending", "paper-fleet-run")


def cmd_trade_paper_backfill(args) -> None:
    _retired_trading_write_command("trade-paper-backfill", "paper-replay-run")


def cmd_trade_supersede_recommendation(args) -> None:
    _retired_trading_write_command("trade-supersede-recommendation", "paper-account-status --status retired")


def cmd_trade_daily_routine(args) -> None:
    _retired_trading_write_command("trade-daily-routine", "paper-fleet-run")


def cmd_paper_account_create(args) -> None:
    from services.paper_fleet_service import paper_account_create

    result = paper_account_create(
        account_id=args.account_id,
        display_name=args.display_name,
        account_mode=args.account_mode,
        model_id=args.model_id,
        model_run_id=args.model_run_id,
        initial_capital=args.initial_capital,
        effective_from=args.effective_from,
        topk=args.topk,
        n_drop=args.n_drop,
        hold_thresh=args.hold_thresh,
        deal_price=args.deal_price,
        strategy_contract_version=args.strategy_contract_version,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))
    if not result.ok:
        raise SystemExit(1)


def cmd_paper_account_status(args) -> None:
    from services.paper_fleet_service import paper_account_set_status

    result = paper_account_set_status(account_id=args.account_id, status=args.status)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))
    if not result.ok:
        raise SystemExit(1)


def cmd_paper_fleet_status(args) -> None:
    from services.paper_fleet_service import paper_fleet_status

    result = paper_fleet_status()
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_paper_fleet_preflight(args) -> None:
    from services.paper_fleet_service import paper_fleet_preflight

    result = paper_fleet_preflight(target_date=args.target_date)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_paper_fleet_run(args) -> None:
    from services.paper_fleet_service import paper_fleet_run

    result = paper_fleet_run(
        target_date=args.target_date,
        confirm_long_replay=bool(args.confirm_long_replay),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))
    if not result.ok:
        raise SystemExit(1)


def cmd_paper_replay_plan(args) -> None:
    from services.paper_fleet_service import paper_replay_plan

    result = paper_replay_plan(
        account_id=args.account_id,
        from_date=args.from_date,
        to_date=args.to_date,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_paper_replay_run(args) -> None:
    from services.paper_fleet_service import paper_replay_run

    result = paper_replay_run(
        account_id=args.account_id,
        from_date=args.from_date,
        to_date=args.to_date,
        confirm_long_replay=bool(args.confirm_long_replay),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))
    if not result.ok:
        raise SystemExit(1)


def cmd_pipeline_run(args) -> None:
    from services.pipeline_service import pipeline_run

    result = pipeline_run(
        end_date=args.end_date,
        skip_download=bool(args.skip_download),
        direction=args.direction,
        universe=args.universe,
        n_candidates=args.n_candidates,
        n_rounds=args.n_rounds,
        target_adopted=args.target_adopted,
        factor_sessions=args.factor_sessions,
        qgpt_url=args.qgpt_url,
        mcp_url=args.mcp_url,
        max_agent_steps=args.max_agent_steps,
        start_date=args.start_date,
        holding_period=args.holding_period,
        benchmark=args.benchmark,
        top_frac=args.top_frac,
        cost_rate=args.cost_rate,
        rebalance_anchor=args.rebalance_anchor,
        universe_date=args.universe_date,
        seed_count=args.seed_count,
        seed_max_concurrent=args.seed_max_concurrent,
        max_direction_attempts=args.max_direction_attempts,
        max_stagnation_rounds=args.max_stagnation_rounds,
        model_family=args.model_family,
        model_loop_n=args.model_loop_n,
        model_step_n=args.model_step_n,
        seed_batch_rounds=args.seed_batch_rounds,
        seed_batch_max_candidates=args.seed_batch_max_candidates,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_pipeline_status(args) -> None:
    from services.pipeline_service import pipeline_status

    result = pipeline_status()
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_maintenance_status(args) -> None:
    from services.maintenance_service import maintenance_status

    result = maintenance_status()
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_maintenance_cleanup(args) -> None:
    from services.maintenance_service import maintenance_cleanup

    retention_days = {}
    if args.pickle_cache_days is not None:
        retention_days["pickle_cache"] = args.pickle_cache_days
    if args.log_days is not None:
        retention_days["logs"] = args.log_days
    if args.report_days is not None:
        retention_days["quantgpt_reports"] = args.report_days
    if args.reset_backup_days is not None:
        retention_days["reset_backups"] = args.reset_backup_days

    result = maintenance_cleanup(
        profile=args.profile,
        execute=bool(args.execute),
        retention_days=retention_days or None,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_factor_audit_status(args) -> None:
    from services.factor_library_audit_service import factor_library_audit_status

    result = factor_library_audit_status(scope=getattr(args, "scope", "all"))
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_factor_audit_run(args) -> None:
    from services.factor_library_audit_service import factor_library_audit

    result = factor_library_audit(
        scope=args.scope,
        status_filter=args.status_filter,
        save_report=not bool(args.no_save_report),
        include_feature_sets=not bool(args.no_feature_sets),
        audit_window_start=args.audit_window_start,
        audit_window_end=args.audit_window_end,
        min_valid_days=args.min_valid_days,
        min_common_stocks=args.min_common_stocks,
        redundancy_threshold_rank_p90=args.redundancy_threshold_rank_p90,
        redundancy_threshold_pearson_p90=args.redundancy_threshold_pearson_p90,
        family_dependency_cut=args.family_dependency_cut,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(prog="fxalpha", description="FXAlpha unified entrypoints")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("data-status", help="Show current data foundation snapshot")
    p = sub.add_parser(
        "data-daily-preflight",
        help="Read-only Tushare daily preflight with direct-network, resource, and partial-promote gates",
    )
    p.add_argument("--target-date", default="auto")

    p = sub.add_parser("data-stage-update", help="Build a staged Tushare daily merge package without touching production data")
    p.add_argument("--target-date", default="auto")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("data-promote-staged", help="Promote a staged Tushare daily package into production after idle checks")
    p.add_argument("--package-id")
    p.add_argument("--latest", action="store_true")
    p.add_argument("--wait-idle", action="store_true")
    p.add_argument("--timeout-minutes", type=int, default=180)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser(
        "data-daily-routine",
        help="Run incremental Tushare daily update with journal promote, post audit, and safe cleanup preview",
    )
    p.add_argument("--target-date", default="auto")
    p.add_argument("--timeout-minutes", type=int, default=180)
    p.add_argument("--no-wait-idle", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("data-production-audit", help="Audit current Tushare production quality and downstream date alignment")
    p.add_argument("--replace-from-date")
    p.add_argument("--deep-sample-count", type=int, default=20, help="Direct Tushare cross-surface sample count; use 0 to skip")
    p.add_argument("--write-report", action="store_true", help="Write audit JSON under runtime/data_foundation/audits")
    p.add_argument("--full-scan", action="store_true", help="Scan full HDF for duplicate code/date keys; default scans the replace window")

    p = sub.add_parser("data-tushare-preflight", help="Read-only preflight for remote-only Tushare full rebuild")
    p.add_argument("--start-date", default="20180101")
    p.add_argument("--cutoff-date", default="20260602")
    p.add_argument("--pad-trading-days", type=int, default=120)
    p.add_argument("--max-trade-days", type=int)
    p.add_argument("--max-codes", type=int)
    p.add_argument("--proxy-mode", choices=["inherit", "direct"], default="direct")

    p = sub.add_parser("data-tushare-network", help="Verify Tushare direct network path, DNS resolution, and HTTP reachability")
    p.add_argument("--no-http-probe", action="store_true")
    p.add_argument("--repair-routes", action="store_true", help="Repair direct host routes; requires --execute")
    p.add_argument("--execute", action="store_true", help="Confirm route mutation when used with --repair-routes")

    p = sub.add_parser("data-tushare-full-rebuild", help="Run remote-only Tushare full rebuild into a new staging package")
    p.add_argument("--start-date", default="20180101")
    p.add_argument("--cutoff-date", default="20260602")
    p.add_argument("--pad-trading-days", type=int, default=120)
    p.add_argument("--package-id")
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-trade-days", type=int)
    p.add_argument("--max-codes", type=int)
    p.add_argument("--proxy-mode", choices=["inherit", "direct"], default="direct")
    p.add_argument("--trade-date-chunk-size", type=int, default=40)

    p = sub.add_parser("data-tushare-full-rebuild-status", help="Show progress for the latest or selected Tushare full rebuild package")
    p.add_argument("--package-id")
    p.add_argument("--no-latest", action="store_true")

    p = sub.add_parser("data-tushare-prepare-production", help="Build production raw/Qlib/QuantGPT artifacts from a completed Tushare staging package")
    p.add_argument("--package-id")
    p.add_argument("--latest", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("data-tushare-status-backfill", help="Build a staging HDF with list_status/st_status backfilled; does not touch production")
    p.add_argument("--package-id")
    p.add_argument("--proxy-mode", choices=["inherit", "direct"], default="direct")
    p.add_argument("--no-fetch-live", action="store_true")

    p = sub.add_parser("data-tushare-limit-backfill", help="Build a staging HDF with Tushare stk_limit up_limit/down_limit backfilled; does not touch production")
    p.add_argument("--package-id")
    p.add_argument("--proxy-mode", choices=["inherit", "direct"], default="direct")
    p.add_argument("--no-fetch-live", action="store_true")

    p = sub.add_parser("data-tushare-promote-staged", help="Promote prepared Tushare production-compatible artifacts into canonical production paths")
    p.add_argument("--package-id")
    p.add_argument("--latest", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("daily-ops-routine", help="Run production daily close: data update, prediction checks, recommendation trading")
    p.add_argument("--target-date", default="auto")
    p.add_argument("--timeout-minutes", type=int, default=180)
    p.add_argument("--topk", type=int, default=MODEL_DEFAULT_TOPK)
    p.add_argument("--total-capital", type=float, default=1000000.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--summary", action="store_true")

    p = sub.add_parser(
        "factor-research",
        help="Legacy/status probe only: production factor mining requires Codex native QuantGPT MCP tools",
    )
    p.add_argument("--direction", default="auto")
    p.add_argument("--universe", default=FACTOR_DEFAULT_UNIVERSE)
    p.add_argument("--n-candidates", type=int, default=10)
    p.add_argument("--n-rounds", type=int, default=3)
    p.add_argument("--target-adopted", type=int, default=3)
    p.add_argument("--qgpt-url", default="http://127.0.0.1:8003")
    p.add_argument("--mcp-url")
    p.add_argument("--max-agent-steps", type=int, default=40)
    p.add_argument("--start-date", default=FACTOR_DEFAULT_START_DATE)
    p.add_argument("--end-date", default=FACTOR_DEFAULT_END_DATE)
    p.add_argument("--holding-period", type=int, default=5)
    p.add_argument("--benchmark", default="hs300")
    p.add_argument("--top-frac", type=float, default=FACTOR_DEFAULT_TOP_FRAC)
    p.add_argument("--cost-rate", type=float, default=FACTOR_DEFAULT_COST_RATE)
    p.add_argument("--rebalance-anchor")
    p.add_argument("--universe-date")
    p.add_argument("--seed-count", type=int, default=3)
    p.add_argument("--seed-max-concurrent", type=int, default=3)
    p.add_argument("--max-direction-attempts", type=int, default=3)
    p.add_argument("--max-stagnation-rounds", type=int, default=3)
    p.add_argument("--auto-sessions", type=int, default=3)
    p.add_argument("--seed-batch-rounds", type=int, default=0)
    p.add_argument("--seed-batch-max-candidates", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--submit-wq", action="store_true")

    sub.add_parser("factor-status", help="Show factor research status and readiness")

    p = sub.add_parser("factor-orch", help="Control the production factor Orchestrator through the local HTTP API")
    factor_orch_sub = p.add_subparsers(dest="factor_orch_command")
    p_orch_status = factor_orch_sub.add_parser("status", help="Show authoritative ORCH control state")
    p_orch_start = factor_orch_sub.add_parser("start", help="Start a new production ORCH run")
    p_orch_start.add_argument("--direction", default="auto")
    p_orch_start.add_argument("--target-adopted", type=int, default=10)
    p_orch_start.add_argument("--n-candidates", type=int, default=10)
    p_orch_start.add_argument("--n-rounds", type=int, default=0)
    p_orch_pause = factor_orch_sub.add_parser("pause", help="Pause at the next safe checkpoint")
    p_orch_resume = factor_orch_sub.add_parser("resume", help="Resume the same paused run")
    p_orch_stop = factor_orch_sub.add_parser("stop", help="End the run at the next safe checkpoint")
    p_orch_guidance = factor_orch_sub.add_parser("guidance", help="Record guidance for the current run")
    for control_parser in (p_orch_pause, p_orch_resume, p_orch_stop, p_orch_guidance):
        control_parser.add_argument("--run-id", required=True)
    p_orch_guidance.add_argument("--message", required=True)
    for orch_parser in (p_orch_status, p_orch_start, p_orch_pause, p_orch_resume, p_orch_stop, p_orch_guidance):
        orch_parser.add_argument("--api-url", default="http://127.0.0.1:18081")

    p = sub.add_parser(
        "factor-seed-mine",
        help="Disabled legacy/debug local batch mining; production uses Codex native QuantGPT MCP",
    )
    p.add_argument("--target-new", type=int, default=6)
    p.add_argument("--universe", default=FACTOR_DEFAULT_UNIVERSE)
    p.add_argument("--start-date", default=FACTOR_DEFAULT_START_DATE)
    p.add_argument("--end-date", default=FACTOR_DEFAULT_END_DATE)
    p.add_argument("--holding-period", type=int, default=5)
    p.add_argument("--max-candidates", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("factor-reset", help="Reset factor research state, factor library, and derived feature artifacts")
    p.add_argument("--keep-model-features", action="store_true")

    p = sub.add_parser("factor-submit-wq", help="Submit active factors to WorldQuant Brain via QuantGPT")
    p.add_argument("--universe", default=FACTOR_DEFAULT_UNIVERSE)
    p.add_argument("--min-icir", type=float, default=0.3)

    sub.add_parser("factor-wq-status", help="Show WorldQuant Brain submission status for active factors")

    p = sub.add_parser("factor-nonst-migration-plan", help="Dry-run active factor migration to tradable_non_st")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--run-id")
    p.add_argument("--holding-period-days", type=int, default=FACTOR_DEFAULT_HOLDING_PERIOD)
    p.add_argument("--selection-start-date", default=FACTOR_DEFAULT_START_DATE)
    p.add_argument("--selection-end-date", default=FACTOR_DEFAULT_END_DATE)
    p.add_argument("--value-start-date", default=FACTOR_VALUE_DEFAULT_START_DATE)
    p.add_argument("--value-end-date", default=FACTOR_VALUE_DEFAULT_END_DATE)

    p = sub.add_parser("factor-nonst-migration-execute", help="Execute a confirmed non-ST migration dry-run plan")
    p.add_argument("--run-id", required=True)
    p.add_argument("--confirm", required=True)
    p.add_argument("--skip-model-refresh", action="store_true")

    p = sub.add_parser("factors", help="List factor registry records")
    p.add_argument("--status", default="active")
    p.add_argument("--category")
    p.add_argument("--min-icir", type=float)
    p.add_argument("--sort", default="icir")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)

    p = sub.add_parser("factor-audit", help="Audit factor library health, clusters, and feature-set recommendations")
    factor_audit_sub = p.add_subparsers(dest="factor_audit_command")
    p_audit_status = factor_audit_sub.add_parser("status", help="Show latest factor library audit report")
    p_audit_status.add_argument("--scope", choices=["quality", "information", "all"], default="all")
    p_audit_run = factor_audit_sub.add_parser("run", help="Run factor library audit")
    p_audit_run.add_argument("--scope", choices=["quality", "information", "all"], default="all")
    p_audit_run.add_argument("--status-filter", default="active")
    p_audit_run.add_argument("--save-report", action="store_true", help="Deprecated compatibility flag; reports are saved by default")
    p_audit_run.add_argument("--no-save-report", action="store_true")
    p_audit_run.add_argument("--no-feature-sets", action="store_true")
    p_audit_run.add_argument("--audit-window-start")
    p_audit_run.add_argument("--audit-window-end")
    p_audit_run.add_argument("--min-valid-days", type=int, default=120)
    p_audit_run.add_argument("--min-common-stocks", type=int, default=300)
    p_audit_run.add_argument("--redundancy-threshold-rank-p90", type=float, default=0.80)
    p_audit_run.add_argument("--redundancy-threshold-pearson-p90", type=float, default=0.75)
    p_audit_run.add_argument("--family-dependency-cut", type=float, default=0.55)

    p = sub.add_parser("serve-api", help="Start FXAlpha HTTP API for service-layer calls")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)

    p = sub.add_parser("model-feature-refresh", help="Build active combined_factors_df.parquet for model training")
    p.add_argument("--feature-set-id")
    p.add_argument("--status-filter", default=MODEL_DEFAULT_STATUS_FILTER)
    p.add_argument("--start-date", default=MODEL_DEFAULT_START_DATE)
    p.add_argument("--end-date")
    p.add_argument("--label-forward-period", type=int, default=MODEL_DEFAULT_FORWARD_PERIOD)
    p.add_argument("--factor-holding-period-days", type=int, default=MODEL_DEFAULT_FACTOR_HOLDING_PERIOD)
    p.add_argument("--feature-missing-strategy", default="qlib_processor_only")
    p.add_argument("--dry-run", action="store_true")

    sub.add_parser("model-status", help="Show model-layer readiness and latest feature-set status")
    sub.add_parser("model-runs", help="List model runs recorded in runtime")

    p = sub.add_parser("model-registry", help="List model registry rows")
    p.add_argument("--status", default="all")

    sub.add_parser("model-production", help="Show current production model")

    p = sub.add_parser("model-promote", help="Promote a model to production")
    p.add_argument("--model-id")
    p.add_argument("--model-run-id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-refit", action="store_true")
    p.add_argument("--manual-override-reason")

    p = sub.add_parser("pred-status", help="Check production prediction readiness")
    p.add_argument("--model-id")
    p.add_argument("--model-run-id")

    p = sub.add_parser("pred-update", help="Update production pred.pkl to latest qlib date")
    p.add_argument("--model-id")
    p.add_argument("--model-run-id")
    p.add_argument("--to-date")
    p.add_argument("--from-date")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("score-export", help="Export latest daily score from production model")
    p.add_argument("--model-id")
    p.add_argument("--model-run-id")
    p.add_argument("--as-of-date")
    p.add_argument("--topk", type=int)

    p = sub.add_parser("target-build", help="Build target portfolio from latest score")
    p.add_argument("--model-id")
    p.add_argument("--model-run-id")
    p.add_argument("--topk", type=int, default=MODEL_DEFAULT_TOPK)
    p.add_argument("--weighting", default="equal")
    p.add_argument("--total-capital", type=float)

    p = sub.add_parser("trade-paper", help="Run Qlib paper execution from latest target portfolio")
    p.add_argument("--model-id")
    p.add_argument("--model-run-id")
    p.add_argument("--topk", type=int, default=MODEL_DEFAULT_TOPK)
    p.add_argument("--total-capital", type=float, default=1000000.0)
    p.add_argument("--skip-pred-update", action="store_true")

    p = sub.add_parser("trade-status", help="Show FXAlpha recommendation trading cockpit status")
    p.add_argument("--model-id")
    p.add_argument("--model-run-id")

    p = sub.add_parser("trade-daily-preflight", help="Read-only safety gate before recommendation trading routine")
    p.add_argument("--model-id")
    p.add_argument("--model-run-id")
    p.add_argument("--signal-date")
    p.add_argument("--topk", type=int, default=MODEL_DEFAULT_TOPK)
    p.add_argument("--total-capital", type=float, default=1000000.0)

    p = sub.add_parser("trade-recommend", help="Generate a T+1 recommendation batch from production predictions")
    p.add_argument("--model-id")
    p.add_argument("--model-run-id")
    p.add_argument("--signal-date")
    p.add_argument("--execution-date")
    p.add_argument("--topk", type=int, default=MODEL_DEFAULT_TOPK)
    p.add_argument("--total-capital", type=float, default=1000000.0)
    p.add_argument("--skip-pred-update", action="store_true")

    p = sub.add_parser("trade-execute-pending", help="Execute pending recommendation batches via Qlib paper account")
    p.add_argument("--recommendation-id")
    p.add_argument("--total-capital", type=float)

    p = sub.add_parser("trade-paper-backfill", help="Backfill Qlib paper account mark-to-market days without rebalance")
    p.add_argument("--model-id")
    p.add_argument("--model-run-id")
    p.add_argument("--target-date")
    p.add_argument("--total-capital", type=float, default=1000000.0)

    p = sub.add_parser("trade-supersede-recommendation", help="Mark a pending recommendation as superseded without execution")
    p.add_argument("--recommendation-id", required=True)
    p.add_argument("--reason", default="")

    p = sub.add_parser("trade-daily-routine", help="Run recommendation trading routine: execute pending, then generate next recommendation")
    p.add_argument("--model-id")
    p.add_argument("--model-run-id")
    p.add_argument("--signal-date")
    p.add_argument("--topk", type=int, default=MODEL_DEFAULT_TOPK)
    p.add_argument("--total-capital", type=float, default=1000000.0)
    p.add_argument("--skip-pred-update", action="store_true")

    p = sub.add_parser("paper-account-create", help="Create or register a production Qlib paper account and model deployment")
    p.add_argument("--account-id", required=True)
    p.add_argument("--display-name")
    p.add_argument("--account-mode", choices=["fixed_model", "rolling_champion"], default="fixed_model")
    p.add_argument("--model-id")
    p.add_argument("--model-run-id")
    p.add_argument("--initial-capital", type=float, default=1000000.0)
    p.add_argument("--effective-from", required=True)
    p.add_argument("--topk", type=int, default=MODEL_DEFAULT_TOPK)
    p.add_argument("--n-drop", type=int, default=2)
    p.add_argument("--hold-thresh", type=int, default=5)
    p.add_argument("--deal-price", default="open")
    p.add_argument(
        "--strategy-contract-version",
        default="top20_drop2_hold5_open_v1",
        choices=["top20_drop2_hold5_open_v1", "confidence_cash_top20_drop2_hold5_open_v1", "confidence_cash_top20_drop2_hold5_open_v2"],
    )

    p = sub.add_parser("paper-account-status", help="Pause, activate or retire a production paper account")
    p.add_argument("--account-id", required=True)
    p.add_argument("--status", required=True, choices=["active", "paused", "retired"])

    sub.add_parser("paper-fleet-status", help="Show multi-model production paper fleet status")

    p = sub.add_parser("paper-fleet-preflight", help="Read-only preflight for all active production paper accounts")
    p.add_argument("--target-date")

    p = sub.add_parser("paper-fleet-run", help="Run all active production paper accounts through the latest available date")
    p.add_argument("--target-date")
    p.add_argument("--confirm-long-replay", action="store_true")

    p = sub.add_parser("paper-replay-plan", help="Plan an as-of-capped historical production replay for one account")
    p.add_argument("--account-id", required=True)
    p.add_argument("--from-date")
    p.add_argument("--to-date")

    p = sub.add_parser("paper-replay-run", help="Execute an as-of-capped historical production replay for one account")
    p.add_argument("--account-id", required=True)
    p.add_argument("--from-date")
    p.add_argument("--to-date")
    p.add_argument("--confirm-long-replay", action="store_true")

    p = sub.add_parser(
        "pipeline-run",
        help="Legacy end-to-end helper; unattended factor mining now blocks unless Codex native MCP performs the research step",
    )
    p.add_argument("--end-date", default=FACTOR_DEFAULT_END_DATE)
    p.add_argument("--skip-download", action="store_true")
    p.add_argument("--direction", default="auto")
    p.add_argument("--universe", default=FACTOR_DEFAULT_UNIVERSE)
    p.add_argument("--n-candidates", type=int, default=12)
    p.add_argument("--n-rounds", type=int, default=6)
    p.add_argument("--target-adopted", type=int, default=10)
    p.add_argument("--factor-sessions", type=int, default=3)
    p.add_argument("--qgpt-url", default="http://127.0.0.1:8003")
    p.add_argument("--mcp-url")
    p.add_argument("--max-agent-steps", type=int, default=60)
    p.add_argument("--start-date", default=FACTOR_DEFAULT_START_DATE)
    p.add_argument("--holding-period", type=int, default=5)
    p.add_argument("--benchmark", default="hs300")
    p.add_argument("--top-frac", type=float, default=FACTOR_DEFAULT_TOP_FRAC)
    p.add_argument("--cost-rate", type=float, default=FACTOR_DEFAULT_COST_RATE)
    p.add_argument("--rebalance-anchor")
    p.add_argument("--universe-date")
    p.add_argument("--seed-count", type=int, default=3)
    p.add_argument("--seed-max-concurrent", type=int, default=3)
    p.add_argument("--max-direction-attempts", type=int, default=3)
    p.add_argument("--max-stagnation-rounds", type=int, default=3)
    p.add_argument("--model-family", default="lgbm")
    p.add_argument("--model-loop-n", type=int, default=1)
    p.add_argument("--model-step-n", type=int)
    p.add_argument("--seed-batch-rounds", type=int, default=0)
    p.add_argument("--seed-batch-max-candidates", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")

    sub.add_parser("pipeline-status", help="Show end-to-end pipeline status")

    p = sub.add_parser("maintenance", help="Platform operations: disk audit and safe cleanup")
    maintenance_sub = p.add_subparsers(dest="maintenance_command")
    maintenance_sub.add_parser("status", help="Show disk audit and dry-run cleanup preview")
    p_cleanup = maintenance_sub.add_parser("cleanup", help="Run cleanup preview or execute cleanup")
    p_cleanup.add_argument("--profile", choices=["safe", "aggressive"], default="safe")
    p_cleanup.add_argument("--execute", action="store_true", help="Actually delete executable candidates; default is dry-run")
    p_cleanup.add_argument("--pickle-cache-days", type=int)
    p_cleanup.add_argument("--log-days", type=int)
    p_cleanup.add_argument("--report-days", type=int)
    p_cleanup.add_argument("--reset-backup-days", type=int)

    args = parser.parse_args()
    if args.command == "data-status":
        cmd_data_status(args)
        return
    if args.command == "data-daily-preflight":
        cmd_data_daily_preflight(args)
        return
    if args.command == "data-stage-update":
        cmd_data_stage_update(args)
        return
    if args.command == "data-promote-staged":
        cmd_data_promote_staged(args)
        return
    if args.command == "data-daily-routine":
        cmd_data_daily_routine(args)
        return
    if args.command == "data-production-audit":
        cmd_data_production_audit(args)
        return
    if args.command == "data-tushare-preflight":
        cmd_data_tushare_preflight(args)
        return
    if args.command == "data-tushare-network":
        cmd_data_tushare_network(args)
        return
    if args.command == "data-tushare-full-rebuild":
        cmd_data_tushare_full_rebuild(args)
        return
    if args.command == "data-tushare-full-rebuild-status":
        cmd_data_tushare_full_rebuild_status(args)
        return
    if args.command == "data-tushare-prepare-production":
        cmd_data_tushare_prepare_production(args)
        return
    if args.command == "data-tushare-status-backfill":
        cmd_data_tushare_status_backfill(args)
        return
    if args.command == "data-tushare-limit-backfill":
        cmd_data_tushare_limit_backfill(args)
        return
    if args.command == "data-tushare-promote-staged":
        cmd_data_tushare_promote_staged(args)
        return
    if args.command == "daily-ops-routine":
        cmd_daily_ops_routine(args)
        return
    if args.command == "factor-research":
        cmd_factor_research(args)
        return
    if args.command == "factor-status":
        cmd_factor_status(args)
        return
    if args.command == "factor-orch":
        cmd_factor_orch(args)
        return
    if args.command == "factor-seed-mine":
        cmd_factor_seed_mine(args)
        return
    if args.command == "factor-reset":
        cmd_factor_reset(args)
        return
    if args.command == "factor-submit-wq":
        cmd_factor_submit_wq(args)
        return
    if args.command == "factor-wq-status":
        cmd_factor_wq_status(args)
        return
    if args.command == "factor-nonst-migration-plan":
        cmd_factor_nonst_migration_plan(args)
        return
    if args.command == "factor-nonst-migration-execute":
        cmd_factor_nonst_migration_execute(args)
        return
    if args.command == "factors":
        cmd_factors(args)
        return
    if args.command == "factor-audit":
        if args.factor_audit_command == "run":
            cmd_factor_audit_run(args)
            return
        cmd_factor_audit_status(args)
        return
    if args.command == "serve-api":
        cmd_serve_api(args)
        return
    if args.command == "model-feature-refresh":
        cmd_model_feature_refresh(args)
        return
    if args.command == "model-status":
        cmd_model_status(args)
        return
    if args.command == "model-runs":
        cmd_model_runs(args)
        return
    if args.command == "model-registry":
        cmd_model_registry(args)
        return
    if args.command == "model-production":
        cmd_model_production_status(args)
        return
    if args.command == "model-promote":
        cmd_model_promote(args)
        return
    if args.command == "pred-status":
        cmd_pred_status(args)
        return
    if args.command == "pred-update":
        cmd_pred_update(args)
        return
    if args.command == "score-export":
        cmd_score_export(args)
        return
    if args.command == "target-build":
        cmd_target_build(args)
        return
    if args.command == "trade-paper":
        cmd_trade_paper(args)
        return
    if args.command == "trade-status":
        cmd_trade_status(args)
        return
    if args.command == "trade-daily-preflight":
        cmd_trade_daily_preflight(args)
        return
    if args.command == "trade-recommend":
        cmd_trade_recommend(args)
        return
    if args.command == "trade-execute-pending":
        cmd_trade_execute_pending(args)
        return
    if args.command == "trade-paper-backfill":
        cmd_trade_paper_backfill(args)
        return
    if args.command == "trade-supersede-recommendation":
        cmd_trade_supersede_recommendation(args)
        return
    if args.command == "trade-daily-routine":
        cmd_trade_daily_routine(args)
        return
    if args.command == "paper-account-create":
        cmd_paper_account_create(args)
        return
    if args.command == "paper-account-status":
        cmd_paper_account_status(args)
        return
    if args.command == "paper-fleet-status":
        cmd_paper_fleet_status(args)
        return
    if args.command == "paper-fleet-preflight":
        cmd_paper_fleet_preflight(args)
        return
    if args.command == "paper-fleet-run":
        cmd_paper_fleet_run(args)
        return
    if args.command == "paper-replay-plan":
        cmd_paper_replay_plan(args)
        return
    if args.command == "paper-replay-run":
        cmd_paper_replay_run(args)
        return
    if args.command == "pipeline-run":
        cmd_pipeline_run(args)
        return
    if args.command == "pipeline-status":
        cmd_pipeline_status(args)
        return
    if args.command == "maintenance":
        if args.maintenance_command == "cleanup":
            cmd_maintenance_cleanup(args)
            return
        cmd_maintenance_status(args)
        return
    parser.print_help()


if __name__ == "__main__":
    main()
