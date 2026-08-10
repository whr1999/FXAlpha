# FXAlpha system interface

These screenshots explain how the platform modules connect. They do not
replace setup instructions, business contracts, or test evidence. They are
**real point-in-time UI records from 2026-08-10, explicitly approved for public
release by the project owner**. Displayed status and statistics change with the
runtime; they are not seed data for a fresh installation or a performance
promise.

The images were reviewed and no API key, token, login credential, local file
path, or device metadata was found. The repository pins filenames, dimensions,
byte sizes, and SHA-256 values in
[`manifest.json`](assets/screenshots/manifest.json). Replacements require a new
manual review and manifest update.

## 1. Platform overview

![Platform overview](assets/screenshots/platform-overview.jpeg)

The command center puts data, factor, model, prediction, paper-trading, and
background-workflow state on one surface. Use it to locate the lane that is
ready, waiting, or blocked; it is not a bypass around module preflights.

Details: [Complete user guide](USER_GUIDE.md) ·
[Platform operations](PLATFORM_OPS_RUNBOOK.md)

## 2. Factor research

![Factor research](assets/screenshots/factor-research.jpeg)

The factor page presents the research run, round, stage, candidate expression,
scores, and blocker. A visible candidate is not an imported factor: expression,
Quick, novelty, Rolling, Deep, and quality gates must all pass before registry
commit.

Details: [Business workflows, Chapter 3](BUSINESS_WORKFLOWS.md) ·
[Factor research operations](FACTOR_RESEARCH_OPERATIONS.md)

## 3. Model research

![Model research](assets/screenshots/model-research.jpeg)

The model page combines feature snapshots, training jobs, Rolling/forward
results, risk metrics, and promotion state. Training completion, accepted
evaluation, and production promotion are separate states. Prediction and paper
trading consume only a promoted model with matching provenance.

Details: [Business workflows, Chapter 4](BUSINESS_WORKFLOWS.md) ·
[Model research workflow](MODEL_RESEARCH_WORKFLOW_CURRENT.md)

## 4. Qlib paper trading

![Qlib paper trading](assets/screenshots/paper-trading.jpeg)

The paper page presents capital, holdings, recommendations, risk controls, and
fills by account and model deployment. Qlib paper exchange is the current
execution engine; vn.py is not required. Every advance remains gated by fleet
preflight, date, model, prediction, and risk checks.

Details: [Business workflows, Chapter 6](BUSINESS_WORKFLOWS.md) ·
[Paper-trading operator runbook](TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md)
