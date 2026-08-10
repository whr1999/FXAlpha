# FXAlpha Factor Orchestrator Production Mode

This document defines the default production `orchestrator` factor-mining mode.
`codex_mcp` remains supported as an explicit human-supervised debugging,
failure-isolation, and evidence-review mode. Both modes reuse the same
QuantGPT/FXAlpha tools and quality rules; MCP is not an archived path or a
second factor standard.

## Data Contract

Every new run also pins the platform evaluation contract: `evaluation_mode`,
`evaluation_profile_version`, `evaluation_contract_hash`, `evidence_class`,
the selection/value windows, and the resolved profile snapshot. This is
independent of controller `orchestration_mode`. Switching the platform default
affects only future runs; resume must reuse the original launch snapshot. The
canonical contract is documented in
`./docs/PLATFORM_EVALUATION_MODES.md`.

Orchestrator mode writes the complete control stream to:

```text
runtime/factor_research/orchestrator_events/current.jsonl
```

DeepSeek input/output debugging traces are written separately to:

```text
runtime/factor_research/orchestrator_llm_traces/current.jsonl
```

The trace stream records redacted `llm_request`, `llm_result`, and `llm_error`
records for Orchestrator mode only. It is for research-audit/debugging and is
not projected to `research_steps`.

Prompt continuity is deliberately narrow:

- `upstream_handoff` is the only actionable cross-stage instruction;
- thesis, hypothesis, expression, and round synthesis may additionally read up
  to three deduplicated completed-round facts;
- conflicting positive/negative candidate projections are not replayed to the
  model;
- Factor Map is available to thesis/hypothesis, formal matched-region evidence
  is available to novelty, and only actionable affected-region guidance is
  available to round synthesis;
- code evolution advice is carried through the existing handoff as
  EXPLOIT/EXPLORE/RECOMBINE/SIMPLIFY plus a concrete treatment.

The current stage contract is maintained in this document and
[`README.md`](README.md). Production prompt traces, canary factor expressions,
scores, IDs, and registry fingerprints are deliberately excluded from the
public repository.

Each event contains all `research_step_v2` fields plus private orchestrator
fields such as `event_type`, `checkpoint`, `candidate_lanes`,
`trajectory_metrics`, `advice`, `allowed_actions`, `blocked_actions`,
`llm_result`, and compact context digests.

Orchestrator event projection uses the same run/round/stage naming contract as
Codex MCP:

- `run_id` is the stable research-session id created by `/factor/research/start`
  or passed through a GUI/heartbeat request.
- Normal `round_id` values are `{run_id}:rNNNN`; terminal/recovery control rows
  may use `{run_id}:stop`, `{run_id}:blocker`, or `{run_id}:interrupted`.
- `stage_id` is `{round_id}:sNN_stage`, where `NN` is the zero-padded
  `stage_seq` and `stage` is the snake_case checkpoint name.
- Candidate or tool-progress projections may append suffixes such as
  `:candidate_N_<candidate_id>`, but must preserve the same base round and
  stage id.
- Projected `previous_stage_id` points to the immediately previous visible
  research-step row so Codex MCP can resume from Orchestrator output and
  Orchestrator can resume from Codex MCP output.

For GUI compatibility, each key event is projected into
`runtime/factor_research/research_steps/current.jsonl`. The projection is
lightweight: it keeps only the standard research-step fields, short
`stage_transition` text, `evidence_refs`, and `tags`. It should not embed full
prompts, raw LLM responses, full context packs, or full tool payloads. To keep
GUI recovery useful, `evidence_refs` should include compact traceable refs:
`orchestrator_event`, `llm_trace` when available, `context_pack_digest`, compact
`candidate_lanes` with ids/expressions/key scores/task ids, compact
`advice_summary`, and action guard information. Full candidate lanes, full
advice, LLM payloads, and context packs remain in the event stream, LLM trace
stream, or QuantGPT task DB.

`research_steps/current.jsonl` keeps a bounded but multi-round projection window
instead of only the last few rows. Orchestrator blockers caused by DeepSeek
request/response failures must include the corresponding `llm_trace` ref in the
projected `evidence_refs`, so a GUI row can be traced back to the exact redacted
prompt, payload, elapsed time, and raw response preview without bloating the
research step itself.

Evidence is currently split across two existing stores. QuantGPT task DB is
authoritative for task-backed MCP score, backtest, and validation executions.
ORCH-local novelty, gate, import, and controller decisions are recorded in the
Orchestrator event stream and linked from `research_steps` where refs exist.
Do not infer that a stage did not run merely because no same-day QuantGPT task
row exists. This split is a known traceability gap; remediation should add one
compact evidence manifest that links the existing records, not create a second
research database.

