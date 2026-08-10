# GitHub publication verification report — 2026-08-10

## Publication result

- public repository: [whr1999/FXAlpha](https://github.com/whr1999/FXAlpha)
- default branch: protected `main`
- intended first release: `v0.1.0`
- license posture: all rights reserved and source-visible; not advertised as
  open source
- production impact: none; the deployed checkout and runtime were not changed

The public history starts from a clean root and contains only reviewed squash
merges. Construction history, local audit bundles, production identifiers, and
private runtime state were never pushed.

## Verified third-party topology

| Component | Fork | Locked commit |
| --- | --- | --- |
| QuantGPT | `whr1999/QuantGPT` | `024818abcf76b35f0a8282f9a212c2309716defd` |
| Qlib | `whr1999/qlib` | `d5379c520f66a39953bad76234a7019a72796fd0` |
| Tushare 1.4.29 | `whr1999/tushare` | `bc5388dcb339ce7e11515cab5cb6087b3724e74b` |

`scripts/verify_publication_topology.py --release` completed with
`status=passed`, three components checked, no release blockers, and no
violations. The check fetches the exact public fork pins and performs a fresh
anonymous recursive clone of the main repository; no local-path substitution
is accepted.

## Automated release gates

| Gate | Verified result |
| --- | --- |
| Public-tree audit | 328 tracked paths, three submodules, zero violations |
| Reachable Git-history audit | all reachable commits and blobs checked; zero violations; largest blob below 5 MiB |
| Publication topology | three exact Gitlinks match the lock file and are anonymously reachable |
| Python source compilation | passed |
| Python 3.11 GitHub CI | 1034 passed, 1 skipped, 1 third-party warning |
| Python 3.12 GitHub CI | 1034 passed, 1 skipped, 1 third-party warning |
| CodeQL | completed successfully; latest default-branch analysis has zero results |

The skipped test is an optional check against a private production feature
snapshot. A public clone intentionally has no such snapshot; the check runs
only when an external `FXALPHA_SANDBOX_FEATURE_SET_ID` is explicitly supplied.

## Security and disclosure audit

At final verification, the public repository reports:

- open Dependabot alerts: **0**; 26 superseded advisories recorded as fixed
- open CodeQL alerts on `main`: **0**
- open secret-scanning alerts: **0**
- secret scanning and push protection: enabled
- Dependabot security updates: enabled

The dependency graph resolves the tested direct pins, including MCP 1.28.1,
MLflow 3.11.1, PyArrow 23.0.1, Requests 2.34.2, and PyTables 3.11.1. The
supported MLflow boundary is local `file://` tracking only; the project does not
launch an MLflow server.

The public tree contains source, tests, current contracts/runbooks, sanitized
examples, one system architecture SVG, four owner-approved UI screenshots, and
pinned third-party Gitlinks. It does not contain API keys, tokens, real local
configuration, market datasets, machine-readable factor values, model files,
registries, account databases, predictions, logs, traces, backups, or runtime
directories.

Tree and history audits reject credential patterns, private paths, production
shaped factor/model/run identifiers, generated-state roots, data/model/archive
formats, unreviewed screenshots, unpinned Actions, dirty Gitlinks, and blobs
above 5 MiB. Secret finding reports contain only a fixed category and location;
matched text and detector labels are never printed.

## Screenshot decision

The project owner explicitly approved the four original runtime captures for
public documentation. Review found no API key, token, login credential, local
filesystem path, EXIF record, comment, or device metadata. The images are
described as point-in-time documentary records, not sanitized demo data or
performance evidence.

[`assets/screenshots/manifest.json`](assets/screenshots/manifest.json) records
the path, dimensions, byte size, and SHA-256 of every screenshot. The
public-tree audit fails on an unreviewed addition, unrecorded removal, or
byte-level replacement without a matching manifest update.

## Repository governance

`main` requires strict Python 3.11, Python 3.12, and CodeQL checks. Administrator
enforcement, conversation resolution, linear history, and force-push/deletion
protection are enabled. Actions use full commit SHAs and minimum read
permissions. Generic Git-submodule Dependabot updates are disabled so a bot
cannot silently replace reviewed fork pins.

Publication is not production migration. No production cutover, data
promotion, factor import, model training, prediction run, paper trade, service
switch, or runtime cleanup was performed. The operating and rollback boundary
remains documented in [`GITHUB_UPLOAD_RUNBOOK.md`](GITHUB_UPLOAD_RUNBOOK.md).
