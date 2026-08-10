# Architecture

The diagram below shows the end-to-end system and governed asset flow: Tushare
staging and promotion, QuantGPT factor research, factor-library import,
fingerprinted feature snapshots, Qlib model training and promotion, production
prediction, recommendation, and Qlib paper execution. It also shows the shared
access/governance and storage layers that serve every module.

For the exact stage count, formulas, gate thresholds, and commit semantics
inside every box, continue with [Business Workflows and Calculation Logic](BUSINESS_WORKFLOWS.md)
or its [Chinese edition](BUSINESS_WORKFLOWS.zh-CN.md).

![FXAlpha end-to-end system architecture](assets/fxalpha-system-architecture.svg)

## System boundary

FXAlpha is one application with three operator surfaces—HTTP/GUI, CLI, and
MCP—over shared service and domain layers. It is not three independent
pipelines. A state-changing operation must pass through the same gate and write
the same audit record regardless of surface.

```text
Operator surfaces
  api_server.py + gui/     cli.py     mcp_servers/
              \             |             /
                       services/
                           |
  domain/data_foundation  domain/factor_research  domain/model  domain/trading
                           |
                        storage/
          data/ (durable local state) + runtime/ (run evidence/status)

third_party/tushare -> data foundation
third_party/quantgpt -> factor research
third_party/qlib -> model training, backtest, and paper execution
```

## Production workflow

1. Tushare data is downloaded into a staged package.
2. Data quality and lineage gates run before promotion.
3. Promoted data feeds QuantGPT factor research and Qlib datasets.
4. Candidate factors pass novelty, rolling validation, quality gate, and
   governed registry import.
5. Model research consumes a fingerprinted active-factor snapshot, performs
   rolling evaluation, and promotes only accepted artifacts.
6. Prediction and recommendation consume a promoted model.
7. The paper fleet uses Qlib exchange semantics and persists recommendations,
   executions, positions, and account snapshots in the FXAlpha registry.

vn.py is not part of this architecture.

## Storage ownership

| Location | Ownership | Git policy |
| --- | --- | --- |
| source and docs | repository | tracked |
| `third_party/` | Git submodules | gitlinks only |
| `config.yaml`, `.env*` | operator | ignored |
| `data/` | durable local datasets/registries | ignored and backed up separately |
| `runtime/` | status, evidence, locks, traces | ignored and lifecycle-governed |
| `mlruns/`, `artifacts/`, logs | generated model/runtime output | ignored |

`storage/paths.py` is the code-level path authority. `FXALPHA_CONFIG_FILE`
selects an external config file; relative defaults remain rooted at the cloned
repository.

When durable data is mounted outside an immutable release checkout, the raw,
Qlib, QuantGPT, factor, model, metadata, and trading roots form one deployment
contract. In particular, `metadata_root` must follow the production data mount,
and relative dataset-manifest paths are resolved against the configured raw HDF
before any expensive live quality fallback.

## Security boundary

The bundled API defaults to `127.0.0.1`. It is an application service, not a
public gateway. Internet exposure requires a separately managed authenticated
TLS reverse proxy, authorization policy, rate limiting, audit logging, and
network controls.

## Architecture audit findings

The functional boundaries are sound enough for publication, but the following
technical debt should be handled after the public baseline is frozen:

| Priority | Finding | Evidence | Recommendation |
| --- | --- | --- | --- |
| P1 | Factor service is a large change hotspot | `services/factor_research_service.py` is about 19.6k lines | Split context/status, orchestration, task-store, and API DTO modules behind unchanged service functions |
| P1 | API boot uses broad optional-import fallbacks | `api_server.py` records import failures and continues | Add explicit capability health and fail startup for configured mandatory lanes |
| P1 | Public-edge auth is absent | loopback is the only safe default | Keep loopback; design gateway/auth before any remote deployment |
| P2 | Configuration is evaluated at import time | `storage/paths.py` builds constants on import | Introduce an immutable settings object and dependency injection in a later compatibility release |
| P2 | Several service/domain modules remain over 1k lines | measured in the publication audit | Refactor by cohesive ownership, with contract tests before moving code |
| P2 | SQLite write concurrency is an operational constraint | registries and task stores are SQLite-backed | Keep single-writer/lock discipline; document backup and migration before multi-host deployment |
| P2 | Fork drift must be actively governed | three pinned third-party forks | Monthly upstream sync, changelog, CI, license diff, and submodule-pin review |
| P2 | Deep diagnostics share process and I/O capacity | explicit maintenance audit and other cache-miss paths can delay work | The candidate makes ordinary maintenance API reads snapshot-first; keep `?deep=true` and other deep audit/preflight work explicit and continue isolating expensive jobs |

No production refactor is included in the publication-preparation change. This
keeps the deployed architecture stable while making the debt visible and
actionable.