`novelty_check` is the same active-pool novelty and counterfactual
`distress_proxy_exposure` review used by Codex MCP. The code first checks
within-batch novelty and active-pool correlation; those novelty decisions remain
hard gates and rejected candidates must not enter deep validation. It then
computes the all-market long-only top50 diagnostic only for novelty survivors.
Default production mode is advisory: a failed `st_exposure_guard` is persisted as
`risk_tags=["distress_proxy_exposure"]` / `advisory_flag` and must not block deep
validation, quality gate, or import. Orchestrator advice produces
`reject_st_exposure` only when `st_exposure_guard.mode="hard"`; in hard mode the
legacy threshold remains `avg_top50_ratio >= 0.05` or `p95_top50_ratio >= 0.15`
on the actual long-only top50 side. The ST lookup must normalize factor-value
stock indexes such as `sh.600000` and `sz.000004` to the stock identity map
forms `600000.SH` / `000004.SZ` and `600000sh` / `000004sz`; unresolved code
formats are invalid diagnostic evidence, not a clean pass.

## Observability API

The GUI can continue to read `research_steps` and QuantGPT task records. For
Orchestrator debugging, the API also exposes read-only event and LLM trace
views:

```text
GET /factor/research/preflight
POST /factor/research/config-defaults
GET /platform/evaluation-profile
POST /platform/evaluation-profile
GET /factor/research/run-view?run_id=<run_id>&limit=120
GET /factor/research/orchestrator-events?run_id=<run_id>&limit=80
GET /factor/research/orchestrator-traces?run_id=<run_id>&limit=50
```

`/factor/research/preflight` is the standard GUI launch check for Orchestrator
mode. It is read-only: it reports API/QuantGPT health, active Orchestrator run
dedupe state, stale/interrupted hints, runtime defaults, and blocking reasons,
but it must not start research or perform recovery.

`/factor/research/config-defaults` is an explicit operator action used by the
GUI "研究指令台" to save a small allowlist of `factor_research` defaults into
`config.yaml`. It must not change `default_orchestration_mode` or any
selection/value date; `orchestrator` remains the governed controller default
and dates belong to the selected evaluation profile. Starting a run and saving defaults are separate
actions: `POST /factor/research/start` uses the current request payload for
this run, while saved defaults only affect later default values.

`/platform/evaluation-profile` is the explicit platform evaluation-mode API.
Its POST action changes only the default used by future tasks and never edits a
running task. It must not be confused with the ORCH/MCP controller choice.

The start response returns the accepted launch payload in `inputs`; the GUI
compares this receipt with the submitted form before reporting a clean launch.
The same values are persisted in the `orchestrator_launch` event as `inputs`
and `research_contract`. Every LLM stage receives a compact read-only
`operator_research_direction` plus `research_contract`. The direction value and
its mode live only in `operator_research_direction`; `research_contract` owns
the remaining run parameters. A non-`auto` direction is binding research scope,
while `auto` authorizes autonomous topic selection.
Scoring and deep-validation calls must forward `top_frac`, `cost_rate`, and
`rebalance_anchor` along with the existing universe/window/holding/neutralize
contract.

The event and trace endpoints return compact summaries by default. Add
`include_payload=true` only for audit/debug sessions that need the full
Orchestrator event or DeepSeek `system_prompt`/`payload`/`result`. These
endpoints are read-only: they must not start research, write events, write
research steps, or affect an explicit `codex_mcp` debugging session.

The read-only rule also applies to status and library list surfaces. They may
report stale active-values or model feature snapshots, but they must not perform
hidden backfill, repair, import, refresh work, or feature-set freezing. After
import, GUI state must distinguish `registry imported`, `active values stale`,
`active values fresh but model snapshot stale`, and `model feature snapshot
ready`. When model snapshot refresh is required, the canonical status is
`refresh_required` and the trigger owner is `model_side`; Orchestrator/factor
import must not present that as a queued model rebuild. An expression match in
the registry is not proof that the current candidate passed novelty, gate,
import, or model-side snapshot freezing.

For GUI active-state display, live QuantGPT `/api/v1/health` `active_tasks` is
authoritative over stale `running` rows in the QuantGPT task DB. This prevents a
service restart or orphaned background run from leaving the GUI stuck at
`running_mcp_tools` after all actual tasks have finished.

