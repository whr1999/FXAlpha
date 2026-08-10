# FXAlpha User Guide

**English** | [简体中文](USER_GUIDE.zh-CN.md)

This guide answers the practical question a new user has after cloning
FXAlpha: where to start, how to use each surface, and how market data, factor
research, model research, and Qlib paper trading form one governed workflow.

This guide explains how to operate the platform. For stage counts,
calculation logic, factor import scoring, model promotion, exposure caps, and
Qlib execution, also read
[Business Workflows and Calculation Logic](BUSINESS_WORKFLOWS.md).

> FXAlpha is research and paper-trading infrastructure, not a bundled dataset
> or a live-trading client. The public repository contains source, tests,
> sanitized examples, and documentation only. Users provide licensed data,
> credentials, factor values, trained artifacts, and account state outside Git.

## 1. Choose the right surface

| Surface | Primary user | Purpose | Write operations |
| --- | --- | --- | --- |
| Web GUI | Researchers and operators | Observe readiness, inspect gates, launch and monitor workflows | Allowed only after reviewing preflight and confirmation |
| CLI | Developers, CI, incident diagnosis | Exact status, regression checks, and single-step diagnosis | Manual or failure fallback rather than the routine production path |
| MCP | Governed agents and automation | Platform, model, and QuantGPT tools with shared evidence | Preferred governed automation surface |

All three surfaces call the same services, domain gates, registries, and audit
records. A healthy GUI or API does not prove that data, factors, models,
prediction, and trading are all ready; each lane has its own readiness gate.

## 2. Install and open the platform

### Requirements

- Linux or WSL2;
- Python 3.11 or 3.12;
- Git with submodule support;
- disk and memory appropriate for the selected data/model workload;
- licensed provider accounts and API credentials for data refresh or LLM
  research.

### Clone the pinned forks

```bash
git clone --recurse-submodules https://github.com/whr1999/FXAlpha.git
cd FXAlpha
```

If submodules were omitted:

```bash
git submodule update --init --recursive
```

QuantGPT, Qlib, and Tushare live under `third_party/` as pinned Git submodules.
Do not silently replace them with arbitrary latest packages.

### Configure and bootstrap

```bash
cp config.example.yaml config.yaml
./scripts/bootstrap_public_env.sh
```

Keep provider and LLM credentials in the ignored local `config.yaml`, or set
`FXALPHA_CONFIG_FILE` to a protected file outside the checkout. Never add real
values to examples, documentation, screenshots, issues, or test fixtures.

An unconfigured clone can start, show readiness failures, run `--help`, and run
the source test suite. Provider-backed and production-asset workflows remain
blocked until their inputs are configured.

### Start API and GUI

```bash
PYTHONPATH=. .venv/bin/python api_server.py --host 127.0.0.1 --port 18081
```

Open `http://127.0.0.1:18081/gui/`. Confirm that the API is online, overview
loading completes, each module reports an explicit ready/blocked/waiting state,
and preflight is read before any write operation.

The server is loopback-only by design. The repository has no public-edge auth
contract; do not expose port 18081 directly to the Internet.

## 3. What each GUI module does

| Page | First check | Purpose | Detailed contract |
| --- | --- | --- | --- |
| Platform overview | API, data date, module state, scheduled workflows | Find the lane that needs attention | [Platform operations](PLATFORM_OPS_RUNBOOK.md) |
| Data foundation | Production date, coverage, quality, live update stage | Query data, preflight, stage, review, and promote | [Data workflow](DATA_FOUNDATION_WORKFLOW_CURRENT.md) |
| Factor research | Run/round/stage, candidates, blockers | Start/resume Orchestrator and review evidence | [Factor operations](FACTOR_RESEARCH_OPERATIONS.md) |
| Model research | Feature snapshot, preflight, task and Rolling evidence | Run research/production evaluation and review results | [Model workflow](MODEL_RESEARCH_WORKFLOW_CURRENT.md) |
| Stock research | Promoted data/model projection | Inspect a security without bypassing readiness | [Field dictionary](DATA_FOUNDATION_TUSHARE_FIELD_DICTIONARY_CURRENT.md) |
| Paper trading | Accounts, deployments, pending work, positions and replay gaps | Preflight and confirm account/fleet/replay actions | [Paper fleet contract](PRODUCTION_MULTI_MODEL_PAPER_TRADING_CURRENT.md) |
| Factor library | Active/retired factors, quality and correlation clusters | Audit factors and recommend feature sets | [Platform governance MCP](PLATFORM_GOVERNANCE_MCP_CURRENT.md) |
| Model registry | Research, Rolling, production and lineage | Review promotion and provenance | [Model naming](MODEL_NAMING_CONTRACT.md) |
| Platform operations | Disk, protected assets, cleanup preview | Safe dry-run first; execute only with approval | [Runtime paths](RUNTIME_AND_DATA_PATHS_CURRENT.md) |

