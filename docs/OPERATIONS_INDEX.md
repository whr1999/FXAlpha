# FXAlpha production operations index

This is the single navigation page for production duty and controlled changes.
Business algorithms and formulas remain authoritative in
[`BUSINESS_WORKFLOWS.md`](BUSINESS_WORKFLOWS.md); this page covers status,
operations, incident handling, and rollback.

## Daily duty order

1. Check `/health` and confirm both FXAlpha API and QuantGPT are reachable.
2. Read `data-status` and verify the production dataset pointer, audit, and target date.
3. Inspect factor, model, prediction, and paper lanes independently.
4. Run the lane-specific preflight before every write and stop on blockers.
5. Close data duty only with `post_promote_audit=passed` and `production_health=ready`.
6. Advance paper trading only when model, prediction, and trading gates all pass.

## Task routing

| Task | Authoritative document | Main entrypoints |
| --- | --- | --- |
| Daily data | [`DATA_FOUNDATION_DAILY_RUNBOOK_CURRENT.md`](DATA_FOUNDATION_DAILY_RUNBOOK_CURRENT.md) | `data-daily-preflight`, `data-daily-routine` |
| Full rebuild | [`DATA_FOUNDATION_TUSHARE_REBUILD_RUNBOOK_CURRENT.md`](DATA_FOUNDATION_TUSHARE_REBUILD_RUNBOOK_CURRENT.md) | rebuild preflight and resume |
| Factor mining | [`FACTOR_RESEARCH_OPERATIONS.md`](FACTOR_RESEARCH_OPERATIONS.md) | `factor-orch status/start/pause/resume` |
| Model research | [`MODEL_RESEARCH_PRODUCTION_RUNBOOK.md`](MODEL_RESEARCH_PRODUCTION_RUNBOOK.md) | model preflight, ORCH/MCP, Rolling |
| Prediction and paper | [`TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md`](TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md) | prediction and paper-fleet preflight |
| Disk governance | [`PLATFORM_OPS_RUNBOOK.md`](PLATFORM_OPS_RUNBOOK.md) | maintenance safe preview / execute |
| Deployment and rollback | [`LOCAL_DEPLOYMENT.md`](LOCAL_DEPLOYMENT.md) | isolated config, shadow ports, systemd |

## Write gates and state truth

Data promotion, factor import/retirement, model promotion, prediction writes,
paper fleet, cleanup execute, and service cutover require an idle lane and an
identified lock owner. Never delete a lock merely to make a lane appear idle.
`storage/paths.py` is the path authority, `FXALPHA_CONFIG_FILE` selects the
deployment configuration, and `paths.runtime_root` keeps mutable state outside
an immutable release. GUI pages are projections rather than duplicate stores.

## Incident and rollback order

Record the lane, task ID, target date, stage, and blocker; reconcile service,
process, lock, task database, and state-file evidence; resume the same durable
task where supported; then rerun the lane audit and downstream identity checks.
Shadow releases use separate runtime, copied SQLite databases, and separate
ports. A failed cutover restores the old units and release without speculative
database repair.

Document classification is maintained in
[`DOCUMENTATION_MANIFEST.yaml`](DOCUMENTATION_MANIFEST.yaml). Dated reports are
point-in-time evidence and never override a current runbook.