Cross-run research context comes from governed Factor Map v3. Its base regions
are built from the fresh active factor-value audit and pinned at run start.
Before each thesis/hypothesis LLM call, completed current-run research steps are
deduplicated into a small funnel overlay. DeepSeek receives the complete
business-readable region projection only while choosing the economic research
question and testable relation. Expression generation, candidate planning, and
evidence-review stages do not receive it, so the map cannot become an
inaccurate candidate-level novelty precheck. `round_synthesis` sees only
regions with observe/action guidance and produces a bounded short-term handoff;
it does not write a second memory store.

The prompt projection contains region name, core field uses, combination form,
one verified representative, current-run novelty/deep/import counts, and
conservative advisory guidance. It excludes member lists, active-factor count,
correlation matrices, region relations, raw events, and legacy experience text.
Region guidance is never a gate and cannot override current tool evidence or
candidate-level code advice. A region is existing coverage, not a forbidden
zone; `active_factor_count` remains a GUI/audit density field rather than a
model-visible opportunity or saturation signal, and missing guidance means no
map-level action. Thesis/hypothesis responses must describe the real research
increment without claiming that window, constant, sign-wrapper, or
monotonic-wrapper changes are first, new, or orthogonal.
Expression design also performs an explicit transformed-leg direction audit:
the sign of every transformed leg must implement the hypothesis before
backtesting. Automatic score-direction flipping is evidence handling, not a
substitute for a semantically correct expression.

Quick-score code advice follows the same direction contract. A negative signed
RankIC may request one global sign flip only for an A/B code keeper. C/D
candidates are not rescued by sign-only mutation because their score is already
below the deep-validation threshold; they return to mechanism or thesis
exploration instead. A candidate marked `global_sign_flip_only` is never
flipped a second time.

## Runtime Flow

The business checkpoints are:

```text
protocol_load
  -> thesis_design
  -> hypothesis_design
  -> expression_design
  -> candidate_plan(code_precheck)
  -> validate_and_score
  -> score_review
  -> novelty_check
  -> novelty_review
  -> deep_validation
  -> deep_validation_review
  -> quality_gate
  -> import_gate_review
  -> import
  -> import_review
  -> checkpoint_stop
```

The Orchestrator owns flow closure, hard-evidence advice, tool execution,
quality gate, import, and state persistence. DeepSeek owns only bounded JSON
research judgments: thesis design, hypothesis design, expression design,
candidate planning, score review, novelty review, deep validation review,
quality-gate packaging review, import review, blocker review, and round
synthesis. DeepSeek must not decide official import eligibility or write
workflow state directly.

`candidate_plan(code_precheck)` is an internal ORCH pre-score guard, not a
replacement for `fxalpha_novelty_check` and not a new official novelty gate.
Explicit MCP mode accesses the same implementation through the read-only
`fxalpha_code_advice(checkpoint=candidate_plan)` tool; it does not maintain a
second rule set. The Orchestrator computes deterministic checks from normalized
expressions and schema metadata. It hard-drops exact active
expressions, same-batch exact duplicates, unsupported/blocked fields, empty
expressions, and obvious zero-sparse or mutually exclusive constructs before
`validate_and_score`. DeepSeek receives the fresh information-audit map pinned
at run start and may skip only an evidenced batch semantic duplicate or library
near-copy. If similarity is uncertain, or a candidate is a directed mutation of
a promising prior-round parent, it proceeds to `validate_and_score`.
Batch skips must preserve the same fields and core operator set and must cite a
candidate that is itself scored. Changing a confirmation field or normalization
operator fails open to score; a skipped candidate cannot serve as the retained
representative for another skip.
The configured candidate count is a maximum score-compute budget, never a
required batch size. Expression Design receives `candidate_budget.must_fill=false`.
Code precheck marks structurally identical numeric/window-only siblings as
`batch_parameter_only_variant`; Candidate Plan keeps one scored representative
and skips the sibling unless it is an evidenced promising-parent time-scale
experiment.
Parent mutations are explicit rather than inferred: Expression Design emits both
`parent_candidate_id` and `mutation_summary`, and Candidate Plan must score that
candidate unless code precheck found a deterministic fatal error. Final production
novelty remains `fxalpha_novelty_check` over factor values. The precheck result
is pure code but is still part of the next LLM context: candidate-plan prompts
receive `code_precheck`, and the event/research-step projection must expose
compact per-candidate lanes (`precheck_blocked`, `candidate_plan_dropped`,
`planned_for_score`) so GUI monitoring can see why a candidate did or did not
reach `score_factor`. A Candidate Plan drop must cite matched batch candidate ids
or a concrete information-audit cluster and factor ids.