## 4. Standard data-to-paper workflow

This chapter gives the operating order. Chapters 2–6 of
[Business Workflows and Calculation Logic](BUSINESS_WORKFLOWS.md) contain the
complete stages and formulas.

### Data foundation

Data is built in isolated staging, checked for quality and lineage, and only
then promoted. Production paths stay unchanged until promotion succeeds.

```text
data-status
  -> data-daily-preflight
  -> data-stage-update
  -> review staged quality and lineage
  -> data-promote-staged
  -> data-production-audit
```

Read-only examples:

```bash
.venv/bin/python cli.py data-status
.venv/bin/python cli.py data-daily-preflight
.venv/bin/python cli.py data-production-audit
```

Do not force promotion through a blocker or overwrite production HDF, Qlib, or
QuantGPT data directly. See the [daily runbook](DATA_FOUNDATION_DAILY_RUNBOOK_CURRENT.md),
[full rebuild runbook](DATA_FOUNDATION_TUSHARE_REBUILD_RUNBOOK_CURRENT.md), and
[network and quality policy](DATA_FOUNDATION_DIRECT_NETWORK_AND_QUALITY_POLICY_CURRENT.md).
The [business logic guide](BUSINESS_WORKFLOWS.md) explains all 11 production
daily steps and their acceptance conditions.

### Factor research and library

FXAlpha Orchestrator is the default controller. It combines bounded LLM
judgment with shared QuantGPT/FXAlpha expression validation, scoring, novelty,
deep validation, quality gate, and governed import. Native MCP mode is for
explicit debugging and evidence review; it does not lower the standard.

```bash
.venv/bin/python cli.py factor-status
.venv/bin/python cli.py factor-orch status
.venv/bin/python cli.py factor-audit status
```

A completed run is not proof of import. Verify the quality-gate decision,
import record, and active snapshot independently. Continue with the
[11-stage mining and import-scoring logic](BUSINESS_WORKFLOWS.md),
[factor contract](../domain/factor_research/README.md),
[Orchestrator contract](../domain/factor_research/ORCHESTRATOR_README.md), and
[operations runbook](FACTOR_RESEARCH_OPERATIONS.md).

### Model research and registry

Models consume a fingerprinted active feature snapshot, not an arbitrary
latest factor file. The snapshot records data, factor-registry, and feature-set
lineage.

```text
model-status
  -> active feature snapshot ready
  -> research or production Rolling preflight
  -> train, validate, and collect forward evidence
  -> registry review
  -> explicit promotion
```

Read-only examples:

```bash
.venv/bin/python cli.py model-status
.venv/bin/python cli.py model-runs
.venv/bin/python cli.py model-registry
.venv/bin/python cli.py model-production
```

`accepted` means that an asynchronous task was created; training completion is
also not automatic production promotion. Verify task state, evidence, registry,
and the production pointer. See the [model scoring and promotion logic](BUSINESS_WORKFLOWS.md),
[model module](../domain/model/README.md),
[workflow](MODEL_RESEARCH_WORKFLOW_CURRENT.md), and
[pretest checklist](MODEL_RESEARCH_PRETEST_CHECKLIST_CURRENT.md).

### Prediction, recommendation, and Qlib paper trading

vn.py is not part of the public architecture. FXAlpha owns account state,
recommendations, deployments, idempotency, and audit; Qlib owns exchange
semantics such as deal price, fees, trade units, and limit handling.

Before a run, verify production data health, a promoted model, aligned
prediction dates, an active account deployment, and paper-fleet preflight.

