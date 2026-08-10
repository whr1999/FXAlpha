"""Production FXAlpha model MCP server."""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path
from typing import Any, Callable, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import model_service as svc  # noqa: E402
from services._base import err_result  # noqa: E402
from storage.paths import (  # noqa: E402
    MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    MODEL_DEFAULT_FORWARD_PERIOD,
    MODEL_DEFAULT_START_DATE,
    MODEL_DEFAULT_STATUS_FILTER,
)

T = TypeVar("T")


def _quiet_call(fn: Callable[..., T], *args, **kwargs) -> T:
    with contextlib.redirect_stdout(sys.stderr):
        return fn(*args, **kwargs)


def _dump(result) -> dict[str, Any]:
    """Return the shared ServiceResult envelope as structured MCP output."""
    return result.to_dict()


mcp = FastMCP(
    "fxalpha-model",
    instructions=(
        "FXAlpha model MCP. Research runs Seed 42 per round and confirms only the session-best round with 17/83. "
        "Production runs four-fold rolling with Top20/Drop2/Hold5 and creates candidate only after formal rolling gates."
    ),
    streamable_http_path="/",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=["localhost", "localhost:8004", "127.0.0.1", "127.0.0.1:8004"],
    ),
)


@mcp.tool()
def fxalpha_model_context(stage: str = "context_review", round_group_id: str = "", feature_set_id: str = "", job_id: str = "", run_id: str = "") -> dict[str, Any]:
    """读取生产模型研究上下文。"""
    return _dump(
        _quiet_call(
            svc.model_tool_context,
            stage=stage,
            round_group_id=round_group_id or None,
            feature_set_id=feature_set_id or None,
            job_id=job_id or None,
            run_id=run_id or None,
        )
    )


@mcp.tool()
def fxalpha_model_protocol() -> dict[str, Any]:
    """读取生产模型训练协议和 prompt contract。"""
    return _dump(_quiet_call(svc.model_tool_protocol))


@mcp.tool()
def fxalpha_model_monitor(session_id: str = "") -> dict[str, Any]:
    """读取生产模型 ORCH 状态。"""
    return _dump(_quiet_call(svc.model_orchestrator_status))


@mcp.tool()
def fxalpha_model_feature_snapshot(
    feature_set_id: str = "",
    status_filter: str = MODEL_DEFAULT_STATUS_FILTER,
    start_date: str = MODEL_DEFAULT_START_DATE,
    end_date: str | None = None,
    label_forward_period: int = MODEL_DEFAULT_FORWARD_PERIOD,
    factor_holding_period_days: int = MODEL_DEFAULT_FACTOR_HOLDING_PERIOD,
    factor_ids: list[str] | None = None,
    feature_missing_strategy: str = "qlib_processor_only",
    dry_run: bool = False,
    source_feature_set_id: str = "",
    job_id: str = "",
) -> dict[str, Any]:
    """冻结生产模型 feature set。"""
    return _dump(
        _quiet_call(
            svc.model_tool_feature_snapshot,
            feature_set_id=feature_set_id or None,
            status_filter=status_filter,
            start_date=start_date,
            end_date=end_date,
            label_forward_period=label_forward_period,
            factor_holding_period_days=factor_holding_period_days,
            factor_ids=factor_ids,
            feature_missing_strategy=feature_missing_strategy,
            dry_run=dry_run,
            source_feature_set_id=source_feature_set_id or None,
            job_id=job_id,
        )
    )


@mcp.tool()
def fxalpha_model_session_start(feature_set_id: str = "", job_id: str = "", run_id: str = "") -> dict[str, Any]:
    """开始单窗口研究 session；生产 Rolling 使用统一 orchestrator 入口。"""
    return _dump(_quiet_call(svc.model_tool_session_start, feature_set_id=feature_set_id or None, job_id=job_id or None, run_id=run_id or None))


@mcp.tool()
def fxalpha_model_submit_experiment(feature_set_id: str, experiment_json: dict, job_id: str = "", run_id: str = "") -> dict[str, Any]:
    """提交研究 round；计划 42/17/83，但本轮先只执行 Seed 42。"""
    return _dump(_quiet_call(svc.model_tool_submit_experiment, feature_set_id=feature_set_id, experiment=experiment_json, job_id=job_id or None, run_id=run_id or None))


@mcp.tool()
def fxalpha_model_run_round(round_group_id: str, execute_qlib: bool = False, job_id: str = "", run_id: str = "") -> dict[str, Any]:
    """执行普通研究 round 的 Seed 42 训练和回测。"""
    return _dump(_quiet_call(svc.model_tool_run_round, round_group_id=round_group_id, execute_qlib=execute_qlib, job_id=job_id or None, run_id=run_id or None))


@mcp.tool()
def fxalpha_model_score_review(round_group_id: str, job_id: str = "", run_id: str = "") -> dict[str, Any]:
    """计算 Seed 42 研究评分。"""
    return _dump(_quiet_call(svc.model_tool_score_review, round_group_id=round_group_id, job_id=job_id or None, run_id=run_id or None))


