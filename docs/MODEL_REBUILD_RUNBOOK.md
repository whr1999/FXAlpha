# MODEL Rebuild Runbook (Historical)

> 本文记录 2026-07-10 的三 Seed + forward + SOTA Gate 旧设计。现行契约已在 2026-07-19 改为研究单 Seed 筛选、优胜轮多 Seed 确认与生产四折 Rolling；请以 `MODEL_RESEARCH_WORKFLOW_CURRENT.md` 为准。

`Model` is the production model workflow. `/model/*` is the canonical API used
by the GUI and all current clients. Versioned compatibility addresses remain
thin aliases into the same handlers for historical automation; they are not a
second workflow. `fxalpha-model` MCP exposes only Model tools. The legacy
RD-Agent module is archived for reference only.

## Workflow

```text
protocol_load
 -> context_review
 -> feature_snapshot
 -> experiment_plan
 -> train_backtest_3seed
 -> score_review
 -> forward_test
 -> SOTA Gate
 -> registry_write
 -> round_synthesis
 -> next experiment_plan / checkpoint_stop
```

Each round is one parameter set, one feature set, and exactly three equal seed
models: the persisted cross-round comparison panel `42`, `17`, `83`. Each seed gets its own
`model_run_id`, artifacts, metrics, validation, `sota_score`, forward-test
evidence, gate decision, and registry status.

Validation audit is a candidate-admission cost, not a blanket post-run cost.
Below-threshold or hard-blocked seeds are archived with a structured
`validation_skipped` reason. Seeds that can still become candidate are validated
before registry admission.

## Time Windows

The canonical model windows are read from `config.yaml:model`, not
hard-coded in callers:

```text
default_segments.train = 2022-01-04 .. 2024-12-31
default_segments.valid = 2025-01-02 .. 2025-06-30
default_segments.test  = 2025-07-01 .. 2026-07-01
```

Changing train/valid/test periods, forward-test slices, or production refit
periods should be done in config first. The contract layer exposes the same
values to ORCH, MCP, GUI, Qlib manifests, and tests.

## Round 0 Default-Parameter Baseline

Every ORCH session first runs Round 0 with the FXAlpha empirical calibrated
LGBM anchor and without an LLM call. Round 1 then tunes from the measured
Round 0 evidence. The `n_rounds` input counts tuning rounds only, so
`n_rounds=5` executes Round 0 plus Round 1-5 (six rounds total).

Round 0 uses the FXAlpha empirically calibrated LGBM anchor, not the raw
Qlib/RD-Agent Alpha158 default. The official Alpha158 parameters remain in the
contract as a reference contrast. The current anchor comes from the 2026-07-18
All33 two-window, three-seed parameter study:

```json
{
  "learning_rate": 0.04,
  "lr": 0.04,
  "num_leaves": 96,
  "max_depth": 8,
  "min_data_in_leaf": 10,
  "lambda_l1": 20,
  "lambda_l2": 50,
  "feature_fraction": 0.9,
  "bagging_fraction": 0.9,
  "bagging_freq": 1,
  "n_estimators": 2400,
  "early_stopping_rounds": 100,
  "bin_construct_sample_cnt": 5000000,
  "seed": 42,
  "feature_fraction_seed": 42,
  "bagging_seed": 42,
  "data_random_seed": 42,
  "drop_seed": 42
}
```

This is a stochastic calibrated anchor. It samples 90% of features and rows,
constructs bins from the full available sample at the current data scale, and
is always evaluated with the persisted `42/17/83` seed panel. Normal production
selection must use median, worst-seed, and dispersion evidence; it must never
choose a model because one test seed happened to win. The deterministic
fallback uses the same capacity and regularization with
`feature_fraction=1.0`, `bagging_fraction=1.0`, and `bagging_freq=0`.

The ORCH Round 0 baseline kind is:

```text
model_orch_round0_baseline
```

Round 1 and later must evolve from evidence and from the parameter-history
ledger. Reusing raw Alpha158 settings such as `learning_rate=0.2` and
`num_leaves=210` is allowed only as an explicit high-capacity contrast
experiment, not as the default.

## Feature Sets

`model` is multi feature-set first. A round must bind to one explicit
`feature_set_id`, and that feature set may come from any immutable model feature
snapshot, including subsets recommended by factor-library audit.

ORCH/MCP sessions are fixed feature-set sessions:

- every round in the session uses the selected `feature_set_id`;
- DeepSeek may change only the nine LGBM parameters listed in the ORCH prompt,
  one coherent group and at most three parameters per round;
- DeepSeek cannot change `feature_set_id` inside a session;
- compare feature sets by running separate fixed-feature-set sessions.