```bash
.venv/bin/python cli.py pred-status
.venv/bin/python cli.py paper-fleet-status
.venv/bin/python cli.py paper-fleet-preflight
```

Account creation, fleet execution, and replay are writes. Review the GUI plan
and use explicit account/date/confirmation parameters. Never edit SQLite, JSON,
or account state to bypass a blocker. See the
[prediction, risk, and Qlib execution logic](BUSINESS_WORKFLOWS.md),
[paper fleet contract](PRODUCTION_MULTI_MODEL_PAPER_TRADING_CURRENT.md),
[operator runbook](TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md), and
[Qlib limit contract](QLIB_LIMIT_TRADING_CONTRACT_CURRENT.md).

## 5. MCP roles

`.codex/config.example.toml` contains relative-path examples only.

| MCP | Responsibility |
| --- | --- |
| QuantGPT MCP | Factor expressions, scoring, backtests, diagnostics, and evidence |
| Model MCP | Model context, research tasks, Rolling, status, and promotion evidence |
| FXAlpha Platform MCP | Cross-lane governance, factor-library audit, maintenance preview, and service health |

MCP is a governed interface, not a bypass. Production factor research requires
native QuantGPT MCP tools. If they are unavailable, repair the configuration;
do not substitute curl, temporary Python, or HTTP glue. See
[platform governance MCP](PLATFORM_GOVERNANCE_MCP_CURRENT.md),
[LLM integration](LLM_INTEGRATION.md), and
[engineering guardrails](CODEX_ENGINEERING_GUARDRAILS.md).

## 6. Read status precisely

| Status | Meaning | Next action |
| --- | --- | --- |
| `ready` | This lane's defined gates pass | Check the downstream lane separately |
| `waiting` | An upstream date, asset, or plan is not ready | Preserve state and resolve the named upstream dependency |
| `blocked` | A traceable hard gate failed | Read the blocker and referenced run/package/account; do not bypass |
| `accepted` | An async task was received | Follow task state, logs, and artifacts; it is not completion |
| `completed` | The current task flow ended | Verify import/promotion/audit/production health separately |
| `already_current` | An idempotent task is caught up | Do not create duplicate work or ledger rows |

## 7. Troubleshooting order

1. Check `GET /health` and GUI availability.
2. Use the overview to identify the failing lane.
3. Open the module and read the exact blocker, not only its color.
4. Confirm with the matching read-only CLI or MCP status.
5. Inspect the current run, package, model, or account evidence; do not start a
   parallel replacement.
6. Repair the cause, resume the same task, and verify downstream dates and
   registries.

For maintenance and governed cleanup, read the
[platform operations runbook](PLATFORM_OPS_RUNBOOK.md). Always preview first;
active, production, recent, running, and locked assets remain protected.

## 8. Public repository boundary

Track source, tests, sanitized examples, documentation, public-safe diagrams,
Gitlinks, and lock files. The only runtime views in this repository are the
individually owner-approved static captures listed in the screenshot manifest.
Never track credentials, local configuration, machine-readable market data,
factor values, active snapshots, trained models, predictions, account state,
P&L, databases, logs, traces, backups, runtime directories, or personal tooling
state.

Before publishing:

```bash
.venv/bin/python scripts/run_release_preflight.py
```

See the [GitHub upload runbook](GITHUB_UPLOAD_RUNBOOK.md).

## 9. Documentation map

- Understand collaboration and ownership: [Architecture](ARCHITECTURE.md).
- Deploy on a new machine: [Local deployment](LOCAL_DEPLOYMENT.md).
- Operate a lane: use its section and runbook in chapter 4.
- Contribute code: [Project structure](PROJECT_STRUCTURE_CURRENT.md),
  [Contributing](../CONTRIBUTING.md), and
  [Engineering guardrails](CODEX_ENGINEERING_GUARDRAILS.md).
- Publish GitHub: [Publication readiness](GITHUB_PUBLICATION_READINESS.md),
  [Verification report](VERIFICATION_REPORT_20260810.md), and
  [Upload runbook](GITHUB_UPLOAD_RUNBOOK.md).
- Browse every current document: [Documentation index](DOCUMENTATION_INDEX_CURRENT.md).
