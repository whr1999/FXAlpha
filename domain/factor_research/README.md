# FXAlpha Factor Mining

This is the canonical production README for FXAlpha factor mining.

## Production Boundary

Production factor mining defaults to the governed FXAlpha Orchestrator:

```text
GUI / API / heartbeat
  -> FXAlpha Orchestrator
  -> bounded DeepSeek research judgments
  -> shared QuantGPT / FXAlpha score, novelty, deep validation, quality gate, import
  -> orchestrator events + research_step_v2 projection + FXAlpha console
```

Operators do not need Codex to control this flow. The GUI, HTTP API, CLI and
systemd user-service procedures are defined in
`./docs/FACTOR_RESEARCH_OPERATIONS.md`. ORCH runs in a detached
worker so restarting the GUI/API does not terminate the research process;
control and recovery continue to use the existing orchestrator event and
research-step evidence streams.

Codex native MCP remains a supported explicit debugging and evidence-review
mode. It is used when an operator needs to call tools step by step, isolate a
failure, replay evidence, or compare an ORCH decision with the raw MCP result.
It is not archived and must use the same evaluator, novelty, deep-validation,
quality-gate, import, date-window, and research-step contracts as ORCH.

There is no Prompt Host, HTTP MCP glue client, one-shot WSL research script, or
legacy runner fallback. In explicit MCP mode, if native QuantGPT MCP tools are
not visible in the active Codex session, stop before candidate generation with
`mcp_native_tools_missing`; do not replace them with shell or HTTP glue.
An explicit MCP debugging run cannot start while a production Orchestrator run
is active. MCP startup clears only its research-step live window and never
clears Orchestrator event or LLM-trace caches.

## Current Defaults

Shared defaults come from `./config.yaml`. Evaluation windows
come from the resolved `platform_evaluation` profile. The runtime mode state
selects only the default profile for new tasks, and every task pins the full
resolved profile. Runtime helpers such as `fxalpha_context` must project that
contract; they are not an independent date source. See
`./docs/PLATFORM_EVALUATION_MODES.md`.

| Name | Runtime source | Meaning |
| --- | --- | --- |
| `evaluation_mode` | `platform_evaluation` resolver + runtime default-mode state | Evidence/date profile (`research` or `production`) pinned into each new task. This is not `orchestration_mode`. |
| `profile_version` / `config_snapshot_hash` | resolved profile snapshot | Immutable lineage used to resume and audit a task. |
| `orchestration_mode` | `config.yaml -> factor_research.default_orchestration_mode` | Default controller. Production uses `orchestrator`; `codex_mcp` is explicitly selected for manual debugging/review. |
| `universe` | `config.yaml -> factor_research.default_universe` | Stock universe used for screening and validation. |
| `selection_start_date` | resolved profile `factor.selection_start_date` | Start of the factor selection/evaluation window. |
| `selection_end_date` | resolved profile `factor.selection_end_date` | End of the factor selection/evaluation window. |
| `value_start_date` | resolved profile `factor.value_start_date` | Start date for factor-value files written after import. |
| `value_end_date` | resolved profile `factor.value_end_date` | End date for factor-value files written after import. |
| `holding_period` | `config.yaml -> factor_research.default_holding_period` | Target holding period and model prediction horizon in trading days. |
| `benchmark` | `config.yaml -> factor_research.default_benchmark` | Benchmark for reports and validation. |

Do not hardcode trading dates in this README, operational prompts, MCP call
examples, or heartbeat handoff text. The single upstream contract is
`./config.yaml`; `fxalpha_context` and `storage.paths` should
only be used as runtime accessors for that config.
If config changes the active trading window, docs and prompts must remain
correct without manual replacement of literal dates.
The same rule applies to example payloads, pseudo-code snippets, and handoff
templates: use window field names or omit the dates entirely.
When native MCP tools already know the runtime defaults, examples should prefer
omitting date arguments rather than repeating literal `YYYY-MM-DD` values in
the payload. Docs, copied MCP payloads, and handoff templates must not maintain
static trading-date constants or a second date policy beside config. If a
workflow genuinely needs explicit dates,
resolve them from the active config-backed window fields at runtime and treat
them as live values rather than template text.
Do not manually type the current trading day, month-end, latest available
factor-value date, or any other concrete `YYYY-MM-DD` trading window into
README examples, prompts, handoff snippets, or pseudo-code. Those values are
runtime state and must be obtained from the active config or runtime accessors
at execution time, not preserved as documentation literals.
README text is allowed to mention window field names such as
`selection_start_date`, `selection_end_date`, `value_start_date`, and
`value_end_date`, but it must not become a second source of truth for the
actual dates.

