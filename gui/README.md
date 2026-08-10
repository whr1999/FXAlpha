# FXAlpha GUI

This folder is the frontend entrypoint for FXAlpha.

## Purpose

The GUI should remain thin:

- read status from HTTP API
- launch platform workflows through HTTP API
- display latest runtime state clearly

It should not duplicate backend business logic.

## Appearance and refresh behavior

The rail theme switch changes between the default dark theme and the equivalent
light theme. The choice is stored in the browser; on a first visit the GUI uses
the operating-system preference and safely falls back to dark. Theme selection
does not change API requests or business state.

Normal page refreshes use compact read-only projections where available. Full
payloads remain available to callers that omit `compact=true`. Background
polling pauses while the tab is hidden and resumes with one fresh snapshot when
the tab becomes visible again.

## Phase 1 pages

1. Overview
2. Data Foundation
3. Factor Research
4. Model Training
5. Prediction And Trading
6. End-to-End Pipeline

See:

- `./docs/PROJECT_STRUCTURE_CURRENT.md`
- `./docs/QUANTGPT_WORKFLOW_GAP_ANALYSIS.md`

## Local serving

The backend API server now serves this folder at:

- `GET /gui/`

Recommended startup:

```bash
cd <repo-root>
systemctl --user restart fxalpha-api-18081.service
```

Then open:

- `http://127.0.0.1:18081/gui/`

For an unmanaged local debug server, choose an explicit non-production port. The
managed GUI/API truth for the Windows desktop is port `18081`.

## Model research workbench

The model workbench is a thin projection of the backend state:

- `POST /model/orchestrator/start` asynchronously starts Research or production Rolling and returns `accepted`.
- `GET /model/status` is the live status source; a successful start response is not training completion.
- `POST /model/jobs/stop` requests a safe-boundary stop.
- `POST /model/jobs/resume` resumes an interrupted/failed task with its completed evidence.

Only one managed model task may run at a time. The GUI must show Research models,
Rolling campaigns, and Production models as separate business categories, while
keeping Seed17/83 in stability-audit details rather than as selectable formal models.

## Factor research workbench

The factor research page is now intended to show one complete research session:

- launch settings
- runtime session summary
- current prompt and seed prompts
- evolution timeline
- candidate factors and backtests
- natural-language operator guidance
- recent research notes
- factor library snapshot

## Recommendation trading cockpit

The 模拟交易 page now follows the production Qlib paper fleet contract:

- `GET /paper/fleet/status` drives the multi-account overview, account comparison, gaps, deployments and latest fleet run.
- `GET /paper/fleet/preflight` is the read-only production gate.
- `POST /paper/accounts` creates an isolated account and production-model deployment.
- `POST /paper/fleet/run` advances every active account idempotently through the latest promoted Qlib date.
- `GET /paper/replay/plan` and `POST /paper/replay/run` plan and execute complete historical signal/trade/snapshot replay.
- All production write routes require `confirm: true`.
- The legacy fixed 历史研究回测 page, `POST /trade/sim`, and `trade-sim` CLI have been retired; formal research belongs to the model-research workflow rather than the production paper-account console.

The cockpit defaults to read-only. Account creation, fleet execution and replay live under 高级操作 and require confirmation. It displays data/model/deployment state, account-isolated pending and execution state, positions, PnL, account curves, replay gaps and multi-account comparison. Qlib owns deal price, fees, trade-unit rounding and limit checks; FXAlpha freezes scores, targets, fills, run events and account snapshots.

## Backend modes

### Console mode

When the backend exposes the latest research-console routes, the GUI uses:

- `GET /factor/console/live`
- `GET /factor/console/full`
- `GET /factor/console`
- `POST /factor/research/start`
- `POST /factor/research/guidance`

This enables async runs, live progress, and operator intervention during execution.

For Codex-native factor mining, `/factor/console/live` is the fast polling
source. The backend aggregates GUI state from:

