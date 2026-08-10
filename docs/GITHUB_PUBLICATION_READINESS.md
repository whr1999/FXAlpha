# GitHub publication readiness

Audit and publication date: 2026-08-10

## Result

Status: **ready for the `v0.1.0` release**.

The public repository is [whr1999/FXAlpha](https://github.com/whr1999/FXAlpha).
It is source-visible under the repository's all-rights-reserved license; it is
not represented as an open-source project. Publication did not migrate or
replace the deployed production workspace.

## Isolation boundary

All public-release construction and repair work was performed in a separate
checkout outside the deployed tree. The deployed source directory, user
services, production databases, market data, Qlib data, factor values, trained
models, predictions, paper-account state, runtime files, and archived vn.py
environment were not switched or edited.

## Phase closure

### Phase 1 — freeze and inventory

Status: complete.

- classified source, data, runtime, secrets, artifacts, and third-party trees
- recorded the production baseline without placing private evidence in the
  public repository
- preserved production and archived vn.py state outside the public checkout

### Phase 2 — public candidate construction

Status: complete.

- built a clean public Git history outside the production checkout
- excluded data, databases, factor values, models, predictions, accounts,
  runtime state, logs, caches, virtual environments, credentials, and private
  archives
- added sanitized examples, bilingual entrypoints, user guides, workflow
  specifications, screenshots, and an end-to-end system architecture diagram
- made local paths configurable and added fail-closed tree/history audits

### Phase 3 — dependencies and execution cleanup

Status: complete.

- published QuantGPT, Qlib, and Tushare as real public forks and locked exact
  reviewed commits
- preserved Tushare 1.4.29 byte/hash/license provenance on the approved fork
  branch
- retired vn.py application and execution contracts from the public platform
- retained Qlib exchange semantics for paper-account execution and replay
- pinned the dependency set tested by Python 3.11 and 3.12 CI

### Phase 4 — verification and GitHub controls

Status: complete.

- public-tree, reachable-history, compile, documentation, topology, and test
  gates pass
- an anonymous network-fresh recursive clone resolves all three Gitlinks
- protected `main` requires Python 3.11, Python 3.12, and CodeQL checks, enforces
  linear history, and blocks force pushes and deletion
- secret scanning, push protection, Dependabot security updates, private
  vulnerability reporting, and CodeQL are enabled
- current open alert counts are zero for Dependabot, CodeQL, and secret scanning

The detailed bilingual evidence is in
[`VERIFICATION_REPORT_20260810.md`](VERIFICATION_REPORT_20260810.md) and
[`VERIFICATION_REPORT_20260810.zh-CN.md`](VERIFICATION_REPORT_20260810.zh-CN.md).

## Published third-party pins

| Component | Public fork | Locked commit |
| --- | --- | --- |
| QuantGPT | `whr1999/QuantGPT` | `024818abcf76b35f0a8282f9a212c2309716defd` |
| Qlib | `whr1999/qlib` | `d5379c520f66a39953bad76234a7019a72796fd0` |
| Tushare 1.4.29 | `whr1999/tushare` | `bc5388dcb339ce7e11515cab5cb6087b3724e74b` |

The release check is fail closed:

```bash
python scripts/run_release_preflight.py --release
```

It must not be replaced by a local-path clone.

## Release and production limits

The repository publication and `v0.1.0` release do not authorize a production
cutover. A later cutover remains a separate reviewed operation using isolated
ports, copied registries, read-only large-data mounts, independent acceptance,
and a tested rollback. No production data migration, systemd switch, package
uninstall, or `.vntrader` deletion was performed by the publication work.