## Effective Runtime Controls

The governed live contract is deliberately narrower than the historical
`factor_research` config surface:

- Effective live controls: evaluation profile, controller mode, universe, selection/value windows,
  holding period, benchmark, neutralization flags, official shared tool
  outputs, and the mode-specific ORCH/MCP runbook discipline.
- Current production backtests default to `neutralize_cap=true` and
  `neutralize_industry=false`. Market-cap neutralization is applied to both
  portfolio/group construction and IC/RankIC measurement; raw-factor IC is kept
  only as a diagnostic.
- The production universe is expected to be `tradable_non_st`: factor
  cross-sections use the non-ST membership fixed at the 2026-06-01 Tushare
  `st_status/list_status` baseline. Security names are not used for this
  membership decision. `all_market` is retained for diagnostics, legacy
  comparison, and migration audits.
- Service-entry defaults only: `default_n_candidates`, `default_n_rounds`
  (`0` means no fixed round cap), `default_target_adopted` (currently 10), seed counts, direction-attempt
  limits, and stagnation diagnostics. They can appear in GUI handoff metadata,
  but they do not drive a hidden batch runner or stop production mining.
- Historical or inactive native-MCP knobs:
  `quick_score_max_stocks`, `quick_score_max_dates`, and `deep_validate_top_n`
  are not consumed by the current Codex-native `score_factor` /
  deep-validation chain. Current quick-score scope comes from the selection
  window and universe; deep-validation admission is decided by the local
  long-only quick score and the MCP tool chain.
- Heartbeat targets, such as minimum valid imports, belong to the heartbeat
  prompt. They must not be copied into this module README as permanent
  quality-gate logic.
- Selection/value dates are profile-owned and cannot be changed through the
  factor-defaults form. Switching the platform default affects new tasks only;
  a running or resumed task keeps its pinned evaluation snapshot.

The two date windows are intentionally different:

- `selection_start_date` to `selection_end_date` is the research evidence
  window. It is used for quick screening, deep backtest evidence, novelty
  comparison, anti-overfit, adversarial validation, and the quality gate. This
  is the window that answers "is the factor good enough to select/import?"
- `value_start_date` to `value_end_date` is the output feature-value window
  after a factor has already passed the gate. It writes date-by-stock factor
  values for the factor registry and downstream model experiments. It may extend
  beyond the selection window because T-day factor values are observable at T;
  they are features, not future labels. Do not use the extended value window to
  decide whether a factor should be imported.

Import date-window rule: `fxalpha_import_factors` must not reuse the
selection/evaluation `start_date` / `end_date`. In normal Codex MCP runs, omit
import `start_date` and `end_date` so the MCP defaults write the full
`value_start_date` to `value_end_date` parquet. If dates are explicitly passed
to import, they must be the active value-output window from `fxalpha_context`
or the active `config.yaml`. `selection_start_date` and
`selection_end_date` may be passed only as evidence-window metadata. Do not
copy concrete import dates from historical runs, docs, or prior chat turns.

When writing prompts or docs, prefer the window field names
(`selection_start_date`, `selection_end_date`, `value_start_date`,
`value_end_date`) instead of copying concrete `YYYY-MM-DD` values into prose.
The config is the contract; any literal date shown in narrative text will drift.
The same rule applies to copied MCP examples and handoff snippets.

## Factor-Value Missing Semantics

Production factor expressions apply a narrow semantic missing-value policy
before expression evaluation:

- Margin and securities-lending fields with missing values are interpreted as
  zero balance/activity.
- Dividend-yield fields (`dv_ttm` and alias `dividend_yield`) with missing
  values are interpreted as zero reported dividend yield.
- PE/PB/PS-style valuation fields with missing or non-positive values are
  treated as worst-rank raw values for inverse valuation expressions such as
  `rank(-pe)`, `rank(-pb)`, and `rank(-ps_ttm)`.
- Other missing values remain NaN; no missing-indicator features are added by
  default in the factor-expression layer.

Money-flow amount fields (`sm_net_amount`, `lg_net_amount`, `net_mf_amount`)
are in ten-thousand CNY, while daily `amount` is in thousand CNY. For
money-flow-to-turnover ratios, use `net_mf_amount * 10 / amount` rather than
`net_mf_amount / amount`.

## Shared Governed Workflow

