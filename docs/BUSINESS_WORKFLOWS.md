# FXAlpha Business Workflows and Calculation Logic

**English** | [简体中文](BUSINESS_WORKFLOWS.zh-CN.md)

This is the business contract for the current FXAlpha implementation, not a
page tour. It states what every module consumes, how many stages it runs, how
it calculates decisions, which gates allow progress, and which governed assets
are committed. New users should read this document before opening a lane's
operator runbook.

> Scope: research and paper trading. The public repository contains no market
> data, factor values, feature snapshots, trained models, account state, or
> credentials. Dates and scores below explain algorithms and are not production
> results.

## 1. One chain and five classes of governed assets

```text
Tushare source data
  -> staging package -> production dataset
  -> factor candidate -> active factor registry and factor values
  -> registry-fingerprinted feature snapshot
  -> research/candidate/production model
  -> daily prediction score -> target portfolio -> pending recommendation
  -> Qlib paper fills -> positions, cash, NAV, and audit snapshots
```

The business checkpoint is not “a program finished.” It is whether the
governed asset passed its gate and was committed:

| Upstream state | Does not mean | Actual downstream-ready condition |
| --- | --- | --- |
| staging completed | production data is usable | promotion succeeded, `post_promote_audit.status=passed`, and `production_health.status=ready` |
| factor run completed | factor was imported | quality gate adopted, value coverage passed, and an active Registry row exists |
| training completed | production model is usable | formal Rolling candidate passed, production refit and validation passed, and active production pointer changed |
| prediction generated | daily trade executed | recommendation remains pending until its execution date |
| one account completed | fleet completed | every active account and the fleet post-run audit completed |

GUI, CLI, and MCP are access surfaces over the same `services/` and `domain/`
logic. The frontend neither reimplements scoring formulas nor overrides backend
rejections.

## 2. Data foundation: vendor data to consumer-ready production

### 2.1 The production daily routine has 11 business steps

| Step | Input | Calculation | Output / continuation condition |
| --- | --- | --- | --- |
| 1. Read production pointer | current dataset manifest | Resolve production package, HDF, Qlib, QuantGPT, and latest date | Pointer and asset paths agree |
| 2. Daily preflight | production date, target, Tushare calendar | Select a closed target session and derive `replace_from_date` | Network, credentials, memory, disk, lock, and date gates pass |
| 3. Short-window source rebuild | preflight plan and Tushare APIs | Fetch daily, daily basic, adjustment, slow fields, indices, and trading constraints | Independent source staging package; production is untouched |
| 4. Source compatibility output | source silver table | Normalize fields, price semantics, indices, and metadata | source compat HDF, calendar, and quality references |
| 5. Full-history merge | production HDF plus source delta | Remove the old tail from `replace_from_date`, append replacement, validate a temporary output | staged full-history HDF |
| 6. Staged calendar | staged HDF | Deduplicate and sort valid trading dates | staged `trade_calendar.txt` |
| 7. Downstream views | staged HDF and production seed | Patch Qlib by window and export QuantGPT stock/benchmark views | staged Qlib and QuantGPT assets |
| 8. Staged quality gates | staged assets and source reports | `daily_compat` checks the refresh window; `deep_full` checks the assembled table | Both reports are complete and passed |
| 9. Atomic promotion | completed staged package | Journal, back up replacements, replace assets, compare staged and production, commit pointer last | `promoted`; journal supports recovery |
| 10. Post-promote audit | new production assets | Audit pointer, date, file mode, consumers, and sampled values | `post_promote_audit.status=passed` |
| 11. Cleanup preview | staging, backups, caches | Calculate reclaimable and protected items | dry-run report only; no automatic deletion |

The GUI compresses progress into six stages: `source_rebuild`,
`source_prepare_production`, `merge_production_hdf`, `merged_quality_check`,
`build_compat_outputs`, and `completed`. This projection does not remove any
business acceptance step above.

### 2.2 Important data calculations

- Research-adjusted OHLC is derived locally as raw OHLC times `adj_factor`;
  there is no per-symbol `pro_bar` download stage.
- Official `pre_close` comes from Tushare `stk_limit.pre_close`; previous close
  is only a fallback when that field is unavailable.
- Financial, holder, margin, chip-cost, and money-flow fields use point-in-time
  availability alignment. A later disclosure cannot be backfilled into an
  earlier date.
- Missing data remains `NaN`, `pd.NA`, or `NaT`; zero does not silently stand
  for completeness.
