"""FXAlpha platform governance MCP server.

Boundary:
- QuantGPT MCP owns factor research and expression validation.
- FXAlpha model MCP owns model research and training.
- This server owns platform asset governance such as factor-library audit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.factor_library_audit_service import (  # noqa: E402
    factor_feature_set_recommendations,
    factor_library_audit,
    factor_library_audit_status,
    factor_retire_plan,
)
from services.factor_map_service import factor_map_status  # noqa: E402
from services._base import err_result  # noqa: E402
from services.maintenance_service import maintenance_cleanup, maintenance_status  # noqa: E402
from services.platform_gui_service import platform_gui_start, platform_gui_status  # noqa: E402
from services.trading_service import (  # noqa: E402
    trading_daily_preflight,
    trading_execute_pending,
    trading_paper_backfill,
    trading_status,
)
from storage.paths import MODEL_DEFAULT_TOPK  # noqa: E402


mcp = FastMCP(
    "fxalpha-platform",
    instructions=(
        "FXAlpha platform governance MCP. Use this server for cross-module asset governance, "
        "not for QuantGPT factor mining or model research. Factor-library audit is "
        "read-only by default: it computes factor health, factor-value correlation clusters, "
        "stable factor-map regions and lineage, cluster representatives by admission score, feature-set recommendations, retire "
        "plans that require human confirmation, and platform maintenance cleanup previews. "
        "Cleanup tools reuse FXAlpha protected-path policy and must not be used to bypass "
        "factor/model asset protections. Production cleanup should use safe preview first, "
        "then safe execute only after explicit user confirmation. Data-foundation staging "
        "and production backup packages are governable runtime artifacts, but current "
        "production references, recent packages, fresh packages, and running/locked updates "
        "must remain blocked. GUI service tools in this server only ensure the FXAlpha GUI/API "
        "and QuantGPT HTTP API are available; they do not start factor research, model training, "
        "research_steps logging, or any runner. Trading paper-account tools operate only on "
        "recommendation execution, Qlib paper account snapshots, and mark-to-market backfill; "
        "they do not train models or mine factors. GUI/HTTP/CLI are fallback or display paths, not "
        "the primary governance entrypoint."
    ),
    streamable_http_path="/",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=["localhost", "localhost:8005", "127.0.0.1", "127.0.0.1:8005"],
    ),
)


def _dump(result) -> dict[str, Any]:
    """Return the shared ServiceResult envelope as structured MCP output."""
    return result.to_dict()


def _parse_retention_days_json(retention_days_json: str | None, *, profile: str, execute: bool):
    if not retention_days_json:
        return None, None
    try:
        parsed = json.loads(retention_days_json)
    except json.JSONDecodeError as exc:
        return None, err_result(
            "invalid_retention_days_json",
            inputs={
                "profile": profile,
                "execute": execute,
                "retention_days_json": retention_days_json,
            },
            outputs={"detail": str(exc)},
        )
    if not isinstance(parsed, dict):
        return None, err_result(
            "invalid_retention_days_json",
            inputs={
                "profile": profile,
                "execute": execute,
                "retention_days_json": retention_days_json,
            },
            outputs={"detail": "retention_days_json must decode to a JSON object."},
        )
    retention_days: dict[str, int] = {}
    for key, value in parsed.items():
        try:
            retention_days[str(key)] = int(value)
        except (TypeError, ValueError):
            return None, err_result(
                "invalid_retention_days_json",
                inputs={
                    "profile": profile,
                    "execute": execute,
                    "retention_days_json": retention_days_json,
                },
                outputs={"detail": f"retention day for {key!r} must be an integer."},
            )
    return retention_days, None


@mcp.tool()
def fxalpha_factor_library_audit(
    scope: str = "all",
    status_filter: str = "active",
    save_report: bool = True,
    include_feature_sets: bool = True,
    audit_window_start: str | None = None,
    audit_window_end: str | None = None,
    min_valid_days: int = 120,
    min_common_stocks: int = 300,
    redundancy_threshold_rank_p90: float = 0.80,
    redundancy_threshold_pearson_p90: float = 0.75,
    family_dependency_cut: float = 0.55,
) -> dict[str, Any]:
    """Run quality, information-cluster, or combined factor-library audit."""
    return _dump(
        factor_library_audit(
            scope=scope,
            status_filter=status_filter,
            save_report=save_report,
            include_feature_sets=include_feature_sets,
            audit_window_start=audit_window_start,
            audit_window_end=audit_window_end,
            min_valid_days=min_valid_days,
            min_common_stocks=min_common_stocks,
            redundancy_threshold_rank_p90=redundancy_threshold_rank_p90,
            redundancy_threshold_pearson_p90=redundancy_threshold_pearson_p90,
            family_dependency_cut=family_dependency_cut,
        )
    )


@mcp.tool()
def fxalpha_factor_audit_status(scope: str = "all") -> dict[str, Any]:
    """Read quality, information, or combined latest audit without recomputing it."""
    return _dump(factor_library_audit_status(scope=scope))


@mcp.tool()
def fxalpha_factor_map_status(region_uid: str = "") -> dict[str, Any]:
    """Read the unified factor map without recomputing it or starting research."""
    return _dump(factor_map_status(region_uid=region_uid))


@mcp.tool()
def fxalpha_factor_feature_set_recommendations() -> dict[str, Any]:
    """Return feature-set recommendations derived from the latest factor-library audit."""
    return _dump(factor_feature_set_recommendations())


@mcp.tool()
def fxalpha_factor_retire_plan() -> dict[str, Any]:
    """Return a read-only retire/watch plan; this tool never modifies factor_registry.db."""
    return _dump(factor_retire_plan())


@mcp.tool()
def fxalpha_platform_maintenance_status() -> dict[str, Any]:
    """Return platform disk audit, cleanup preview, and service-health status."""
    return _dump(maintenance_status())


@mcp.tool()
def fxalpha_platform_gui_start() -> dict[str, Any]:
    """Idempotently ensure the FXAlpha GUI services are healthy, then open gui_url in Codex browser manually."""
    return _dump(platform_gui_start())


@mcp.tool()
def fxalpha_platform_gui_status() -> dict[str, Any]:
    """Inspect FXAlpha GUI service health, ports, pid files, and recent stderr tails without starting anything."""
    return _dump(platform_gui_status())


@mcp.tool()
def fxalpha_platform_cleanup_preview(
    profile: str = "safe",
    retention_days_json: str | None = None,
) -> dict[str, Any]:
    """Dry-run platform cleanup. This never deletes files."""
    retention_days, error = _parse_retention_days_json(retention_days_json, profile=profile, execute=False)
    if error:
        return _dump(error)
    return _dump(maintenance_cleanup(profile=profile, execute=False, retention_days=retention_days))


@mcp.tool()
def fxalpha_platform_cleanup_execute(
    profile: str = "safe",
    retention_days_json: str | None = None,
) -> dict[str, Any]:
    """Execute platform cleanup through the existing safe cleanup policy. Requires explicit user confirmation before use."""
    if profile != "safe":
        return _dump(
            err_result(
                "cleanup_execute_profile_not_allowed",
                inputs={"profile": profile},
                outputs={"detail": "fxalpha-platform MCP execute only allows profile='safe'; run preview and use manual fallback for non-safe cleanup."},
                warnings=["non_safe_cleanup_execute_rejected"],
            )
        )
    retention_days, error = _parse_retention_days_json(retention_days_json, profile=profile, execute=True)
    if error:
        return _dump(error)
    result = maintenance_cleanup(profile=profile, execute=True, retention_days=retention_days)
    return _dump(result)


@mcp.tool()
def fxalpha_trading_paper_status(model_id: str = "", model_run_id: str = "") -> dict[str, Any]:
    """Read Qlib paper-account trading status, latest recommendation, latest fills, account snapshot, and ledger paths."""
    return _dump(trading_status(model_id=model_id or None, model_run_id=model_run_id or None))


@mcp.tool()
def fxalpha_trading_paper_preflight(
    model_id: str = "",
    model_run_id: str = "",
    signal_date: str = "",
    topk: int = MODEL_DEFAULT_TOPK,
    total_capital: float = 1_000_000.0,
) -> dict[str, Any]:
    """Run the read-only safety gate before Qlib paper-account daily trading."""
    return _dump(
        trading_daily_preflight(
            model_id=model_id or None,
            model_run_id=model_run_id or None,
            signal_date=signal_date or None,
            topk=topk,
            total_capital=total_capital,
        )
    )


@mcp.tool()
def fxalpha_trading_paper_execute_pending(
    recommendation_id: str = "",
    total_capital: float | None = None,
) -> dict[str, Any]:
    """Execute pending recommendation batches through Qlib paper account; use after preflight or explicit operator request."""
    return _dump(
        trading_execute_pending(
            recommendation_id=recommendation_id or None,
            total_capital=total_capital,
        )
    )


@mcp.tool()
def fxalpha_trading_paper_backfill(
    model_id: str = "",
    model_run_id: str = "",
    target_date: str = "",
    total_capital: float = 1_000_000.0,
) -> dict[str, Any]:
    """Mark an existing Qlib paper account to market for missed data days without generating new recommendations."""
    return _dump(
        trading_paper_backfill(
            model_id=model_id or None,
            model_run_id=model_run_id or None,
            target_date=target_date or None,
            total_capital=total_capital,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="FXAlpha platform governance MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8005)
    args = parser.parse_args()
    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