For audit-derived subsets, `feature_snapshot(factor_ids=[...],
source_feature_set_id=...)` materializes a new immutable subset from an existing
source snapshot. It does not require all-active value refresh, does not update the
all-active pointer, and does not use legacy single-snapshot identity logic as a
training gate.

Building a fresh all-active snapshot is still allowed only as an explicit
all-active operation. That path may update the all-active pointer, but it is not
the default identity of model training.

## State Sources

Live truth is under `runtime/model/`:

```text
jobs.sqlite
research_steps/current.jsonl
context_snapshots/<context_id>.json
orchestrator_events/current.jsonl
orchestrator_traces/current.jsonl
mcp_traces/current.jsonl
runs/<model_run_id>/
```

`latest_status.json` is intentionally not used as live truth.

## ORCH Logging

`/model/orchestrator/start` now runs a bounded stage machine. Each stage
writes `stage_start` and `stage_complete` events, updates `jobs.sqlite`, and
records compact research steps. Event and trace endpoints hide large payloads by
default; pass `include_payload=true` for full context/prompt inspection.
Event and trace endpoints are latest-first and accept `job_id` or `run_id`, so
the GUI can default to the current job while keeping older jobs in a history
view. Legacy traces from the pre-DeepSeek shadow planner are marked with
`legacy_trace=true`.

ORCH experiment planning calls DeepSeek through `DeepSeekJSONClient`. There is
no local experiment-planner fallback: if the provider is unavailable, the API key
is missing, JSON parsing fails, or the returned `experiment_json` does not pass
the model contract, the job moves to `stage=blocker` and no round is
submitted. Traces carry `planner_mode=deepseek` and
`llm_call_status=call_required/called`.

Default ORCH execution is shadow-safe:

```text
execute_qlib=false
write_registry=false
```

So multi-round status and GUI flows can be tested without running full Qlib
training or writing the shared production registry. Explicitly set
`execute_qlib=true` and `write_registry=true` only for formal runs.

## Status Language

```text
asset_status: candidate / production / archived
job_status: queued / running / completed / failed / cancelled / interrupted
stage: protocol_load / context_review / feature_snapshot / experiment_plan /
       train_backtest_3seed / score_review / forward_test / sota_gate /
       registry_write / round_synthesis / blocker
gate_status: pass / pass_with_warnings / reject
```

## SOTA Score

`score_review` calculates a score for each seed model:

```text
seed_sota_score =
  0.80 * individual_performance_score
+ 0.20 * seed_consistency_score
```

`relative_round_quality_score` is intentionally not used: a seed model is judged
against absolute business thresholds, not by being the best of a weak 3-seed
round.

`individual_performance_score` is a segmented 0-100 score:

```text
35% return_score: excess annualized return <10%=0, 10%-60%=linear, >=60%=100
30% ir_score: excess IR <0.5=0, 0.5-1.5=linear, >=1.5=100
15% drawdown_score: DD <=10%=100, 10%-30%=linear down, >=30%=0
15% rank_signal_score: rank IC 0.02-0.05 and rank ICIR 0.20-0.50
 5% turnover_score: mean daily Qlib portfolio turnover, missing evidence=50 warning
```

`seed_consistency_score` is a 3-seed round background score:

```text
40% return_dispersion_score: return std <=10%=100, 10%-30%=linear down, >=30%=0
35% ir_dispersion_score: IR std <=0.30=100, 0.30-0.90=linear down, >=0.90=0
25% prediction_rank_corr_score: mean daily cross-seed prediction rank correlation, missing evidence=0 warning
```

The default threshold is read from `config.yaml:model.seed_sota_score_threshold`.
Below-threshold seed models are archived without Gate. Above-threshold seed
models enter the single SOTA Gate. Gate pass or pass-with-warnings writes
`candidate`; Gate reject writes `archived`.

## Validation Audit

Validation audit is generated during the normal research flow for seed models
that can still become `candidate`; it is not lazily delegated to the GUI. After
`score_review` and `forward_test`, `sota_gate` validates only above-threshold,
forward-pass/watch seeds before registry admission and persists the result in
both:

```text
runtime/model/runs/<model_run_id>/validation_audit.json
runtime/model/jobs.sqlite seed_runs.validation_json
```

The audit covers artifact completeness, Qlib config/processor/portfolio
contract, single Top20/Drop2 portfolio evidence, cost/concentration, prediction
distribution, ST-like tradability exposure, and style exposures when reference
data is available. Below-threshold, performance-hard-blocked, or forward-rejected
models do not spend validation compute; they keep a lightweight
`validation_skipped` reason in state and registry metadata.

