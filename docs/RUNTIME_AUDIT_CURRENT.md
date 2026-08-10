# Current runtime audit contract

This document defines runtime ownership; it is not a snapshot of a particular
production host. Runtime files are intentionally absent from the public source
repository.

## Data foundation

- `runtime/data_foundation/CURRENT_PRODUCTION_DATASET.json` is the production
  dataset pointer.
- `runtime/data_foundation/staging/` contains resumable packages and locks.
- promotion and post-promotion audit evidence must be retained together.

## Factor research

- research steps, orchestrator events, LLM traces, and task references live
  under `runtime/factor_research/`.
- diagnostic/archive files must never replace the current run truth source.

## Model

- `runtime/model/jobs/<job_id>.json` owns managed job state.
- `runtime/model/jobs/<job_id>.log` contains worker logs.
- `runtime/model/rolling/<campaign_id>/campaign.json` is rolling evidence.
- `runtime/model/active_production_model.json` is the active production model
  pointer.

## Qlib paper trading

- `runtime/trading/fleet/latest_status.json` is the fleet snapshot.
- recommendations, frozen execution inputs, risk decisions, and account
  snapshots are stored under `runtime/trading/` and the trading registry.
- vn.py runtime and `.vntrader` are not current state sources.

## Cleanup boundary

Cleanup must use the platform's governed maintenance service, respect locks and
retention rules, and write a receipt. Databases, active status, promoted
pointers, current runs, and rollback backups are never generic cache targets.