ORCH is the default controller and follows
`./domain/factor_research/ORCHESTRATOR_README.md` plus the shared
`fxalpha_context.must_read_contract`. In explicit MCP debugging/review mode,
Codex must read `./third_party/quantgpt/PROMPT.md` before candidate
generation and orchestrate native MCP tools directly. Both modes follow the
same workflow below; the mode changes the controller, not the quality standard.

Required flow:

1. `list_operators`, `fxalpha_context(run_id=run_id)`, and `list_universes` when needed.
2. `fxalpha_record_research_step(stage=protocol_load)` after protocol/context loading.
3. Thesis-first candidate design: `economic_thesis -> hypothesis -> expression`.
4. Complexity, `SIMPLIFY`, and expression precheck judgments, when needed, are
   recorded inside `candidate_plan` or `candidate_decision`; they are not a
   separate workflow stage. Explicit MCP mode obtains the shared result through
   `fxalpha_code_advice`.
5. The shared `candidate_plan` code precheck runs before score spending.
   Explicit MCP mode calls `fxalpha_code_advice(checkpoint=candidate_plan)` to
   run the same implementation used by ORCH. It hard-drops
   exact active expressions, same-batch duplicate expressions, unsupported or
   blocked fields, empty/invalid expressions, and known zero-sparse or mutually
   exclusive expression constructs. Every fresh ORCH run refreshes and pins the
   factor-library information audit; Candidate Plan receives that run-pinned
   result and may skip only an evidenced
   batch semantic duplicate or library near-copy. Uncertain cases and directed
   mutations of promising prior-round parents proceed to `validate_expression` /
   `score_factor`.
   The prompt and precheck share the local parser's exact operator signatures:
   `ts_av_diff(x, window)` is two-argument and `ts_std(x, window)` is the valid
   volatility form. A validation failure has no quick score or grade and is
   shown as expression precheck interception, never as a D-grade result.
   Candidate Plan also blocks an exact expression already generated in an
   earlier round of the same run. Parent lineage protects only a materially
   changed expression, never an unchanged deterministic re-score.
   A traceable parent mutation carries both `parent_candidate_id` and
   `mutation_summary`; this explicit lineage forces score unless code precheck is fatal.
   This is conservative pre-score budget triage only; it does not replace numeric
   factor-value novelty in `fxalpha_novelty_check`.
6. `validate_expression`.
7. `score_factor` for quick screening.
8. Call `fxalpha_code_advice(checkpoint=score_review)` with the complete batch,
   then let the LLM review its evidence. Continue only A/B quick-screen candidates.
   During `score_review`, an A/B
   candidate with negative primary signed RankIC returns once to
   `expression_design` for a global-sign-only normalization (`-1 * (...)`) and
   must be validated and quick-scored again before novelty. No field, operator,
   window, or structure may change in that correction. C/D quick-screen
   candidates are rejected under the current production standard and should be
   recorded as negative evidence rather than deep-validated.
9. `fxalpha_novelty_check` as the combined novelty and distress-proxy review. First compare A/B
   candidates within the same quick-screen batch and keep the higher quick-score
   representative when candidates are highly correlated. Then compare the
   surviving representatives with the same-holding-period active factor pool,
   using the same threshold standard as import consideration. Finally, run the
   counterfactual all-market top50 `distress_proxy_exposure` check only on
   novelty survivors. In default advisory mode this adds `risk_tags` metadata and
   does not stop deep validation; in hard mode it remains a reject reason.
10. `run_backtest`, `diagnose_factor` when useful, `run_anti_overfit`,
   `run_rolling_validation`, and `run_adversarial_validation` as the required
   deep-validation evidence chain for import candidates. Rolling contributes to
   `deep_score`; low rolling scores are not a separate hard veto.
11. `fxalpha_quality_gate` as the single final import gate. Once a candidate has
    complete deep evidence, submit it to the gate unless required evidence is
    missing or malformed; do not substitute an agent-side veto based on one
    weak subtest.
12. `fxalpha_import_factors` only for quality-gate adopted candidates. Do not
   pass selection-window `start_date` / `end_date` into import; omit them or use
   the value-output window.
   Import metadata must preserve the backtest-selected long-only side through
   `selected_group_is_flipped_low_side` and `long_only_direction`, so downstream
   model audits can distinguish high-factor-side and low-factor-side winners.
13. `fxalpha_record_research_step` at each major boundary using
    `research_step_v2`, so the GUI can show the process-log chain and the next
    Codex step.

Do not call score, novelty, validation, gate, or import through shell scripts,
curl, temporary Python clients, or streamable-HTTP glue. ORCH calls the shared
service/tool implementations; MCP debugging calls the native tools explicitly.

