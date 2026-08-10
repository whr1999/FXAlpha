# Current runtime and data paths

`storage/paths.py` is the code authority. Relative configuration values are
resolved against the repository root, not the caller's working directory.
`paths.runtime_root` is the single root for mutable run evidence. In production
it should be an absolute directory outside the immutable release checkout.

| Path | Contract |
| --- | --- |
| `data/raw/tushare/` | promoted raw/compatible market data |
| `data/qlib/` | Qlib provider assets |
| `data/quantgpt/` | QuantGPT research-facing parquet assets |
| `data/factors/` | factor registry and adopted values |
| `data/model/` | model registry and feature snapshots |
| `data/trading/` | execution/account registry |
| `runtime/data_foundation/` | staged packages, production pointer, audits |
| `runtime/factor_research/` | run journals, events, traces, locks |
| `runtime/model/jobs/` | managed job metadata and worker logs |
| `runtime/model/rolling/` | rolling-campaign evidence |
| `runtime/model/active_production_model.json` | active production model pointer |
| `runtime/trading/` | recommendation, execution, paper-fleet evidence |

Important files include `jobs/<job_id>.log`,
`rolling/<campaign_id>/campaign.json`, and `active_production_model.json`.

All paths in this document are local state and Git-ignored. Operators must back
up durable registries separately and must not manually delete active locks or
staged packages. Third-party source is under `third_party/`, not `data/` or
`runtime/`. QuantGPT report output is similarly externalized with
`QUANTGPT_REPORTS_DIR`; it must not be written into the pinned submodule.