When Orchestrator needs LLM reasoning, it records a redacted LLM trace with the
full system prompt, stage briefing, context pack, and JSON contract, calls
DeepSeek with a layered context pack, then writes `event_type="llm_result"` and
syncs the lightweight decision projection. There is no `codex_task` handoff in
Orchestrator mode.

The LLM context keeps the active library and research history separate and uses
one explicit stage policy. At every fresh ORCH run start, before the first
research stage, the service refreshes and pins one
`factor_library_information_context`. Thesis, hypothesis, and expression design
receive this real factor-value information-family map, its single-copy representatives
and advisory crowding summary together with their
stage-specific research-space and memory context. Candidate planning
receives the current batch, deterministic code precheck, research-space legality,
the same pinned information families, and strict redundancy clusters. Score,
novelty, deep, gate, and import review
receive current-round lineage plus their stage-specific tool evidence and code
advice; they do not receive the unrelated full active research space. Round
synthesis receives the compact round chain, authoritative outcome, short-term
handoff evidence, and advisory long-term memory. Blocker review receives only
error and recovery context.

Stage briefings must describe only fields the payload builder actually supplies.
The research prompt derives its information-family representatives and crowding
summary from the information audit rather than a separate symbolic-family
calculator. Representatives are not copied into parallel aliases. An
information-audit refresh is not a quality gate:
a small active-library count change merely causes the next new run to refresh.
If that refresh has an operational failure, the prior map can be marked
`stale_advisory_only` for research orientation, while library-based Candidate
Plan skips are disabled and formal numeric novelty remains mandatory.
If the active-value pool is partial, `active_pool_coverage_complete=false` and
`excluded_factors` are carried explicitly; those factors are not valid evidence
for a library near-copy skip, so uncertainty continues to score.
Official full-pool novelty remains the job of `fxalpha_novelty_check`.

`OrchestratorContextPack` must preserve the run-pinned
`library_information_context`, one-shot pending operator guidance, and orchestrator contract in
the serialized payload. A protocol event saying the audit is available is not
sufficient: prompt-contract tests also assert that Candidate Plan receives the
same audit id and availability state.

The latest undelivered `human_guidance` record is a one-shot operator message.
It is resolved immediately before the next LLM request, included in that request
only, and consumed when the request-side research step records
`operator_guidance_delivery` with its `guidance_id` and trace id. Later stages
must not inherit it, and `human_guidance` records must be excluded from generic
short-term model history. If several messages arrive before delivery, only the
newest remains pending. GUI receipts may link the actual result with the same
trace id, but must not claim adoption merely because a later event occurred.

Every stage that may create or recommend an expression uses the shared local
parser signature contract. In particular, `ts_av_diff(x, window)` has exactly
two arguments and the volatility operator is `ts_std(x, window)`, not
`ts_stddev`. Expression precheck parses every candidate before score spending;
an invalid expression is a construction/precheck failure with no score or
grade, and must never be counted as a D-grade factor.

Candidate Plan also hard-blocks an expression that is exactly equal after
normalization to any Expression Design candidate from an earlier round of the
same run. The block records the prior round and candidate id. A parent is
protected as a directed mutation only when its current expression materially
differs from the prior expression; a `mutation_summary` cannot protect an
unchanged replay.

Expression Design receives the same-run exact-expression history once under
`tool_evidence.prior_expression_history`, grouped by round and containing only
candidate references plus expressions. Derived field/operator/window lists are
not duplicated. Semantic failures remain in short-term history, and promising
parents remain in `upstream_handoff`; prompt truncation never weakens the full
deterministic exact-repeat precheck.

Score, novelty, and deep review are diagnostic stages. They receive candidate
and tool evidence but not the full operator-signature table, and they return a
semantic mutation objective rather than a complete executable expression.
Expression Design remains the only stage responsible for constructing the
next formula under the full parser contract.

`recent_orchestrator_anchors` and
`recent_high_signal_anchors` are only recent research examples; prompts must not
present them as the full active library or use them as a substitute for
official novelty checks.

`orchestrator` is the default production controller and may call the shared
QuantGPT/FXAlpha service components directly. `codex_mcp` is an explicit manual
debugging/review entrypoint that uses native Codex MCP tools and writes the same
research-step naming and evidence references.

The two controllers are mutually exclusive. Starting `codex_mcp` while a live
Orchestrator exists returns `orchestrator_run_already_active`. MCP startup may
open a fresh research-step live window only after that guard passes and never
clears Orchestrator event or LLM-trace live caches.