Candidate budgeting is a triage policy, not a fixed quota. A normal batch should
contain three to five meaningfully different candidates. Every A/B candidate
that is not an obvious duplicate should enter novelty, and every novelty-allowed
A/B candidate can enter deep validation, provided it has either a distinct
mechanism or a useful control role. Reduce compute by dropping weak, duplicate,
novelty-rejected, or unjustified complex candidates, not by enforcing an
arbitrary deep-validation count.

The pre-score expression precheck is allowed to reduce score calls only for
obvious waste or invalid work: exact active expression reuse, same-batch exact
duplicates, same-batch expressions whose structure is identical and only
numeric parameters/windows differ, unsupported/blocked fields, empty
expressions, and known invalid constructs. A parameter-only variant is skipped
in favor of one scored batch representative unless it is an explicit promising
parent time-scale experiment. Active-family crowding from compact family
representatives remains advisory. Formal novelty remains the later numeric
`fxalpha_novelty_check`.

`n_candidates` is the maximum quick-score compute budget, not an output target.
Expression Design may return any count from one through that maximum and must
not add parameter, window, or wrapper variants merely to fill unused budget.

## Research Discipline

Every batch is thesis-first:

```text
economic_thesis: why the market behavior may create return
hypothesis: how available fields observe that behavior
expression: executable translation of the hypothesis
```

Complex expressions are overfitting-risk signals. They should trigger four-step
review and `SIMPLIFY` reasoning: explain the smallest tradable hypothesis,
essential components, tuning/baggage components, and the economic or market
behavior behind each retained part. Complexity is not a separate hard-coded
quality-gate cutoff.

Complexity review is a budget judgment, not a separate fixed workflow stage. A
candidate with multiple complete alpha legs, nested `where` fallback legs,
repeated window tuning, or more than two independent confirmation fields should
be simplified or explicitly classified as `KEEP_WITH_JUSTIFICATION` inside
`candidate_plan` or `candidate_decision` before spending more evaluation
budget. If a simpler parent tests the same thesis, score the parent first and
advance the heavier child only when it provides a meaningful incremental score
or novelty improvement.

## Quality Gate

`fxalpha_quality_gate` is the only import decision gate. Import persists adopted
candidates; it must not invent a second quality standard.

Current production import standard:

```text
quality_gate adopted
AND deep_score >= 80
AND |IC| >= 0.02
AND |ICIR| >= 0.3
AND novelty_guard.allowed == true
AND quick_score is present
AND adversarial validation passes under the official gate rule
AND holding_period_days is present and valid
AND required deep-validation evidence exists
```

`qgpt_grade` is audit metadata only. It is not a separate import veto.

Do not add extra hard ICIR or RankICIR cutoffs outside the official score/gate
logic.
Do not add a separate temporal-shuffle hard veto outside the gate either.
Temporal shuffle is important diagnostic evidence, but the implemented import
gate evaluates the overall adversarial result and the combined `deep_score`.
Factor-value and IC autocorrelation metrics are diagnostic evidence only. They
can guide the next research round, but they are not a standalone hard veto in
the import gate.

Import closure has separate factor-side and model-side states.
`fxalpha_import_factors` first writes the strict registry row and single-factor
parquet. Factor-side completion then means the active-values wide store has
been rebuilt from the adopted factor parquet and its manifest fingerprint
matches the current active registry.

The factor-mining flow must not freeze or overwrite a model feature set. It may
mark the model snapshot as stale with `model_feature_snapshot_status` /
`model_snapshot_refresh_required`, but the trigger owner is `model_side`.
Freezing a feature set belongs to `fxalpha_model_feature_snapshot` or
`fxalpha_model_session_start(rebuild_feature_set=true)`.

Compatibility fields such as `model_feature_refresh_status` and
`model_refresh_required` may still appear for old clients, but they mean
`refresh_required`, not "a model rebuild was queued." GUI and status must
distinguish `registry imported`, `active values stale`, `active values fresh
but model snapshot stale`, and `model feature snapshot ready`.
Read/list/status/context endpoints are read-only and must not run hidden
backfill, repair, import, active-values refresh, or model-snapshot freezing.

Quick score is the single A-share long-only screening score. It uses:

- `IC Mean` 20%
- `IC IR` 20%
- `Rank IC Mean` 10%
- `Rank IC IR` 10%
- `annual_return` 15%
- `sharpe` 10%
- `max_drawdown` 10%
- `turnover` 5%