- Qlib is generated from raw prices plus explicit factors. QuantGPT consumes
  adjusted research prices.

The gate checks row/date/symbol coverage, schema, missing ratios, zero prices,
benchmarks, price limits, adjustment consistency, listing/ST metadata, and
consumer date alignment. [`quality_check.py`](../domain/data_foundation/quality_check.py)
and the [data workflow](DATA_FOUNDATION_WORKFLOW_CURRENT.md) are the detailed
authorities.

## 3. Factor mining: economic thesis to active factor

### 3.1 The successful path has 11 stages

This count includes one startup stage and ten business stages per round. A
candidate may return to expression, hypothesis, or thesis design. A system
failure enters a blocker and is not misclassified as poor factor quality.

| # | Stage | Input | Calculation and decision | Normal output |
| --- | --- | --- | --- | --- |
| 0 | `protocol_load` | runtime config, tools, budgets, checkpoint history | Freeze protocol, fields/operators, dates, holding period, and recovery point | enter design; no candidate score |
| 1 | `thesis_design` | objective, field families, Factor Map, recent facts | Propose 1–3 economic mechanisms and distinguish actual relation overlap | economic theses |
| 2 | `hypothesis_design` | one thesis, field/operator constraints, handoff | Define falsifiable main signal, confirmation, risk control, and direction | 1–4 hypotheses |
| 3 | `expression_design` | hypotheses, signatures, budget, no-repeat list | Implement a few expressions and verify signs, branches, windows, and complexity | 1–5 drafts; targeted repair uses 1–2 |
| 4 | `candidate_plan` | drafts, static precheck, semantic descriptions | Check fields, syntax, direction, exact repeats, and batch parameter duplicates | legal candidates enter Quick Score |
| 5 | `score_review` | validation, formal Quick Score, backtest summary | A/B normally advance; C/D do not; negative signed RankIC allows one global sign flip only | keepers to novelty or evidence-based return upstream |
| 6 | `novelty_review` | keepers, active values, batch, ST evidence | Calculate Pearson, Spearman, and p90 crowding; apply ST/combined guard | novel candidates to deep validation |
| 7 | `deep_validation_review` | backtest, anti-overfit, Rolling, adversarial | Check complete numeric evidence, IC/IR, complexity, Deep Score, and gap | gate-ready candidate or evidence/mutation route |
| 8 | `import_gate_review` | formal quality gate, name, metadata | Accept only code-adopted outcomes; LLM cannot override rejection | adopted candidate to import |
| 9 | `import_review` | import result, Registry, value sync | Confirm real `imported`, factor ID, value file, and synchronization | imported references or engineering blocker |
| 10 | `round_synthesis` | formal outcome, trajectory, recent history | Record retained mechanism, failed relation, and proper return layer | next round or checkpoint stop |

Three interpretable iteration strategies are allowed:

- `EXPLOIT`: retain an evidenced parent and change one attributable role;
- `RECOMBINE`: combine complementary parents; return to hypothesis for a new
  information relation or expression for within-hypothesis restructuring;
- `EXPLORE`: return to thesis only after the present mechanism has no parent
  value.

### 3.2 Expression and label calculation

The parser validates fields and operators before scoring. Time-series operators
use each security's full available history before output dates are trimmed.
Cross-sectional operators run within the governed tradable non-ST universe.
Current formal defaults are:

```text
universe = tradable_non_st
holding_period = 5
n_groups = 5
top_frac = 0.20
cost_rate = 0.003
benchmark = hs300
neutralize_cap = true
neutralize_industry = false
```

The formal label is the return after signal date over the five-day horizon.
Rolling v2 uses the same calendar-date T+N close-return contract. Per-symbol row
shifts are not an equivalent substitute because suspensions change the real
horizon.

Daily IC is the cross-sectional factor/forward-return correlation; RankIC is
Spearman correlation, and IR is the mean daily IC divided by its standard
deviation. The backtest also records long-short return, monotonicity, top-group
annualized return, Sharpe, drawdown, and turnover. Admission still requires a
usable long-only result.

### 3.3 Quick Score

Quick Score combines eight normalized 0–100 components:

```text
Quick = 0.20 * IC_mean_score
      + 0.20 * IC_IR_score
      + 0.10 * RankIC_mean_score
      + 0.10 * RankIC_IR_score
      + 0.15 * AnnualReturn_score
      + 0.10 * Sharpe_score
      + 0.10 * MaxDrawdown_score
      + 0.05 * Turnover_score
```

