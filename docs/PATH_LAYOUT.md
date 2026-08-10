# FXAlpha path and file ownership

This document defines one authority for source, production data, runtime
evidence, configuration, backups, and retired history. Releases stay
replaceable while mutable production assets remain stable.

## 1. Recommended production layout

```text
/home/USER/
├── fxalpha-deploy/
│   ├── current -> releases/<git-commit>/
│   └── releases/<git-commit>/       # immutable code and pinned third_party
├── fxalpha-data/                    # durable business data
│   ├── raw/
│   ├── qlib/
│   ├── quantgpt/
│   ├── factors/
│   ├── model/
│   ├── trading/
│   └── metadata/
├── fxalpha-state/
│   ├── runtime/                     # jobs, status, traces, locks, logs, audits
│   ├── quantgpt/                    # SQLite, reports, and research notes
│   ├── backups/
│   ├── operations/
│   └── test-tmp/
├── fxalpha-archive/                 # retired releases and reviewed snapshots
└── .config/fxalpha/
    ├── config.yaml                  # mode 0600
    └── runtime.env                  # mode 0600
```

A development clone may live anywhere, but long-running services execute only
from `fxalpha-deploy/current`. Data, state, and credentials never belong in a
release checkout.

## 2. Path resolution

`storage/paths.py` is the code authority. Precedence is:

1. a specific file or subdirectory override such as `factor_registry_db`;
2. the standard path derived from `paths.data_root` or `paths.runtime_root`;
3. the in-repository `data/` and `runtime/` defaults for an unconfigured clone.

A new production deployment normally needs only these roots and external
QuantGPT state:

```yaml
paths:
  data_root: /home/USER/fxalpha-data
  runtime_root: /home/USER/fxalpha-state/runtime
  third_party_root: /home/USER/fxalpha-deploy/current/third_party
  quantgpt_code_root: /home/USER/fxalpha-deploy/current/third_party/quantgpt
  qlib_source_root: /home/USER/fxalpha-deploy/current/third_party/qlib
  quantgpt_db: /home/USER/fxalpha-state/quantgpt/quantgpt.db
  quantgpt_research_notes_dir: /home/USER/fxalpha-state/quantgpt/research_notes
```

`data_root` derives `raw/tushare`, `qlib`, `quantgpt`, `factors`, `model`,
`trading`, and `metadata`. Fine-grained legacy overrides remain supported for
compatible migration. Relative values resolve against the release root, never
the caller's working directory. Production should use absolute paths through a
protected external file selected by `FXALPHA_CONFIG_FILE`.

## 3. Ownership and lifecycle

| Class | Authority | Git | Backup and cleanup rule |
| --- | --- | --- | --- |
| Source, tests, public docs | release / development clone | tracked | Git commits and tags |
| Third-party source | release `third_party/` submodules | Gitlinks only | pin commits; no mutable assets |
| Raw, Qlib, factor, model, trading data | `fxalpha-data/` | ignored | application-consistent backups |
| Jobs, traces, locks, logs | `fxalpha-state/runtime/` | ignored | maintenance retention policy only |
| Keys and environment | `.config/fxalpha/` | ignored | mode 0600; never paste into issues/logs |
| SQLite and critical manifest backups | `fxalpha-state/backups/` | ignored | online SQLite backup plus SHA-256 |
| Retired releases and legacy snapshots | `fxalpha-archive/` | ignored | move first; delete only after review |

Public current product documentation lives in `docs/`. Host-specific paths,
cutover timestamps, database checks, and rollback scripts belong in
`fxalpha-state/operations/` or `fxalpha-state/backups/`.

## 4. Safe migration order

1. Record release, services, timers, task `run_id`, and ownership.
2. Pause writers through their control plane at a durable checkpoint; stop timers.
3. Back up SQLite, configuration, units, MCP configuration, and critical manifests.
4. Atomically move data and runtime directories on the same filesystem.
5. Keep explicit compatibility symlinks for historical manifests with old absolute paths.
6. Update external configuration, then validate imports and resolved paths before startup.
7. Check API, data, factor, model, prediction, and paper-trading lanes independently.
8. Re-enable timers and resume the original task from its durable checkpoint.
9. Retain the previous release and migration backup through the observation window.

Never move a directory while a writer is active, manually delete an active
lock, or treat one healthy API response as proof that every lane is ready.

See [`SECURITY.md`](../SECURITY.md), [`LOCAL_DEPLOYMENT.md`](LOCAL_DEPLOYMENT.md),
and [`RUNTIME_AND_DATA_PATHS_CURRENT.md`](RUNTIME_AND_DATA_PATHS_CURRENT.md).