Each component is first normalized to a `0..100` component score, then
multiplied by its weight in the final `0..100` quick score. For example,
`max_drawdown` contributes at most `10` total quick-score points because its
weight is 10%. The drawdown component is inverted: absolute drawdown <= 10%
gets component score 100, absolute drawdown >= 40% gets component score 0, and
values between them are linearly interpolated. A 40% drawdown must therefore
contribute 0 total points from the drawdown component.

`score_factor` is the only quick-score entrypoint. Only `A/B` candidates enter
deep validation. `C/D` stop at quick reject. Long-short and WQ fields remain
diagnostic-only and must not drive local screening, deep score, or import.

Deep score reuses the already computed `quick_score`; it does not recompute a
second quick core. The production formula is:

```text
deep_score = quick_score * 0.55
           + anti_overfit.score * 0.15
           + rolling_validation.score * 0.20
           + adversarial_validation.score * 0.10
```

All deep sub-scores must come from the original tool outputs. Missing
`quick_score`, `anti_overfit.score`, `rolling_validation.score`,
or `adversarial_validation.score` is treated as incomplete numeric evidence.
Novelty guard evidence remains mandatory and can veto import, but novelty is
not a numeric deep-score component.

Stable scoring facts must come from the original MCP task outputs persisted in
the QuantGPT task store / DB. `research_steps` remain the GUI-visible
process-log chain, but they are not the canonical source of `quick_score`,
`deep_score`, or `gate_result`.

Rolling v2 uses signed daily Rank IC under the formal calendar-date T+5 close
return contract. It scores non-overlapping 0-6/6-12/12-24/24-36/36-48 month
periods with weights 40/25/15/12/8, then applies
`robust_ic = weighted_ic - 0.25 * weighted_std` and
`rolling_score = clip(robust_ic / 0.08 * 100, 0, 100)`. Trailing
6/12/24/36/48 month IC/ICIR are explanation views, not a second score. It
requires at least 24 months; available whole periods are reweighted when fewer
than 48 months exist. Rolling never takes absolute IC or changes factor
direction. It is required evidence for new gate/import candidates, but a low
score is not a separate hard veto beyond its 20% contribution to deep score.

## Novelty

Novelty is numeric factor-value correlation, not LLM semantic similarity and not
factor persistence/autocorrelation. The MCP entrypoint combines batch
de-duplication, active-library increment, and a counterfactual all-market
`distress_proxy_exposure` review on novelty survivors only.

`fxalpha_novelty_check` first compares same-batch A/B candidates by factor-value
correlation and keeps the higher quick-score representative from highly
correlated siblings. It then compares the survivors with the active factor pool
for the same `holding_period_days` using cross-sectional Pearson and rank
correlations. These novelty checks remain hard pre-deep-validation gates:
`batch_redundancy`, `active_pool_low_information_gain`, or
`novelty_correlation_veto` stops that candidate in the current round. A crowded
expression usually means the translation is crowded; it does not automatically
kill the broader economic thesis.

After novelty passes, the same tool computes the long-only top50
`distress_proxy_exposure` diagnostic on counterfactual all-market rows. Use the
actual long-only side: flipped low-factor-side candidates rank low values as the
top portfolio, otherwise high values. The historical risk thresholds remain
`avg_top50_ratio >= 0.05` or `p95_top50_ratio >= 0.15`.

Default production mode is advisory: the diagnostic is stored as
`st_exposure_guard.mode="advisory"`, `scope="counterfactual_all_market"`,
`label="distress_proxy_exposure"`, and `risk_tags=["distress_proxy_exposure"]`
when thresholds are hit. Advisory hits do not block deep validation, quality
gate, orchestrator progression, or import. Hard veto behavior is restored only
when `factor_research.st_exposure_guard_mode` or
`FXALPHA_ST_EXPOSURE_GUARD_MODE` is set to `hard`; in that mode downstream code
must use `combined_guard.allowed`, not only `novelty_guard.allowed`, to decide
whether the candidate may enter deep validation.

The diagnostic is based on actual factor values and current stock names, not on
expression text. It must normalize factor-value stock indexes before name
lookup: qlib-style `sh.600000` / `sz.000004`, market-code keys such as
`600000.SH` / `000004.SZ`, and instrument keys such as `600000sh` /
`000004sz` are equivalent. If a candidate's counterfactual ST count is zero only
because the factor-value index could not be resolved to the stock identity map,
that is invalid evidence and must not be treated as a clean diagnostic pass.