| Component | 100-point reference | Rule |
| --- | --- | --- |
| `IC_mean` | `abs(IC)=0.04` | `clip(abs(IC)/0.04*100)` |
| `IC_IR` | `abs(IR)=0.50` | `clip(abs(IR)/0.50*100)` |
| `RankIC_mean` | `abs(RankIC)=0.08` | `clip(abs(RankIC)/0.08*100)` |
| `RankIC_IR` | `abs(RankICIR)=0.75` | `clip(abs(RankICIR)/0.75*100)` |
| annual return | `18%` | `clip(max(return,0)/0.18*100)` |
| Sharpe | `0.65` | `clip(max(Sharpe,0)/0.65*100)` |
| max drawdown | `<=10%` | 100; `>=40%` is 0; linear between |
| turnover | `10%..28%` | 100 in range; rises below 10%; declines to 0 from 28% to 60%; `>=60%` is 0 |

Grades are A `>=85`, B `>=70`, C `>=55`, and D `<55`. Only A/B normally
advance to novelty. A negative long-only annual return or Sharpe caps the score
at 59.9 and grade C even when long-short diagnostics look strong.

### 3.4 Novelty is a gate, not score points

Candidates are compared with both the active pool and their batch. Default hard
thresholds are:

```text
max_existing_pearson < 0.75
max_existing_rank_corr < 0.80
p90_pearson < 0.70
p90_rank_corr < 0.75
```

Crossing any threshold, or `novelty_guard.allowed=false`, creates
`novelty_correlation_veto`. `novelty_score` remains evidence but contributes no
Deep Score points, so novelty cannot compensate for weak quality. A hard ST
guard blocks; advisory mode records a risk tag only.

### 3.5 Four classes of deep evidence

Anti-overfit runs IC stability, subsample stress, placebo, and half-life tests.
Their weights are `30% / 25% / 25% / 20%`. Recommendations are `>=80`
recommended, `>=60` cautious, `>=40` needs improvement, and `<40` not
recommended. A score below 50 or an explicit reject/fail recommendation is a
quality-gate veto.

Rolling v2 divides the latest 48 months into `0–6`, `6–12`, `12–24`, `24–36`,
and `36–48` month non-overlapping periods with base weights
`0.40 / 0.25 / 0.15 / 0.12 / 0.08`. At least 24 months are required, the first
three periods are mandatory, and each six-month unit normally requires 60 valid
RankIC dates. Missing optional older periods are removed and weights are
renormalized.

```text
weighted_ic  = sum(effective_weight_i * signed_rank_ic_i)
weighted_std = sqrt(sum(effective_weight_i * (rank_ic_i - weighted_ic)^2))
robust_ic    = weighted_ic - 0.25 * weighted_std
rolling_score = clip(robust_ic / 0.08 * 100, 0, 100)
```

Rolling never takes `abs(IC)` and never flips direction. A weak result lowers
Deep Score through its 20% weight but is not a separate hard veto.

Adversarial validation runs label permutation, temporal block shuffle, random
universe, and noise injection. Their four scores are equally averaged, and the
official pass threshold is 60.

The formal backtest must also be complete, finite, and consistent with the
candidate holding period. IC, ICIR, RankIC, RankICIR, annual return, Sharpe,
drawdown, and turnover evidence must be available.

### 3.6 Final import score and hard gates

The sole official Deep Score formula is:

```text
Deep = 0.55 * Quick
     + 0.15 * AntiOverfit
     + 0.20 * Rolling
     + 0.10 * Adversarial
```

Any missing numeric component makes the score incomplete and therefore zero.
Novelty remains an admission guard. Admission requires all of the following:

```text
Deep Score >= 80
abs(RankIC_mean, falling back to IC_mean) >= 0.02
abs(RankIC_IR, falling back to IC_IR) >= 0.30
Adversarial score >= 60
Anti-overfit does not fail
Novelty, hard ST, and combined guards pass
Expression, backtest, holding period, and numeric evidence are complete
Long-only annual return >= 0 and Sharpe >= 0
```

Example:

```text
Quick=88, AntiOverfit=82, Rolling=79, Adversarial=75
Deep = 88*0.55 + 82*0.15 + 79*0.20 + 75*0.10
     = 48.40 + 12.30 + 15.80 + 7.50
     = 84.00
```