The backtest API is read-only for validation evidence: it reads registry
metadata and existing `validation_audit.json` artifacts, and must not generate
audit files while serving GUI requests. Historical candidate backfills must run
through an explicit maintenance or acceptance command, not through page reads.

## Forward Test

Every seed model whose `sota_score` is at or above
`config.yaml:model.forward_test.score_threshold` enters `forward_test`
before SOTA Gate. The default threshold is `60.0`.

Forward test has two evidence legs:

```text
existing_model_slice:
  use the already-trained model and evaluate 2025H2
  default test window: 2025-07-01 .. 2025-12-31

shifted_retrain:
  retrain the same feature set, seed, fixed sample weight policy, portfolio and model
  parameters after shifting the train/valid/test windows by six months
  default train: 2022-07-01 .. 2025-06-30
  default valid: 2025-07-01 .. 2025-12-31
  default test:  2026-01-02 .. 2026-07-01
```

The stage writes per-seed forward evidence:

```text
pass    -> stable enough for Gate
watch   -> can enter Gate, but Gate is capped to pass_with_warnings
reject  -> archived before Gate
skipped -> below score threshold
```

Shadow runs may record `shadow_forward_evidence=true` for dry-run UI and
contract tests, but shadow evidence cannot be used for production promotion.
Formal production admission must use real Qlib artifacts and full validation.

## Same-Window Feature-Set Replay

Use `scripts/model_replay_feature_sets.py` for controlled 54/56/60
feature-set comparisons. The script starts one fixed feature-set ORCH session
per supplied feature set and keeps the same configured train/valid/test windows.

Default usage stops after DeepSeek experiment planning so the context and schema
can be checked quickly:

```bash
.venv/bin/python scripts/model_replay_feature_sets.py \
  --feature-set-id fs-54 \
  --feature-set-id fs-56 \
  --feature-set-id fs-60 \
  --rounds 1 \
  --max-stage experiment_plan
```

Add `--execute-qlib --max-stage round_synthesis` only for formal replay runs.
Same-window comparisons use one fixed-feature-set session per snapshot.

`round_synthesis` receives the forward-test summary together with the three seed
results, so the next experiment plan can distinguish parameter quality from
out-of-time robustness.

## Registry

`model` shares `data/model/model_registry.db`, but every row includes:

```json
{
  "model_system_version": "model",
  "source_module": "domain.model",
  "round_group_id": "...",
  "seed": 42,
  "sota_score": 63.2,
  "score_review_version": "model_sota_score_v2",
  "gate_version": "model_sota_gate_v2_single_top20"
}
```

Production remains a manual action, but promotion is no longer a status flip on
the candidate row. The selected candidate supplies the feature set, seed and
model parameters; model then creates a new production refit run using the
configured production window:

```text
production_refit.train = 2023-01-03 .. 2025-12-31
production_refit.valid = 2026-01-02 .. 2026-06-30
production_refit.test  = 2026-01-02 .. 2026-07-01
```

Only the new refit artifact can be written as `production`. Multiple production
models are allowed. The original candidate remains a candidate audit record.

An operator-requested exception may bypass only the Rolling candidate-admission
decision. It must name an existing production-mode Rolling campaign, include a
non-empty audit reason, retain complete and reliable Seed42 four-fold evidence,
and still run the fixed-Seed42 production refit plus artifact validation. It
does not rewrite the failed Rolling decision or create a synthetic candidate
row. The exception and final outcome are appended to
`runtime/model/manual_promotion_audit/current.jsonl`:

```bash
python3 cli.py model-promote \
  --model-run-id <rolling_campaign_id> \
  --manual-override-reason "operator requested exception: <reason>"
```

## API

Shadow GET endpoints:

```text
/model/status
/model/feature-sets
/model/backtest
/model/runs
/model/registry
/model/production
/model/forward-tests
/model/research/current
/model/research/journal
/model/orchestrator/status
/model/context/current
/model/orchestrator/events
/model/orchestrator/traces
/model/mcp/traces
```

Shadow POST endpoints:

```text
/model/tools/context
/model/tools/protocol
/model/tools/feature-snapshot
/model/tools/session-start
/model/tools/submit-experiment
/model/tools/run-round
/model/tools/score-review
/model/tools/forward-test
/model/tools/sota-gate
/model/tools/research-step
/model/orchestrator/start
/model/promote
```

## Notes

The 0703 runner records the formal qlib0627 contract in each run manifest. The
current shadow execution path can accept externally supplied metrics for tests
and audit dry-runs; formal qrun execution remains bounded by the same processor,
sample-weight, portfolio, deal-price, limit-threshold, artifact, and validation
contracts before production use.