Novelty review records must distinguish `batch_redundancy`,
`active_pool_low_information_gain`, advisory `distress_proxy_exposure`, and hard
`reject_st_exposure`. The first is a representative-selection decision inside
the current batch; the second is evidence that the candidate does not add enough
information beyond the live factor library; the third is a risk tag by default;
the fourth exists only when the ST exposure guard mode is hard.

## Research Steps, Stages, And Rounds

`fxalpha_record_research_step` / the ORCH research-step projection is the
research process log. ORCH writes it automatically; explicit MCP debugging
writes it through `fxalpha_record_research_step`. The chain is:

```text
previous_stage -> stage -> stage_transition.next_stage
```

Codex MCP and Orchestrator projections share one naming contract:

- `run_id` is the stable GUI/heartbeat/manual run identifier for the whole
  research session.
- Normal `round_id` values are `{run_id}:rNNNN`, starting at `r0001`; terminal
  or recovery rows may use `{run_id}:stop`, `{run_id}:blocker`, or
  `{run_id}:interrupted`.
- `stage_seq` is the integer order inside the round, and `stage_id` is
  `{round_id}:sNN_stage`, with zero-padded `NN` and snake_case stage names.
- Candidate or tool progress rows may append suffixes such as
  `:candidate_N_<candidate_id>`, but they must keep the same base
  `{round_id}:sNN_stage`.
- `previous_stage_id` always points to the immediately previous visible
  research-step row, so Codex MCP and Orchestrator can hand off without a
  separate log dialect.

New records use this shape:

```json
{
  "schema_version": "research_step_v2",
  "run_id": "fxalpha-factor-mining-follow-up",
  "round_id": "run_current_session:r0003",
  "stage_seq": 4,
  "stage_id": "run_current_session:r0003:s04_score_review",
  "previous_stage": "candidate_plan",
  "previous_stage_id": "run_current_session:r0003:s03_candidate_plan",
  "stage": "score_review",
  "summary": "short factual summary of the completed stage",
  "decision": "short summary of the next move",
  "stage_transition": {
    "next_stage": "novelty_review",
    "next_action": "detailed next step",
    "research_strategy": "natural-language process/evolution strategy",
    "facts": "what the tools showed",
    "judgment": "research judgment after four-step reasoning",
    "why": "why this next step is correct",
    "history_used": "recent steps, knowledge, or failed paths used"
  },
  "evidence_refs": [
    {"tool": "score_factor", "task_id": "qgpt_task_xxx", "note": "raw metrics live in QuantGPT task store"}
  ],
  "tags": ["quick_screen", "thesis_first"],
  "priority": "normal"
}
```

Field intent:

- `summary`: summarize what the just-completed stage found, in factual language.
- `decision`: summarize the next move in one short line for GUI display.
- `stage_transition.next_action`: carry the formal detailed next action.

`decision` and `stage_transition.next_action` should point to the same next
move, but at different levels of detail; `decision` should not repeat the
completed-stage summary.

`next` is not part of the formal new schema; if a legacy caller passes it, it
is only a fallback for `stage_transition.next_action`. Raw
score/backtest/gate/novelty metrics should stay in the QuantGPT task store and
be cited by `evidence_refs`, not copied into `extra`.

A `round` is one thesis-derived candidate batch attempt. Returning to thesis,
hypothesis, or expression design starts a new round. A `stage` is a required
decision point inside a round, such as `protocol_load`, `pre_batch_decision`,
`score_review`, `novelty_review`, `deep_validation_review`,
`import_gate_review`, `import_review`, `round_synthesis`, `checkpoint_stop`,
or `blocker`.

Normal process stages should say they are normal process flow, such as "run
novelty next" or "prepare import after gate adoption". Evolution language is
only needed when the loop returns to thesis, hypothesis, or expression design,
for example simplifying a complex expression, switching observable fields, or
changing the market-behavior thesis.

Quality gate decisions remain deterministic. Four-step reasoning controls the
next research direction, stage transition, and knowledge writing.

Research steps are stored in:

```text
./runtime/factor_research/research_steps/current.jsonl
./runtime/factor_research/research_steps/history/YYYY-MM-DD.jsonl
```

`current.jsonl` is a bounded live cache (the most recent 5,000 semantic
research-step records, with a 16 MiB guard). GUI/status views default to the
latest live window. `history/YYYY-MM-DD.jsonl` is append-only and preserves the
full semantic process chain for replay and audit.

ORCH has two separate journals under the same runtime root:

- `orchestrator_events/current.jsonl`: bounded to 6,000 rows / 24 MiB; the
  daily history retains every controller event, including tool heartbeats and
  candidate progress.