@mcp.tool()
def fxalpha_model_confirm_research_round(round_group_id: str, execute_qlib: bool = False, write_registry: bool = True, job_id: str = "", run_id: str = "") -> dict[str, Any]:
    """仅为研究会话最优轮补跑 Seed 17/83 并计算确认结果。"""
    return _dump(_quiet_call(svc.model_tool_confirm_research_round, round_group_id=round_group_id, execute_qlib=execute_qlib, write_registry=write_registry, job_id=job_id or None, run_id=run_id or None))


@mcp.tool()
def fxalpha_model_start_production_rolling(source_round_group_id: str, write_registry: bool = True, campaign_id: str = "") -> dict[str, Any]:
    """对已通过研究确认的 round 启动正式四折 Rolling。"""
    return _dump(_quiet_call(svc.model_tool_start_production_rolling, source_round_group_id=source_round_group_id, write_registry=write_registry, campaign_id=campaign_id or None))


@mcp.tool()
def fxalpha_model_round_synthesis(round_group_id: str, round_no: int = 1, write_registry: bool = False, job_id: str = "", run_id: str = "") -> dict[str, Any]:
    """确定性汇总 Seed 42 研究结果；此步骤不调用 DeepSeek。"""
    return _dump(_quiet_call(svc.model_tool_round_synthesis, round_group_id=round_group_id, round_no=round_no, write_registry=write_registry, job_id=job_id or None, run_id=run_id or None))


@mcp.tool()
def fxalpha_model_orchestrator_start(
    evaluation_mode: str = "research",
    feature_set_id: str = "",
    source_round_group_id: str = "",
    n_rounds: int = 1,
    max_stage: str = "round_synthesis",
    run_id: str = "",
    session_id: str = "",
    parent_job_id: str = "",
    execute_qlib: bool = False,
    write_registry: bool = False,
) -> dict[str, Any]:
    """统一入口：research 运行单窗口研究，production 运行四折 Rolling。"""
    return _dump(
        _quiet_call(
            svc.model_orchestrator_start,
            evaluation_mode=evaluation_mode,
            feature_set_id=feature_set_id or None,
            source_round_group_id=source_round_group_id or None,
            n_rounds=n_rounds,
            max_stage=max_stage,
            run_id=run_id or None,
            session_id=session_id or None,
            parent_job_id=parent_job_id or None,
            execute_qlib=execute_qlib,
            write_registry=write_registry,
        )
    )


@mcp.tool()
def fxalpha_model_orchestrator_status() -> dict[str, Any]:
    """读取生产 ORCH 状态。"""
    return _dump(_quiet_call(svc.model_orchestrator_status))


@mcp.tool()
def fxalpha_model_orchestrator_events(limit: int = 80, include_payload: bool = False, job_id: str = "", run_id: str = "", session_id: str = "") -> dict[str, Any]:
    """读取生产 ORCH events。"""
    return _dump(
        _quiet_call(
            svc.model_orchestrator_events,
            limit=limit,
            include_payload=include_payload,
            job_id=job_id or None,
            run_id=run_id or None,
            session_id=session_id or None,
        )
    )


@mcp.tool()
def fxalpha_model_orchestrator_traces(limit: int = 50, include_payload: bool = False, job_id: str = "", run_id: str = "", session_id: str = "") -> dict[str, Any]:
    """读取生产 ORCH traces。"""
    return _dump(
        _quiet_call(
            svc.model_orchestrator_traces,
            limit=limit,
            include_payload=include_payload,
            job_id=job_id or None,
            run_id=run_id or None,
            session_id=session_id or None,
        )
    )


@mcp.tool()
def fxalpha_model_promote(model_id: str = "", model_run_id: str = "", execute_qlib: bool = True, dry_run: bool = False, manual_override_reason: str = "") -> dict[str, Any]:
    """把正式 Rolling candidate 固定以 Seed42 refit 后提升为 production；人工例外必须提供审计原因。"""
    return _dump(
        _quiet_call(
            svc.model_promote,
            model_id=model_id or None,
            model_run_id=model_run_id or None,
            execute_qlib=execute_qlib,
            dry_run=dry_run,
            manual_override_reason=manual_override_reason or None,
        )
    )


@mcp.tool()
def fxalpha_model_status() -> dict[str, Any]:
    """读取生产模型状态。"""
    return _dump(_quiet_call(svc.model_status))


@mcp.tool()
def fxalpha_record_model_step(stage: str = "note", summary: str = "", decision: str = "", next: str = "", refs: list[str] | None = None, round_group_id: str = "", model_run_id: str = "", feature_set_id: str = "") -> dict[str, Any]:
    return _dump(
        _quiet_call(
            svc.model_tool_research_step,
            stage=stage,
            summary=summary,
            decision=decision,
            next=next,
            refs=refs or [],
            round_group_id=round_group_id,
            model_run_id=model_run_id,
            feature_set_id=feature_set_id,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8004)
    args = parser.parse_args()

    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
        return
    if args.transport == "sse":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="sse")
        return
    mcp.run()


if __name__ == "__main__":
    main()