The score threshold alone is insufficient. RankIC 0.018 still rejects this
candidate; a novelty threshold breach also rejects it. Conversely, strong IC
and IR cannot admit a candidate with Deep Score 78. The decision is the score
threshold AND every hard gate.

### 3.7 What import commits

Auto-import repeats consistency checks after gate adoption: gate and deep
scores agree, all four components and Rolling exist, Registry metrics are
complete, and no active expression duplicate exists. It then:

1. computes values using full per-security history and trims the value window;
2. audits date coverage and daily valid counts;
3. saves an independent factor-value parquet;
4. creates or repairs a readable factor name, category, and unique data column;
5. registers an active factor with universe, holding period, scores, metrics,
   all guards, lineage, and value path;
6. leaves the historical wide store unchanged in this transaction; the
   active-values worker refreshes the active feature store after Registry
   commit and records the Registry fingerprint.

`run completed`, `gate adopted`, and `imported=1` are therefore distinct states.

## 4. Model training: feature snapshot to production model

### 4.1 Input snapshot

Models do not read an arbitrary “latest factor file.” Feature Set Builder reads
active Factor Registry definitions and values and emits a manifest, combined
feature file, and `registry_fingerprint`. Preflight verifies production-data
health, snapshot date, factor IDs/columns/files, fingerprint freshness, and the
label/holding-period contract.

The default `LABEL0` is the five-day next-open-to-forward-open return. The
portfolio contract is top20/drop2/hold5 at the open against CSI 300.

### 4.2 Research, confirmation, Rolling, and promotion

| Stage | Calculation | Business result |
| --- | --- | --- |
| protocol/context/feature snapshot | Load contract, history, production data, and snapshot lineage | Frozen session input |
| experiment plan | Define baseline or one attributable parameter experiment | signed round |
| Seed 42 train/backtest | Qlib LGBM, early stopping, prediction, cost-aware portfolio backtest | `pred.pkl`, `ret.pkl`, metrics, manifest |
| research score | Normalize IR, excess return, drawdown, and RankIC/IR | research asset, not candidate |
| Registry write | Store research asset and artifact references | auditable research row |
| round synthesis | Compare with session best and choose next experiment | next move or best round |
| research confirmation | Run Seed 17 and 83 only for the session-best round | fixed `42/17/83` confirmation |
| production Rolling | Four expanding folds per seed, 6m valid, 6m test, 5-day purge | per-fold and stitched evidence |
| Rolling gate | Campaign score plus all stability gates | candidate or research |
| production refit | Fixed Seed 42 on date-shifted train/valid segments | new production artifact, not the tested model |
| production validation/pointer | Audit manifest, metrics, pred, ret, and lineage; atomically write pointer | one active production model |

### 4.3 Research score

```text
IR_score       : 0.50 -> 0, 1.50 -> 100
Return_score   : 10%  -> 0, 60%  -> 100
Drawdown_score : 10%  -> 100, 30% -> 0
RankIC_score   : 0.02 -> 0, 0.05 -> 100
RankICIR_score : 0.20 -> 0, 0.50 -> 100
RankSignal     = 0.5*RankIC_score + 0.5*RankICIR_score
ResearchScore  = 0.40*IR_score + 0.30*Return_score
               + 0.20*Drawdown_score + 0.10*RankSignal
```

Cost-aware excess annualized return below 10% or excess IR below 0.50 is a
research hard flaw. Ordinary rounds run Seed 42 only; the system never selects
the most attractive of three test seeds.

### 4.4 Formal Rolling candidate score

```text
PortfolioQuality = 0.45*IR_score + 0.35*Return_score + 0.20*Drawdown_score
SeedRolling = 0.55*overall + 0.25*worst_fold + 0.20*latest_fold
CampaignRolling = 0.55*overall_median
                + 0.25*worst_fold_median
                + 0.20*latest_fold_median
```

The campaign requires seeds `{42,17,83}` and exactly four folds. Candidate
admission requires `CampaignRolling >= 70` plus every stability gate: at least
two positive stitched IR values, bounded IR and return standard deviation,
bounded median drawdown, at least three positive fold-median IR values, and a
positive latest-fold IR. Score alone cannot override a failed gate.

## 5. Prediction and recommendation: production model to pending

The standard workflow has eight steps:

1. Resolve the active production pointer and Registry row.
2. Verify model run, feature set, artifacts, data date, and lineage.
3. Recompute stale factors with full warm-up history into a runtime-only
   feature cache; never mutate the training snapshot.