- `orchestrator_llm_traces/current.jsonl`: bounded to 2,000 rows / 32 MiB; the
  daily history retains complete redacted request/response traces for debugging
  and audit.

Progress records have two deliberately different levels. Complete controller
heartbeats and raw tool results stay in the event journal and the QuantGPT task
store. A concise `llm_request_progress`, `tool_progress`, or
`candidate_progress` research step is also allowed when it carries information
needed by the GUI, human recovery, or the next research handoff. These concise
steps do not replace the underlying tool evidence and must not copy full raw
results. `_history_step_is_substantive()` excludes progress-only steps from the
LLM short-term history, so observability does not crowd out research context.
Repeated no-change heartbeats remain event-only.

## Factor Map Context

The legacy experience-card library is archived and has no runtime writer, audit
worker, API, MCP tool, or prompt projection. Historical files remain read-only
solely for migration provenance. The completed migration JSON is a frozen audit
receipt: the live Factor Map service reports its status but does not reopen,
rehash, parse, or project archived experience records.

Cross-run context is supplied by the governed Factor Map v3. The base map is
the fresh active factor-value audit and is pinned for the life of a run. Each
LLM design-stage boundary adds a read-only, deduplicated overlay from that
run's completed novelty, deep-validation, gate, and import trajectories.

The model-visible region projection is deliberately small:

- business-readable region name;
- core fields and their dominant use;
- dominant combination form;
- one verified representative factor with expression and admission score;
- simple current-run funnel counts;
- advisory guidance only after repeated evidence across rounds.

Raw member expressions, cross-region correlations, relation graphs, legacy
annotations, and raw research events are not sent in `factor_map_context`.
DeepSeek receives the complete region list only in `thesis_design`, where it
selects an economic research question. `hypothesis_design` receives only
regions whose core fields intersect the selected thesis, so the map can help
compare nearby information relations without reopening topic selection.
`expression_design`, `candidate_plan`, score, novelty, deep validation, gate,
and import use the current hypothesis, candidate history, code advice, and
tool evidence instead. This prevents the map from becoming an inaccurate
candidate-level novelty precheck.
`round_synthesis` receives only regions with observe/action guidance and writes
the next-round short-term handoff.

Region guidance never changes score, novelty, deep validation, gate, import, or
state-machine decisions. The authority order is current tool evidence, current
candidate code advice, upstream handoff, region guidance, then region
description. A shared field or occupied region is not itself a duplicate.
`active_factor_count` remains available to the audit API and GUI, but is not
sent to DeepSeek because count alone is neither opportunity nor saturation. Repeated rejection
must also share the same semantic structure before an action-level novelty
warning is emitted. Thesis and hypothesis may continue inside an existing
region when they add a real information relation, confirmation condition, or
risk mechanism, but window, constant, sign wrapper, or monotonic wrapper
changes must not be described as new or orthogonal. Formal novelty remains the
owner of candidate-level similarity decisions.

## GUI Data Flow

The 18081 GUI reads the existing service projections:

- `/factor/research/run-view?run_id=...` for the active run's read-only joined
  projection (process steps, ORCH events/traces, and QuantGPT task evidence)
- `runtime/factor_research/research_steps/current.jsonl` and daily history for
  the semantic process chain
- QuantGPT task records for task-backed MCP score/backtest/validation evidence
- Orchestrator event records for ORCH-local novelty/gate/import/controller evidence
- Orchestrator event `evidence_refs` such as `candidate_plan_code_precheck`,
  `candidate_plan_llm_budget_triage`, and `code_advice_keeper` summaries
- Orchestrator projected candidate lanes for expression precheck state:
  `precheck_blocked`, `semantic_revision`, `candidate_plan_dropped`, and
  `planned_for_score`.
  Candidate Plan drops preserve matched candidate or information-cluster evidence.
- factor registry metadata and research note markdown

The GUI displays research state; it does not run the research brain.
`runtime/factor_research/jobs/*.json` is deprecated. If old heartbeat prompts
call `scripts/factor_automation_gui_log.py`, that compatibility script now
translates the event into `research_steps/current.jsonl` instead of writing a
separate job stream.

## Code Map

| Area | Path |
| --- | --- |
| Default production controller | `./services/factor_research_service.py` + `./domain/factor_research/ORCHESTRATOR_README.md` |
| MCP debugging/review prompt | `./third_party/quantgpt/PROMPT.md` |
| MCP server | `./third_party/quantgpt/quantgpt/mcp_server.py` |
| Platform service | `./services/factor_research_service.py` |
| Numeric novelty | `./domain/factor_research/dedup.py` |
| Quality gate | `./domain/factor_research/quality_gate.py` |
| Import persistence | `./domain/factor_research/auto_import.py` |
| Factor registry | `./storage/factor_registry.py` |

