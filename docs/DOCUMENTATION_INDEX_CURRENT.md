# Current documentation index

Start with the publication and architecture documents below. Operational
runbooks describe governed workflows, but they do not ship production data,
credentials, or service state.

## Repository and deployment

1. [`README.md`](../README.md) and [`README.zh-CN.md`](../README.zh-CN.md) —
   task-oriented project entrypoints, first start, module map, and safety boundary.
2. [`OPERATIONS_INDEX.md`](OPERATIONS_INDEX.md) and
   [`OPERATIONS_INDEX.zh-CN.md`](OPERATIONS_INDEX.zh-CN.md) — single production
   duty, write-gate, incident, deployment, and rollback navigation.
3. [`USER_GUIDE.md`](USER_GUIDE.md) and
   [`USER_GUIDE.zh-CN.md`](USER_GUIDE.zh-CN.md) — complete operator journey,
   GUI module guide, data-to-paper workflow, status semantics, MCP roles, and
   troubleshooting order.
4. [`BUSINESS_WORKFLOWS.md`](BUSINESS_WORKFLOWS.md) and
   [`BUSINESS_WORKFLOWS.zh-CN.md`](BUSINESS_WORKFLOWS.zh-CN.md) — detailed
   business-stage counts, calculations, formulas, gates, commit semantics, and
   evidence for data, factor, model, prediction, and Qlib paper workflows.
5. [`ARCHITECTURE.md`](ARCHITECTURE.md) — end-to-end module/data-flow diagram,
   boundaries, ownership, workflow, and
   architecture-audit findings.
6. [`SCREENSHOTS.md`](SCREENSHOTS.md) and
   [`SCREENSHOTS.zh-CN.md`](SCREENSHOTS.zh-CN.md) — owner-approved point-in-time
   GUI records for overview, factor, model, and Qlib paper workflows.
7. [`GITHUB_PUBLICATION_READINESS.md`](GITHUB_PUBLICATION_READINESS.md) — phase
   1–4 record, blockers, publication order, and later cutover plan.
8. [`GITHUB_UPLOAD_RUNBOOK.md`](GITHUB_UPLOAD_RUNBOOK.md) — exact fork-first,
   main-upload, GitHub-settings, fresh-clone, and first-release procedure.
9. [`LICENSE_POLICY_DECISION.md`](LICENSE_POLICY_DECISION.md) — current
   source-visible terms and decision boundary for external contributions.
10. [`VERIFICATION_REPORT_20260810.md`](VERIFICATION_REPORT_20260810.md) and
   [`VERIFICATION_REPORT_20260810.zh-CN.md`](VERIFICATION_REPORT_20260810.zh-CN.md)
   — final privacy, screenshot, history, regression, topology, and release-gate
   evidence.
11. [`THIRD_PARTY_FORKS.md`](THIRD_PARTY_FORKS.md) — fork/submodule lifecycle.
12. [`VNPY_RETIREMENT.md`](VNPY_RETIREMENT.md) — execution decision and evidence.
13. [`LOCAL_DEPLOYMENT.md`](LOCAL_DEPLOYMENT.md) — isolated install and service
   template instructions.
14. [`PATH_LAYOUT.md`](PATH_LAYOUT.md) and
    [`PATH_LAYOUT.zh-CN.md`](PATH_LAYOUT.zh-CN.md) — canonical production
    directory ownership, configuration precedence, retention, and migration.
15. [`SECURITY.md`](../SECURITY.md), [`SUPPORT.md`](../SUPPORT.md),
    [`CHANGELOG.md`](../CHANGELOG.md), and [`CONTRIBUTING.md`](../CONTRIBUTING.md).

The classification and authority status of every top-level document is checked
against [`DOCUMENTATION_MANIFEST.yaml`](DOCUMENTATION_MANIFEST.yaml).

## Data foundation

1. [`DATA_FOUNDATION_DATASET_REGISTRY_CURRENT.md`](DATA_FOUNDATION_DATASET_REGISTRY_CURRENT.md)
2. [`DATA_FOUNDATION_WORKFLOW_CURRENT.md`](DATA_FOUNDATION_WORKFLOW_CURRENT.md)
3. [`DATA_FOUNDATION_DAILY_RUNBOOK_CURRENT.md`](DATA_FOUNDATION_DAILY_RUNBOOK_CURRENT.md)
4. [`DATA_FOUNDATION_TUSHARE_REBUILD_RUNBOOK_CURRENT.md`](DATA_FOUNDATION_TUSHARE_REBUILD_RUNBOOK_CURRENT.md)
5. [`DATA_FOUNDATION_TUSHARE_FIELD_DICTIONARY_CURRENT.md`](DATA_FOUNDATION_TUSHARE_FIELD_DICTIONARY_CURRENT.md)
6. [`DATA_FOUNDATION_DIRECT_NETWORK_AND_QUALITY_POLICY_CURRENT.md`](DATA_FOUNDATION_DIRECT_NETWORK_AND_QUALITY_POLICY_CURRENT.md)
7. [`QLIB_LIMIT_TRADING_CONTRACT_CURRENT.md`](QLIB_LIMIT_TRADING_CONTRACT_CURRENT.md)

## Factor research

1. [`domain/factor_research/ORCHESTRATOR_README.md`](../domain/factor_research/ORCHESTRATOR_README.md)
2. [`domain/factor_research/README.md`](../domain/factor_research/README.md)
3. [`FACTOR_RESEARCH_OPERATIONS.md`](FACTOR_RESEARCH_OPERATIONS.md)
4. `third_party/quantgpt/PROMPT.md` — explicit MCP debugging/review prompt.

## Model research

1. [`domain/model/README.md`](../domain/model/README.md)
2. [`domain/model/PROMPT.md`](../domain/model/PROMPT.md)
3. [`MODEL_RESEARCH_WORKFLOW_CURRENT.md`](MODEL_RESEARCH_WORKFLOW_CURRENT.md)
4. [`MODEL_RESEARCH_PRETEST_CHECKLIST_CURRENT.md`](MODEL_RESEARCH_PRETEST_CHECKLIST_CURRENT.md)
5. [`MODEL_RESEARCH_PRODUCTION_RUNBOOK.md`](MODEL_RESEARCH_PRODUCTION_RUNBOOK.md)

## Prediction and Qlib paper trading

1. [`PRODUCTION_MULTI_MODEL_PAPER_TRADING_CURRENT.md`](PRODUCTION_MULTI_MODEL_PAPER_TRADING_CURRENT.md)
2. [`TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md`](TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md)
3. [`DATA_TRADING_OPERATION_CONTRACT_CURRENT.md`](DATA_TRADING_OPERATION_CONTRACT_CURRENT.md)

Older implementation audits, production prompt traces, canary results, runtime
reports, and archived documents remain private operational evidence. They are
intentionally omitted from the public candidate; only the current contracts
above are normative.