## P0 Runtime Guarantees

Orchestrator is hosted inside the API process as a singleton background thread.
It must not create independent worker processes that can scatter across WSL.
`/factor/research/start` first checks the in-memory active Orchestrator job; if
no live job exists, it reconciles the event log. A non-terminal event stream
from before the current API boot, or one past the heartbeat stale threshold, is
marked as an `orchestrator_interrupted` blocker with a handoff instead of being
silently treated as still running.

Backtracking is precise. Review stages may return to
`expression_design`, `hypothesis_design`, or `thesis_design` according to the
LLM's legal `stage_transition.next_stage`. The runner captures that research
return before forcing the immediate pipeline transition to `round_synthesis`;
the synthesis event and the next-round research entry are separate decisions.
`round_synthesis` preserves the captured target in its projection. On the next
round, the runner resumes from the
handoff target when prior thesis/hypothesis state is available; it does not
mechanically restart every failed path from economic-thesis design. If a target
such as `deep_validation_review` cannot be safely resumed without rerunning
tools, the handoff records the original target while the runner resumes from
the nearest safe upstream entry, usually `expression_design`.

DeepSeek retry is layered. Transport, timeout, and provider connection errors
retry the original full messages. Empty content, non-JSON content, and schema
contract failures use the JSON repair/shrink path. Network or HTTP error text
must not be fed back to the model as if it were a malformed JSON answer.

Every stage has a context budget. Fixed protocol/gate/system text stays stable
for DeepSeek cache friendliness, while dynamic history, candidates, and tool
evidence are clipped per stage. `round_synthesis` only receives a compact stage
chain, candidate/evidence refs, LLM decision chain, and handoff; it must not
receive full raw event payloads. Budget statistics are written into the LLM
trace and GUI-facing research-step digest.

GUI monitoring comes from the existing `research_steps` projection, not a new
front-end module. LLM request/result/error and tool-progress projections include
`mode`, `llm_trace_id`, `judgment`, `why`, `history_used`, concise `facts`,
candidate/evidence watches, heartbeat status, and trace/event refs so the GUI
can supervise a background run without Codex watching the foreground.

When an external caller needs to persist a full event, use
`/factor/tools/orchestrator-event`. This endpoint is separate from
`/factor/tools/research-step`: the former writes the full event stream and then
projects a lightweight research step, while the latter keeps the existing
explicit Codex-MCP debugging/review behavior.

## Advice Rules

Advice is not an import gate. It guides the next research action and prevents
wasted compute before the official gate.

## DeepSeek Prompt Contract

Every DeepSeek call follows the same four-part contract:

```text
SYSTEM:
You are an A-share quantitative factor researcher. You make research judgments,
but Orchestrator code controls tools, gate, import, events, and research_step.

STAGE BRIEFING:
Natural-language explanation of the current stage, why the run reached this
stage, what evidence exists, and what decision is needed now.

CONTEXT PACK:
Run state, current lineage, recent research history, current tool evidence,
previous review advice, downstream return handoff, and compact research
experience cards.

OUTPUT CONTRACT:
Strict JSON only. Required fields include decision, judgment, why, next_action,
stage_transition, history_used, and confidence.
```

The LLM stages are intentionally split:

- `thesis_design`: propose economic theses only; do not write expressions.
- `hypothesis_design`: turn theses into testable factor hypotheses.
- `expression_design`: write executable expressions from hypotheses; if no
  valid expression can be produced, return `blocked` and do not fallback.
- `candidate_plan`: decide every expression in the batch. It receives
  `code_precheck` and pinned information-audit evidence; any `fatal=true` lane
  must not be kept. Non-fatal actions are `score`, `revise_expression`,
  `skip_batch_duplicate`, or `skip_library_near_copy`. Routing is per candidate:
  revised or skipped expressions never hold back valid siblings from Quick.
  Missing evidence, uncertainty, and meaningful promising-parent mutations
  default to `score`.
- `score_review`: inspect validate/score evidence and decide novelty advance or
  upstream mutation.
- `novelty_review`: inspect active-pool and batch similarity before deep.
- `deep_validation_review`: the main business-quality review before gate.
- `import_gate_review`: packaging/evidence/metadata review of official gate
  results, not a second deep business review.
- `import_review`: audit automatic import results after official adoption.
- `round_synthesis`: compress the round into reusable next-round memory.
- `blocker_review`: classify runtime/schema/tool blockers without inventing
  expressions.