- `runtime/factor_research/research_steps/current.jsonl` for LLM/Codex research
  decisions, four-step analysis, thesis state, runtime status, counters, and
  next actions;
- QuantGPT task store for score/backtest/anti-overfit/adversarial task evidence;
- factor registry and knowledge notes for active factors, imports, thesis cards,
  and reusable findings.

`runtime/factor_research/jobs/*.json` is deprecated for factor research. Older
files may remain for audit, but live GUI progress should be written through
`fxalpha_record_research_step` and surfaced from `research_steps/current.jsonl`.

## Data foundation workbench

The data foundation page is split into three workspace tabs:

- `GET /data/status` refreshes the read-only snapshot.
- `GET /data/live-status` refreshes the lightweight live update floor for daily/full-rebuild progress.
- `GET /data/query/fields` lists queryable production HDF fields and default field groups.
- `GET /data/query` reads one stock/index code from the production HDF for charting and quality inspection.
- `POST /data/daily-preflight` runs the Tushare direct-network and production-safety preflight.
- `POST /data/stage-update` with `dry_run=true` previews the staged daily merge package.
- `POST /data/update/start` starts a GUI-visible async daily or full-rebuild job; it returns immediately and writes `runtime/data_foundation/gui_jobs/<job_id>.json`.

`数据情况` keeps the production date, coverage, quality, and safe data-operation controls. `更新现场` polls `/data/live-status` every 30 seconds only while the data-foundation live tab is visible; users can click the manual refresh button for higher-frequency checks. `数据库查询` supports one code at a time, field selection, HS300 benchmark comparison, and `raw` / `index100` / `zscore` / `pct_change` chart transforms.

The legacy synchronous `POST /data/daily-routine` endpoint remains available for compatibility, but the GUI uses `/data/update/start` so users can monitor download, conversion, merge, quality, and compatibility-output progress while the job is running.

The backend routine is:

1. direct Tushare preflight against the current production pointer
2. short-window Tushare rebuild from the current production latest date
3. compatibility export for that short window
4. write `data/raw/tushare/trade_calendar.txt` for the staged production bundle
5. merge the refreshed window back into the current production full HDF
6. regenerate Qlib and QuantGPT staged outputs directly from raw HDF
7. staged quality check
8. promote only after the staged package is complete

The status panel now treats latest-day stale stocks separately from true field-quality failures:

- `data_quality_summary.latest_code_activity.stale_stock_count` shows names whose last trade date is older than the HDF5 latest trade date.
- Historical full-rebuild metadata count mismatches are shown as warnings, not hard failures, because post-repair staged bundles can outgrow the raw-window snapshot stored in `metadata.json`.

The legacy `POST /data/refresh` endpoint has been removed. Use the staged endpoints above for all GUI and manual data-foundation work.

AmazingData has been removed from the workspace. Tushare daily updates must use the domestic direct route rather than mihomo/TUN/proxy. Missing data is not downgraded to a fallback mode; staging fails and production remains unchanged. The current backend policy is documented in `./docs/DATA_FOUNDATION_DIRECT_NETWORK_AND_QUALITY_POLICY_CURRENT.md`.

### Compatibility mode

If the running backend is older and does not yet expose `/factor/console`, the GUI falls back to:

- `GET /factor/status`
- `GET /factors`
- synchronous `POST /factor/research`

This keeps the workbench usable, but disables live event streaming and guidance until the API server is restarted with the latest backend code.

## Current files

- `index.html` dashboard shell
- `app.js` API bindings and page interactions
- `styles.css` visual system


## Schema V2 data foundation status

The data foundation page now surfaces Schema V2 rebuild metadata directly from the backend quality summary:

- `schema_version`
- `price_mode`
- `cache_mode`
- `effective_target_date`
- `factor_adjusted_quality`

The GUI does not offer a legacy/new toggle. Data foundation workflows should assume Schema V2 semantics once the staged package becomes the promote candidate.
