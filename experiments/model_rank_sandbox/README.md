# Isolated model-rank research sandbox

This directory is deliberately outside `domain/model` and is not imported by
the API, MCP server, model registry, production refit, prediction, or trading
paths.

The runner reads an existing immutable feature-set parquet and writes only to
`runtime/research_sandbox/model_rank`.  It does not call the model
orchestrator, does not update the active feature pointer, and does not write
research, candidate, or production registry rows.

The screen is signal-level research evidence.  It compares Ridge, LightGBM
MSE, Huber, and cross-sectional ranking objectives on three purged inner
validation windows.  It is not a formal portfolio Rolling result and cannot
be used for promotion.

Smoke test:

```bash
systemd-run --user --scope \
  -p MemoryMax=6G -p MemorySwapMax=4G \
  .venv/bin/python \
  experiments/model_rank_sandbox/run_isolated_rank_screen.py --mode smoke
```

Full 12-candidate screen:

```bash
systemd-run --user --scope \
  -p MemoryMax=6G -p MemorySwapMax=4G \
  .venv/bin/python \
  experiments/model_rank_sandbox/run_isolated_rank_screen.py --mode screen
```

Tie-policy portfolio comparison for an existing prediction artifact:

```bash
.venv/bin/python \
  experiments/model_rank_sandbox/run_tie_policy_backtest.py \
  --model-run-id MODEL_RUN_ID \
  --run-id tie-policy-YYYYMMDD
```

The tie-policy runner compares the current instrument-code Top20 baseline,
exclusion of the whole Top20 boundary tie, inclusion of the whole boundary tie,
and point-in-time ROE ranking within model-score ties.  All variants use the
same daily equal-weight Qlib execution contract and write only below
`runtime/research_sandbox/model_tie_policy`.

For an already audited walk-forward prediction artifact, replace
`--model-run-id MODEL_RUN_ID` with `--prediction-path /absolute/path/to/stitched_pred.pkl`.
This keeps the longer comparison out-of-sample instead of applying one fitted
model backwards across its own training window.