Review stages receive history too. Score, novelty, deep, and gate review
prompts include recent research steps from both `current.jsonl` and archived
history tails, same-family failures, high-signal anchors, current round events,
research experience cards, and upstream LLM advice. If a
review returns to `thesis_design`, `hypothesis_design`, or `expression_design`,
Orchestrator writes a `return_handoff`; the next upstream prompt must include
that handoff and the LLM must address it first. The handoff carries only the
return level, evidence references, and mechanism-level preserve/change/avoid
constraints. Raw expressions remain in the research trace as evidence, never
as next-round formula instructions.

`research_steps/current.jsonl` projections must include the LLM's concise
`decision`, `stage_transition.next_action`, `stage_transition.judgment`,
`stage_transition.why`, `stage_transition.history_used`,
`stage_transition.llm_trace_id`, and `stage_transition.mode="orchestrator"` so
the GUI research scene shows the model's actual judgment chain without a new UI
module. Candidate-plan projections must also retain compact precheck lanes;
counts-only evidence refs are not enough for candidate-level debugging.

The GUI research cockpit displays `Orchestrator · DeepSeek v4` when the latest
research step has the `orchestrator` tag or `stage_transition.mode` is
`orchestrator`; otherwise it displays `Codex MCP`.

## DeepSeek Context Pack

Every DeepSeek call receives a compact layered context pack:

- `upstream_handoff`: the selected return level, parent evidence references,
  required mechanism changes, and code-selected evolution strategy;
- `current_round_context`: the current thesis, hypotheses, candidate drafts,
  and candidate plan that define this round's research lineage;
- `tool_evidence`: the current stage's authoritative tool output;
- `code_advice`: candidate-level deterministic actions, cross-candidate
  trajectory analysis, evolution strategy, and mutation diagnosis.
- `active_context`: the run contract, complete supported field/operator space,
  field constraints, and Factor Map only in the stages that use it;
- `history_context`: at most three completed round summaries for the design and
  synthesis stages, without conflicting positive/negative candidate-card
  projections.

API keys and raw full prompts must not be written to events or research steps.
The default context pack is intentionally compact for background efficiency:
recent round facts and tool evidence are bounded summaries. Expression design
receives the complete production operator palette/signatures and alpha-eligible
field contract, but no Factor Map. Review stages receive richer hard-evidence
payloads because their job is to explain the actual result and choose the
correct research return level.

Thesis design receives the compact complete Factor Map. Hypothesis design
receives only regions whose core fields intersect the selected thesis.
The model-visible projection omits active-factor counts and carries one verified
representative factor per region; complete counts and region membership remain
available to the audit API and GUI.

Region guidance is intentionally evidence-delayed. An observe-level novelty
warning requires at least two novelty rejections across two rounds. An
action-level near-copy warning additionally requires at least three checks,
three rejections, a rejection rate of at least 75%, and the same semantic
signature at least twice. An observe-level deep warning requires the same deep
failure category at least twice across two rounds. An action-level deep warning
requires at least three checks and three rejections across two rounds, a
rejection rate of at least two thirds, and the same failure category three
times. These rules only choose the wording supplied to thesis, hypothesis, or
round synthesis; they never reject a candidate or alter an official tool
result.
If expression design fails to return strict JSON or any candidate lacks a
non-empty expression, Orchestrator must stop with a blocker. It should not
invent rule-based expressions or silently drop malformed candidates as a
substitute for failed LLM candidate design.

The shared LLM system prompt contains only the stable research role, reading
order, authority boundary, and strict JSON rules. Factor Map usage,
candidate-by-candidate coverage, evidence interpretation, and handoff semantics
belong to the stage briefing that owns the work; they must not be repeated in
every call. `code_advice_alignment` is derived by code after the response for
audit compatibility and is not an LLM output requirement.

For DeepSeek v4-backed calls, the client relies on prompt-level strict JSON and
strict parser validation by default. API JSON mode is disabled unless the
operator explicitly sets `FXALPHA_DEEPSEEK_JSON_MODE=1`, because v4 JSON mode
can return empty content. Structured parsing should use the assistant message
content rather than reasoning text. If JSON parsing still fails, the run blocks
and the trace should retain a raw response preview for debugging; the
Orchestrator must not repair the failure by inventing or filtering candidates.
Model changes are operator decisions and should not be introduced implicitly by
Orchestrator code.

LLM mutation advice must stay executable under the current research contract.
Changing holding period, universe, neutralization flags, gate thresholds, or
import policy is an operator-review idea, not a next-round action, unless the
Orchestrator contract explicitly allows that parameter to change.

