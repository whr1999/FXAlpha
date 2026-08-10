# Changelog

All notable changes to the public FXAlpha project are recorded here. Release
dates and tags are added only after the corresponding GitHub CI run passes.

## [0.1.0] - Pending

### Added

- governed workflows from market-data preparation through factor research,
  model training, prediction, recommendation, and Qlib paper trading
- HTTP/GUI, CLI, and MCP adapters over shared domain and service boundaries
- pinned QuantGPT, Qlib, and Tushare fork contracts
- sanitized configuration examples and portable external-data paths
- public-tree, full-Git-history, submodule-topology, and release preflight gates
- GitHub CI, CodeQL, Dependabot, issue forms, and publication runbooks
- bilingual business-workflow specifications with stage-by-stage inputs,
  calculations, formulas, gates, commit semantics, and acceptance evidence for
  data, factor, model, prediction, and Qlib paper-trading lanes

### Changed

- maintenance status is snapshot-first, with deep inspection explicitly opted in
- Tushare 1.4.29 source provenance is tied to a verified PyPI wheel hash
- the first Tushare fork release accepts disconnected Git ancestry while
  retaining byte-level wheel, metadata, hash, and license provenance
- the English and Chinese repository entrypoints now lead with first-use steps,
  task-to-module routing, safe read-only checks, and direct links to a complete
  bilingual operator guide; architecture is documented with a public-safe
  end-to-end system and module-data-flow diagram
- the system diagram explicitly connects data foundation, QuantGPT factor
  research, factor library, fingerprinted feature snapshots, Qlib model
  training, model registry, prediction, recommendation, and Qlib paper trading
- the consolidated release preflight uses pytest system capture so WSL or
  sandboxed desktop sessions cannot fail before collection when an fd-capture
  temporary backing file disappears; Linux test subprocesses also prefer
  native `/tmp` over an inherited Windows TEMP path

### Fixed

- platform overview rendering no longer stops on out-of-scope model-registry
  readiness and signed-percentage helpers; live data cards now replace the
  initial loading skeleton
- the overview asset is versioned as `20260810-overview-v128` so embedded
  browsers cannot keep executing the stale loader; render failures now replace
  the loading skeleton with a visible, versioned diagnostic
- public CI installs PyTables for the HDF5-backed data tests and detects broken
  pytest session symlinks consistently on Python 3.11 and 3.12
- GUI asset serving uses canonical containment instead of a string-prefix path
  check, and response headers use strict origin and content-type allowlists

### Removed

- vn.py application, dependency, runtime, and paper-execution contracts from the
  public candidate; paper execution uses Qlib exchange semantics

### Security

- runtime data, databases, credentials, logs, artifacts, and operator-private
  paths are excluded from the repository and its reachable history
- trained-model formats, factor-value datasets, archives, personal assistant
  configuration, and editor state are rejected by ignore and audit policy
- GitHub Actions are fixed to full commit SHAs and run with minimum permissions
- API launch defaults to loopback; preflight subprocess dates and Tushare
  source-bind addresses are strictly normalized before use
- MCP, MLflow, PyArrow, Requests, and PyTables are pinned to the versions
  validated by the final public release gate; generic Dependabot submodule
  updates cannot replace reviewed fork integration pins

[0.1.0]: https://github.com/whr1999/FXAlpha/releases/tag/v0.1.0