## Path Boundaries

| Boundary | Canonical path | Rule |
| --- | --- | --- |
| Production domain code | `./domain/factor_research/` | Business contracts, prompt-facing rules, local checks, quality gate, and import logic only. |
| Production service/API | `./services/factor_research_service.py` and `./api_server.py` | Stable GUI/API entrypoints; no parallel factor backend switch. |
| QuantGPT engine and MCP | `./third_party/quantgpt/` | Shared score, backtest, validation, task DB, and native MCP debugging path. |
| Production factor library | `./data/factors/` | Registry and imported factor values only. |
| Live research state | `./runtime/factor_research/` | Current journals, traces, and resumable run records only. |
| Diagnostics | `./runtime/diagnostics/factor_research/` | Operational reports that must not drive production status. |
| Runtime archive | `./runtime/archive/factor_research/` | Historical evidence and maintenance receipts; never a live input. |
| Document archive | `./docs/archive/factor_research_*/` | Historical design/audit material; never a current runbook. |

The live runtime root is reserved for resumable production state and governed
operational backups:

```text
./runtime/factor_research/research_steps/current.jsonl
./runtime/factor_research/orchestrator_events/current.jsonl
./runtime/factor_research/orchestrator_llm_traces/current.jsonl
./runtime/factor_research/codex_mcp_runs/
./runtime/factor_research/non_st_migration/
./runtime/factor_research/repair_backups/
./runtime/factor_research/registry_backups/
./runtime/factor_research/experience_digest_state.json
```

Completed diagnostics and one-off reports do not belong in this root. Write
new diagnostics under `runtime/diagnostics/factor_research/`; move completed
historical material to `runtime/archive/factor_research/` through governed
maintenance rather than introducing another top-level runtime directory.

The live GUI should derive runtime status from the existing read-only run view,
which keeps its source roles explicit. For conflicting status surfaces, use this order:
research progress comes from `research_steps/current.jsonl`, production factor
state comes from `factor_registry.db`, ORCH control evidence comes from its
event stream, and raw MCP execution evidence comes from the QuantGPT task DB.
GUI views and `run-view` are read-only projections, not persistent state stores.

Legacy research steps are preserved as audit history. Do not rewrite
`current.jsonl` to retrofit old rows; the explicit journal-compaction maintenance
action is the sole exception and first backfills missing rows into daily history.
New `research_step_v2` records will appear
after the next production call to `fxalpha_record_research_step`; the GUI should
label old rows as `legacy schema` and new rows as `research_step_v2`.
Historical active factors remain compatible audit/production records. Do not
retrofit, re-score, retire, or reject them solely because the current admission
standard changes later.

Acceptance checklist for new production runs:

- The first new heartbeat/manual record is `schema_version=research_step_v2`.
- No new research step writes `extra.four_step`, `extra.trajectory_snapshot`,
  `extra.resume_cursor`, `extra.metrics`, `extra.quality_gate`, or
  `extra.novelty_guard`.
- `stage_transition.next_action` is the formal next action; legacy `next` is
  only a compatibility input.
- `evidence_refs` points to task-store evidence instead of copying full
  score/backtest/gate JSON into the research step.
- Pre-score precheck appears as compact evidence refs and Chinese summaries in
  GUI, and its per-candidate state appears as compact candidate lanes; raw
  code-precheck and Candidate Plan evidence remain in Orchestrator events / traceable JSON,
  not in long `summary` text.

## Forbidden Production Paths

- `mcp_agent_runner.py`
- Prompt Host fallback
- Python `streamable_http` MCP substitute
- `curl http://127.0.0.1:8003/mcp`
- `/tmp/fxalpha_heartbeat_mine.py`
- one WSL command that performs score, novelty, deep validation, gate, and import
- `domain/factor_research/legacy/*` as a production runner

These restrictions do not remove MCP debugging. The supported MCP debugging
path is the native `quantgpt` MCP server declared by the project, used through a
human-supervised Codex session and the shared research-step/evidence contract.

## Engineering history

Production engineering logs are retained as private operational evidence
because they can contain candidate expressions, factor IDs, scores, registry
fingerprints, runtime paths, and incident state. They are not part of the
public contract. Use this README, the ORCHESTRATOR README, and the MCP prompt
for current rules.