### Trajectory

Trajectory metrics follow QuantGPT:

```text
exploration_diversity = min(std(scores) / mean(scores), 1.0)
convergence_rate = clamp(linear_regression_slope(scores) / 10.0, 0, 1)
stability_score = max(0, 1 - std(scores) / best_score)
consecutive_declines = trailing score drops
```

The production Orchestrator restores the chronological, cross-candidate
trajectory for the current run from `research_steps`; it must not pass the
current batch as if it were the complete trajectory.  Score review uses all
persisted quick-score candidates and deep review uses all persisted deep-score
candidates.  No additional trajectory database is maintained.

Deep progress analysis scopes that existing journal before deciding whether a
candidate should keep mutating.  An explicit `parent_candidate_id` lineage is
preferred; otherwise the recorded Factor Map `matched_region_uid` is used.  For
legacy records lacking both links, fields plus operators (with windows ignored)
form an in-memory fallback view only.  This fallback is not persisted as a new
factor-family identity and is not another state store.

Within an explicit parent lineage, two completed deep failures without at least
`+1` deep-score or `+2` rolling-score improvement switch the recommendation from
targeted mutation to recombination.  If a recombination in that lineage also
fails to improve, the recommendation becomes exploration.  Region and legacy
structural fallback views require three attempts before the first plateau
recommendation.  These are advisory research-return decisions only: official
deep-score, quality-gate, and import rules remain unchanged.

The meta-strategy layer exposes the same four research modes as QuantGPT:

- `EXPLOIT`: keep the supported mechanism and apply a targeted diagnosis.
- `EXPLORE`: abandon an early weak direction and generate a materially new
  thesis.
- `RECOMBINE`: after repeated declines or a large gap to the best candidate,
  recombine compatible evidence from prior high scorers.
- `SIMPLIFY`: reduce excessive expression nesting before further evaluation.

The targeted diagnosis layer exposes eight treatments:
`mutate_window`, `mutate_operator`, `mutate_normalization`,
`mutate_signal_type`, `mutate_nonlinear`, `mutate_interaction`, `simplify`,
and `regenerate_full`.  Expression fields are parsed from the actual
expression identifiers, so FXAlpha fields such as `net_mf_amount`,
`borrow_money_bal`, `holder_num`, and `ps_ttm` count as signal legs; the engine
must not use a fixed price/volume-only field list.

Use quick score during quick review, novelty only as ordering/risk evidence,
deep score during deep review, and official gate result during gate review.

### Quick Advice

- Quick-score components are `0..100` normalized sub-scores multiplied by
  component weights. `max_drawdown` has 10% weight, so it can contribute at most
  10 total quick-score points.
- The `max_drawdown` component is inverted: absolute drawdown <= 10% gets
  component score 100, absolute drawdown >= 40% gets component score 0, and
  values between them are linearly interpolated. A 40% drawdown contributes 0
  total quick-score points from this component.
- A/B valid candidates normally advance to novelty.
- C/D candidates are negative evidence and receive mutation advice.
- Strict code keeper is authoritative for score progression: only final
  `score_factor` payloads with `status="success"`,
  `screening_stage="quick_score"`, grade A/B, and explicit deep-validation
  hint/decision may advance. If LLM review omits such a candidate, Orchestrator
  advances it and records `code_advice_keeper` warning evidence; LLM disagreement
  remains audit commentary.
- `score < 20` means explore a new thesis.
- `abs(IC) < 0.005` means mutate the core operator or observable.
- `IC < -0.01` means rethink signal direction; do not only multiply by `-1`.
- Low ICIR without normalization means add `rank`, `zscore`, `scale`, `tanh`,
  or similar stabilization.
- Deep nesting above 8 levels means simplify.
- Repeated declines or a large score gap to the best parent means recombine or
  explore rather than keep micro-tuning.

### Historical Code-Advice Replay

Use the recorded-request replay before loading a code-advice change into a live
runner:

```bash
python scripts/replay_code_advice.py \
  --trace-file runtime/factor_research/orchestrator_llm_traces/current.jsonl \
  --run-id <run_id>
```

The replay follows request timestamps and restores separate score, novelty, and
deep trajectories. It compares recorded and current lane actions, reports
evolution/mutation strategy counts and action transitions, and fails with a
non-zero exit code if a formally valid score, novelty, or deep keeper is
regressed. Deep prompt traces store official component scores in compact form;
the replay reconstructs the four formal score components instead of treating
the compact record as missing evidence.

