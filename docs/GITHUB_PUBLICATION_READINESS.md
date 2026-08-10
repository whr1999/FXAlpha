# GitHub publication readiness

Audit date: 2026-08-10

## Scope and safety boundary

The work was performed in a private staging workspace outside the deployed
checkout. The deployed source directory, user services, production
databases, Qlib environment, data, runtime state, and `.vntrader` directory were
not switched or edited.

## Phase 1 — freeze and inventory

Status: complete.

- froze the source/runtime dependency baseline
- copied the trading database and current paper-fleet status into a private,
  permission-restricted audit directory outside the repository
- archived local vn.py state and recorded SHA-256 checksums
- recorded the deployed Python package set
- classified source, data, runtime, secrets, artifacts, and third-party trees

## Phase 2 — public candidate construction

Status: complete.

- copied source into a new Git working tree
- excluded data, runtime, databases, artifacts, logs, virtual environments,
  caches, local configuration, secrets, and historical private archives
- generated sanitized configuration examples
- removed personal absolute paths from tracked current material
- added project/security/contribution notices and a publication-focused README
- made configuration portable through `FXALPHA_CONFIG_FILE`

## Phase 3 — dependency and execution cleanup

Status: complete locally; external fork creation remains.

- converted QuantGPT, Qlib, and Tushare to pinned submodules
- prepared local fork branches and secure QuantGPT defaults
- retired vn.py application/runtime contracts from the candidate
- retained Qlib paper fleet/replay behavior
- documented upstream/fork ownership and license boundaries

## Phase 4 — verification and release gates

Status: complete locally; see
[`VERIFICATION_REPORT_20260810.md`](VERIFICATION_REPORT_20260810.md).

- repository-hygiene and secret-pattern audit
- Python compile/import checks
- regression suite in a copied environment with vn.py packages removed
- GitHub Actions CI, CodeQL, and dependency-update configuration
- clean-clone/submodule and documentation-link checks
- exact publication and production-cutover runbooks
- production-asset shadow validation on isolated ports and copied registries
- external-data path contract regression for quality reports and metadata cache
- bilingual business-workflow and formula contracts routed from both READMEs,
  both user guides, the architecture guide, and the current documentation index
- four owner-approved runtime UI captures reviewed for credentials and metadata,
  recorded in a hash manifest, and routed through bilingual screenshot guides

## External actions still required

These actions mutate GitHub and were deliberately not performed by local audit:

1. Create `whr1999/QuantGPT`, `whr1999/qlib`, and `whr1999/tushare` as real
   GitHub forks of their official upstreams.
2. Publish the byte-verified Tushare 1.4.29 candidate on an FXAlpha-owned branch
   of the real GitHub fork; disconnected commit ancestry is accepted for this
   release and recorded in the fork policy.
3. Push each `codex/fxalpha-public` branch and confirm the recorded commits are
   reachable from its public remote.
4. Create `whr1999/FXAlpha`, enable secret scanning, push protection, Dependabot,
   CodeQL, and branch protection.
5. Push the tested `codex/github-publication-final` HEAD as public `main`; verify
   submodules initialize in a fresh unauthenticated clone and CI passes.
6. Decide whether to retain all-rights-reserved terms or adopt an open-source
   license before inviting contributions.

Current automated preflight state: local topology passed for all three
components with no local release blockers. Network evidence on 2026-08-10 was
intermittent: an anonymous fetch of official Qlib succeeded and the four target
repository API requests returned 404, but a later complete release gate hit
`gnutls_handshake` failures for all four Git URLs. Public-pin and recursive-clone
gates therefore remain closed until the repositories are created or made public,
populated, and one uninterrupted anonymous release check passes.

The final commands are fail-closed gates:

```bash
python scripts/audit_public_repo.py
python scripts/audit_git_history.py
python scripts/verify_publication_topology.py --release
```

The topology command verifies each public fork pin and clones the main
repository recursively from GitHub. It must not be replaced by a local-path
clone.

## Publication order

```text
QuantGPT fork -> Qlib fork -> real Tushare fork -> verify public pins
              -> create FXAlpha repo -> push tested HEAD as main -> CI
              -> network-fresh acceptance -> release tag
```

Do not publish the main repository before all three submodule commits are
publicly reachable; otherwise a recursive clone will fail.

The command-level handoff is in
[`GITHUB_UPLOAD_RUNBOOK.md`](GITHUB_UPLOAD_RUNBOOK.md).

## Production cutover (separate change)

Publication does not authorize production migration. A later cutover must:

1. refresh database/config/runtime backups and checksums
2. build a fresh environment from the public pins
3. start the candidate on a different loopback port
4. use copied registries, independent runtime, and read-only large-data mounts
5. compare data, factor, model, prediction, and paper-fleet health independently
6. stop the old unit only after acceptance; switch unit paths atomically
7. keep the previous workspace/environment and rollback instructions intact
8. observe at least one full scheduled cycle before cleanup

No production data migration, systemd switch, package uninstall, or `.vntrader`
deletion was performed during phases 1–4.
