# FXAlpha Platform Ops Runbook

Updated: 2026-07-10

## Purpose

Platform Ops is the shared maintenance layer for FXAlpha. It does not belong to only factor mining, model training, or data foundation. Its job is to keep runtime caches, logs, experiment workspaces, and generated reports under control while protecting production data assets.

## Scope

Protected by default:

- `data/quantgpt/stocks`
- `data/qlib`
- `data/factors/factor_registry.db`
- `data/factors/active_adopted_factor_values.parquet` (shared canonical store for FXAlpha and embedded QuantGPT)
- `data/model/model_registry.db`
- `data/model/features/active`
- `third_party/quantgpt/research_notes`

Cleanup candidates:

- Python cache directories: `__pycache__`, `.pytest_cache`
- RD-Agent cache: `pickle_cache` regenerable `.pkl` / `.pickle` / `.zip` / `.joblib` files older than 1 day
- QuantGPT report HTML files older than 7 days: `third_party/quantgpt/reports`
- Runtime reset backups older than 7 days: `runtime/reset_backups`
- Runtime logs older than 3 days: `log`
- Data foundation repair/diagnostic backups older than 2 days: `runtime/data_foundation/backups`
- Trading prediction feature snapshots older than 30 days (aggressive profile only): `runtime/trading/prediction_features`
- Completed factor-value repair runs older than 2 days: `runtime/factor_research/value_repair`
- Quarantined bad model feature snapshots older than 2 days: `runtime/model/quarantine`
- Historical factor-research trace/event files older than 30 days:
  - `runtime/factor_research/orchestrator_llm_traces/history`
  - `runtime/factor_research/orchestrator_events/history`
- Factor-research repair backups older than 7 days: `runtime/factor_research/repair_backups`
- Factor-research registry backups older than 14 days: `runtime/factor_research/registry_backups`
- Old model run diagnostics older than 30 days (aggressive profile only): `runtime/model/runs`
- Old model feature sets (aggressive profile only; all registry-referenced snapshots remain protected)
- Data foundation daily-update runtime packages:
  - `runtime/data_foundation/staging`
  - `runtime/data_foundation/production_backups`
- Stale pytest sessions and completed `runtime/tmp/fxalpha_*` tasks older than 1 day, only when no live process or lock owns them

## Interfaces

Production governance entrypoint:

```text
fxalpha-platform MCP
```

Use these MCP tools for routine platform governance:

```text
fxalpha_platform_maintenance_status()
fxalpha_platform_cleanup_preview(profile="safe")
fxalpha_platform_cleanup_execute(profile="safe")
```

Production governance prefers `fxalpha-platform` MCP. GUI uses the HTTP API for display and explicit operator actions. CLI is only a manual/failure fallback or regression-check surface.

HTTP:

```bash
GET  /maintenance/status
POST /maintenance/cleanup
```

Cleanup body:

```json
{
  "profile": "safe",
  "execute": false,
  "retention_days": {
    "pickle_cache": 1,
    "logs": 3,
    "quantgpt_reports": 7,
    "reset_backups": 7,
    "data_foundation_keep_extra": 2,
    "data_foundation_min_age_days": 7,
    "data_foundation_misc_backups": 2,
    "trading_prediction_features": 30,
    "trading_prediction_features_keep_latest": 1,
    "factor_value_repair": 2,
    "factor_value_repair_keep_latest": 1,
    "model_quarantine": 2,
    "model_quarantine_keep_latest": 1,
    "factor_research_trace_history": 30,
    "factor_research_event_history": 30,
    "factor_research_repair_backups": 7,
    "factor_research_repair_backups_keep_latest": 1,
    "factor_research_registry_backups": 14,
    "runtime_test_tmp": 1,
    "runtime_task_tmp": 1
  }
}
```

CLI fallback:

```bash
python3 cli.py maintenance status
python3 cli.py maintenance cleanup --profile safe
```

Do not use CLI execute as the routine production path. If MCP transport is unavailable and a human explicitly approves cleanup after reading the safe preview, CLI execute may be used as a documented emergency fallback:

```bash
python3 cli.py maintenance cleanup --profile safe --execute
```

GUI:

- Platform overview contains a "Platform Ops" card.
- The default action is dry-run preview.
- Execute action is limited to `safe` profile and requires confirmation.

Windows doctor:

```powershell
.\fxalpha_doctor.cmd -Action status
.\fxalpha_doctor.cmd -Action open-gui
.\fxalpha_doctor.cmd -Action recover-safe
```

`open-gui` checks the 18081 API first, starts it through the normal API start path if needed, and then opens `http://127.0.0.1:18081/gui/` from Windows.

MCP transport fallback:

1. First call `fxalpha_platform_maintenance_status()` or `fxalpha_platform_cleanup_preview(profile="safe")`.
2. If the Codex MCP transport reports `Transport closed`, verify the server code imports with `python3 -c "import mcp_servers.platform_server"` and check HTTP health at `GET /health`.
3. Use HTTP `GET /maintenance/status` or `POST /maintenance/cleanup` only as fallback diagnostics.
4. Use CLI only when MCP and HTTP are unavailable or for local regression checks.

## Operating Rules

- Always run dry-run before execute.
- Prefer `fxalpha-platform` MCP for production governance.
- Use GUI / HTTP for operator display and fallback checks.
- Use CLI only for manual/failure fallback or local regression checks.
- Use `safe` for routine cleanup.
- `safe` is directly enhanced; there is no `safe_plus` profile.
- `safe` covers pickle cache, old data-foundation packages, old reset backups, old model feature sets, and old RD-Agent workspaces.
- `safe` also covers old data-foundation repair/diagnostic backups and old trading prediction feature snapshots.
- `aggressive` is not the routine recommendation and must not be used as the default GUI/operator action.
- Never use OS-level recursive deletion against `data`, `third_party/quantgpt/research_notes`, or registry databases.
- If factor mining and model training run in parallel, do not clean active model feature sets or current RD-Agent workspaces.
- For data foundation daily-update runtime packages, safe cleanup must keep:
  - the current production staging package from `CURRENT_PRODUCTION_DATASET.json`
  - the current promotion backup from `CURRENT_PRODUCTION_DATASET.json`
  - the latest 1 extra staging package
  - the latest 1 extra production backup
  - packages modified within the last 24 hours
- If a data-foundation lock exists, or daily status indicates staging/promote is running, all data-foundation cleanup candidates must be blocked.
- For `runtime/data_foundation/backups`, safe cleanup may remove repair/diagnostic backup directories older than 2 days, but must block all candidates while data-foundation locks or running status are present and must block directories modified within the last 24 hours.
- For trading prediction feature snapshots, safe cleanup may remove directories older than 14 days while retaining the latest 1 snapshot.
- For model feature sets, safe cleanup must protect the active snapshot references, feature sets still referenced by production/active/best/latest model registry rows when parseable, the latest 5 feature-set directories, and directories modified within the last 48 hours.
- For RD-Agent workspaces, safe cleanup must protect the latest 10 workspaces, workspaces modified within the last 48 hours, running/locked workspaces, and workspaces linked from the current/latest model status or model registry references when parseable.
- Active, production, recent, running, and locked assets must appear as protected/blocked in preview and must not be executable candidates.
- Cleanup execute requires explicit human confirmation after reviewing a dry-run preview.
- Do not manually delete old data-foundation packages; use MCP preview first, then execute only after explicit confirmation or documented failure fallback.

## Audit evidence boundary

Each deployment keeps its cleanup previews, execute reports, candidate counts,
disk totals, runtime paths, and incident evidence outside the public Git tree.
The public repository contains only the ownership rules and commands needed to
produce those records. A preview is never authorization to execute cleanup;
review its exact protected, blocked, and executable sets and obtain explicit
approval for that deployment.

The public-safe workspace-audit pointer is
[`WORKSPACE_AUDIT_20260620.md`](WORKSPACE_AUDIT_20260620.md). It explains why
the original machine inventory remains private without reproducing its paths
or measurements.

## Stage Roadmap

- Stage 1: backend service, API, CLI, dry-run reports.
- Stage 2: GUI Platform Ops overview card and safe cleanup trigger.
- Stage 3: platform MCP tools for Codex-driven maintenance audit and cleanup preview/execute.
- Stage 4: scheduled automation and alerting.
