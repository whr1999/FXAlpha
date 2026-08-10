# GitHub publication verification report — 2026-08-10

## Candidate

- public branch: `codex/github-publication-final`
- intended mapping: candidate `HEAD` to GitHub `refs/heads/main`
- history shape: one root commit with no construction-history parent
- QuantGPT pin: `024818abcf76b35f0a8282f9a212c2309716defd`
- Qlib pin: `d5379c520f66a39953bad76234a7019a72796fd0`
- Tushare 1.4.29 pin: `bc5388dcb339ce7e11515cab5cb6087b3724e74b`

The public candidate is a separate checkout. This final audit did not edit the
deployed source tree, services, databases, data, model artifacts, factor values,
account state, or vn.py environment.

## Final local release gate

Command:

```bash
python scripts/run_release_preflight.py
```

Result: **passed**.

| Gate | Result |
| --- | --- |
| public-tree audit | passed; 328 paths and three submodules; zero violations |
| Git-history audit | passed; one commit and 321 blobs; zero violations |
| local publication topology | passed; all three Gitlinks match the lock file |
| Python source compilation | passed |
| regression suite | **1022 passed, 1 skipped in 39.38 seconds** |

The skipped test is an optional check against a private production feature
snapshot. A public clone has no such snapshot, so the test now requires an
explicit external `FXALPHA_SANDBOX_FEATURE_SET_ID` and otherwise skips with a
clear reason.

## Disclosure and material audit

The public tree contains source, tests, current contracts/runbooks, sanitized
examples, one architecture SVG, four owner-approved UI screenshots, and pinned
third-party Gitlinks. It does not contain API keys, tokens, real configuration,
market datasets, machine-readable factor values, model files, registries,
account databases, predictions, logs, traces, backups, or runtime directories.

The final pass removed private construction material that was not needed to use
the platform:

- production prompt/canary and chronological factor-engineering logs;
- implementation reports containing factor/model/run identifiers or account
  values;
- one-off model diagnostic scripts hard-coded to private feature snapshots;
- deployment-specific cleanup counts, disk totals, and runtime report paths.

The history gate now rejects production-shaped factor IDs, factor run IDs,
model IDs, production model run IDs, and legacy feature-snapshot IDs in
addition to credential patterns, personal paths, private file types, and blobs
above 5 MiB. Construction history was replaced by a clean root commit so
removed material is not reachable from the branch intended for public `main`.

## Screenshot decision

The project owner explicitly approved the four original runtime captures for
public documentation. Manual review found no API key, token, login credential,
local filesystem path, EXIF record, comment, or device metadata. The images are
described as point-in-time documentary records, not sanitized demo data or
performance evidence.

[`assets/screenshots/manifest.json`](assets/screenshots/manifest.json) pins the
path, dimensions, byte size, and SHA-256 of every screenshot. The public-tree
audit fails if a screenshot is added without review, removed without updating
the manifest, or changed byte-for-byte without a new recorded hash. The largest
reachable blob is the 2,363,546-byte model-research screenshot, below the 5 MiB
history limit.

## Third-party and network gates

Local topology passed for QuantGPT, Qlib, and Tushare. Network evidence from the
workstation is intermittent. An anonymous `git ls-remote` against the official
Qlib repository succeeded, and anonymous GitHub API requests for these intended
public targets returned HTTP 404:

- `whr1999/QuantGPT`
- `whr1999/qlib`
- `whr1999/tushare`
- `whr1999/FXAlpha`

A later `verify_publication_topology.py --release` attempt hit
`gnutls_handshake` failures for all four Git URLs. The gate therefore cannot
yet distinguish repository reachability on every attempt, but the successful
API response already proves the four named repositories were unavailable to an
anonymous caller when the network worked. Release requires both: create or make
the repositories public and populate the recorded commits, then obtain one
uninterrupted anonymous component and recursive-clone pass.

## Remaining human decisions and release limits

1. Decide whether the first public release remains source-visible under the
   current all-rights-reserved terms or adopts an open-source license.
2. Create and publish the three real forks in lock-file order, then rerun the
   anonymous component gate.
3. Create the empty public main repository, push only
   `codex/github-publication-final:main`, and never use `git push --all`.
4. Enable GitHub security and branch-protection settings, wait for CI/CodeQL,
   and pass the network-fresh recursive-clone gate before tagging `v0.1.0`.

No production cutover, data promotion, factor import, model training, paper
trade, remote repository creation, push, or release tag was performed by this
audit. Exact commands and stop conditions are in
[`GITHUB_UPLOAD_RUNBOOK.md`](GITHUB_UPLOAD_RUNBOOK.md).