### Novelty Advice

Use the current novelty score:

```text
novelty_score = 0 if not allowed
else max(0, 1 - max(abs(pearson), abs(rank_corr), abs(p90_pearson), abs(p90_rank_corr)))
```

Novelty-allowed candidates may advance to deep validation. Batch duplicates
should keep the best representative. Active-pool or p90 crowding should switch
source, orthogonalize, or stop the family.

The same-run novelty journal is restored from `research_steps`.  Two or more
prior formal rejections against the same matched region/cluster/factor produce
an advisory `explore_new_thesis` recommendation only when the current candidate
is also formally rejected.  A current formal novelty keeper must still advance
to deep validation; repeated-family history is never a late hard veto.

Novelty progression is also code-authoritative: Orchestrator starts only from
`fxalpha_novelty_check` keepers, requires `novelty_guard.allowed=true`, and
requires `combined_guard.allowed=true` when present. Legacy payload fallback is
limited to novelty allowed with no hard-mode ST veto. Advisory
`distress_proxy_exposure` tags do not block.

### Deep Advice

Deep review decides whether a candidate is worth sending to the official gate.
The current deep score formula is:

```text
deep_score =
  quick_score * 0.55
  + anti_overfit_score * 0.15
  + rolling_score * 0.20
  + adversarial_score * 0.10
```

Novelty remains a required admission guard but contributes no numeric bonus.
Missing novelty, anti-overfit, rolling, or adversarial evidence requires
evidence completion. Rolling v2 is a signed Rank-IC recency diagnostic rather
than a clean OOS backtest; low rolling is interpreted as decay/regime
concentration and is not a separate veto. `deep_score < 80`, IC below `0.02`, or ICIR below `0.3`
should return to candidate design instead of being sent to gate.
Autocorrelation metrics, rolling low scores, and temporal shuffle are diagnostic
inside the combined evidence package and must not become standalone hard
thresholds.

### Gate Advice

Gate advice is quality-control, not primary business judgment. It checks whether
the previous deep review allowed a bad or incomplete candidate through.

- `quality_gate_adopted` advances to import.
- `missing_*` or `requires_deep_validation` returns to deep evidence.
- `holding_period_mismatch` or `data_abnormal` blocks for repair.
- Business rejection at gate should produce `gate_mismatch_feedback` to improve
  deep advice, not repeat a full business review.

## Background Runner

`/factor/research/start` defaults to `orchestration_mode="orchestrator"` and starts a
daemon thread and returns `status="running"`. The thread writes events and
research-step projections as it moves through context, LLM candidate generation,
validate/score, novelty, deep validation, gate, import, and knowledge update.

The runner never falls back to foreground Codex. If DeepSeek, QuantGPT, service
state, or schema validation fails, it records a `blocker` event and stops.

### DeepSeek Runtime

The DeepSeek client reads `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, and
`LLM_CROSS_REVIEW_MODEL` from `storage.paths`. API keys must never be written to
events, research steps, or logs.

The default Orchestrator model is `deepseek-v4-flash`. This keeps routine
research judgment fast and inexpensive. If a live config explicitly sets
`deepseek-v4`, the provider call is mapped to `deepseek-v4-pro`; otherwise
`deepseek-v4-flash` is preserved end to end.

For the `deepseek` provider, the client normalizes the older
`https://api.deepseek.com/v1` base URL to the official OpenAI-compatible
`https://api.deepseek.com` endpoint before trying the configured URL. Calls use
a bounded timeout (`llm_timeout_s`, default 360 seconds, clamped by the client/runtime)
and no SDK auto-retries, so provider/network failures become explicit
`blocker` events instead of leaving the run stuck at `llm_request`.

When running inside WSL without explicit `HTTP_PROXY`/`HTTPS_PROXY`, the client
may use the Windows gateway proxy on port `7890` for DeepSeek calls if that port
is reachable. This is a per-client transport choice only; it does not modify the
system proxy and does not affect an explicit `codex_mcp` debugging session.

## Factor Naming

Before quality gate and import, every candidate must have `factor_name` and
`category_info`.

Use `fxalpha_classify_factor` / `classify_factor_expression` first. Name
generation reuses `generate_factor_name`, which creates compact English names
from mechanism, fields, operators, and windows, for example:

```text
HolderDown60 ROEUp60 CloseVolCorr10
```

`factor_name` is capped at 80 characters. Import still creates the storage-safe
column name with the existing `QGF_<factor_name>_<idx>` logic.