4. Generate a cross-sectional score for the target date.
5. Reject degenerate scores. With enough records, unique scores must reach
   `min(max(topk*3,20), max(record_count//20,1))` and standard deviation must
   not be near zero.
6. Exclude delisted/ST securities using target-date PIT identity; match
   coverage must be at least 95%.
7. Rank, apply confidence exposure and three-layer risk caps, and build targets.
8. Write a deterministic recommendation ID, order preview, frozen contract and
   risk evidence, with status `pending`.

At a tied top-k boundary the confidence contract selects only scores strictly
above the boundary; it does not use symbol order as an artificial tiebreak. A
model with no more than one built tree receives model multiplier 0.5; otherwise
1.0. The current performance multiplier defaults to 1.0.

```text
exposure_multiplier = min(model_multiplier, performance_multiplier)
slot_weight = exposure_multiplier / topk
model_stock_cap = slot_weight * selected_count
final_stock_cap = min(model_stock_cap, market_cap, account_cap)
```

Market stress requires CSI 300, 500, and 1000 positive-return breadth to be at
or below one third on both 20- and 60-day horizons, while the maximum annualized
volatility is at least 18%. It enters after two days and exits after three; its
stock cap is 75%. The account cap becomes 50% only when market stress is active
and the 60-day account drawdown reaches -8%. Shadow mode records the decision;
enforced mode scales all target weights.

## 6. Qlib paper trading: pending to fills and NAV

FXAlpha no longer uses vn.py. Each active account has an explicit model
deployment and strategy contract. Its daily order is:

```text
account integrity check
  -> execute old pending recommendations due today
  -> publish post-fill account state
  -> generate today's score, target, and new pending recommendation
  -> account-day audit
  -> mark account run completed
```

A signal on T normally executes on the next Qlib trading date T+1. If that date
is unavailable, it stays pending; T prices cannot be used early.

- Target shares are approximately `target_weight * pretrade_value / deal_price`
  rounded down by the Qlib trade unit. A-share lots are normally 100, but the
  Exchange factor is authoritative.
- Default open cost is 0.15%, close cost is 0.25%, and minimum fee is 5.
- The default policy is top20, drop2, hold5.
- Risk-cap reduction may override strategic `n_drop/hold5`, but never
  suspension, price-limit, lot-size, price, cash, or tradability constraints.
- Sells run before buys. Orders, fills, constraints, cash, positions, and file
  hashes are persisted as audit assets.
- Account/date/config produces a deterministic run ID; a completed run returns
  `already_completed` instead of booking twice.

The fleet governs isolated accounts sequentially; it does not combine their
cash or holdings. Replay beyond five trading dates requires explicit
confirmation, and blockers retain the account, date, stage, and recovery
evidence.

## 7. Where to find operational truth

| Question | Start with | Final evidence |
| --- | --- | --- |
| Is data actually usable? | `data-status` / Data Foundation GUI | production pointer + same-package post-promote audit + production health |
| Why was a factor not imported? | `factor-orch status` | stage evidence + quality gate + import result + Factor Registry |
| Can a model go to production? | `model-status` | feature fingerprint + research confirmation + Rolling campaign + refit validation |
| Why is there no recommendation? | `pred-status` / trading preflight | model/date/provenance + score diversity + ST/risk evidence |
| Why was there no fill? | account/fleet status | pending execution date + Qlib constraint/fill + account-run audit |

The most common mistake is stopping at a top-level `completed`. Always inspect
the final governed asset and its audit status.

## 8. Documentation layers and code authority

- This document: business steps, formulas, and acceptance conditions.
- [User Guide](USER_GUIDE.md): pages, surfaces, and operating order.
- [Data Workflow](DATA_FOUNDATION_WORKFLOW_CURRENT.md): data implementation and operations.
- [Factor Domain Contract](../domain/factor_research/README.md): factor rules.
- [Factor Orchestrator Contract](../domain/factor_research/ORCHESTRATOR_README.md): factor state machine.
- [Model Domain Contract](../domain/model/README.md): model rules.
- [Multi-model Paper Trading](PRODUCTION_MULTI_MODEL_PAPER_TRADING_CURRENT.md): accounts and fleet.
- [Trading Runbook](TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md): trading operations.
- [Architecture](ARCHITECTURE.md): module boundaries and system architecture.

If documentation and implementation diverge, current calculations in
`domain/`, routing in `services/`, and contract tests are the execution
authority. The mismatch is a documentation defect to repair, not an invitation
for operators to guess.
