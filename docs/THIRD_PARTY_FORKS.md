# Third-party fork policy

FXAlpha tracks third-party components as Git submodules. The main repository
never vendors or silently edits their source.

| Component | Official upstream | FXAlpha fork target | Pin in this candidate | Purpose |
| --- | --- | --- | --- | --- |
| QuantGPT | `Miasyster/QuantGPT` | `whr1999/QuantGPT` | `024818abcf76b35f0a8282f9a212c2309716defd` | Factor engine and MCP integration |
| Qlib | `microsoft/qlib` | `whr1999/qlib` | `d5379c520f66a39953bad76234a7019a72796fd0` | Training, backtest, exchange semantics |
| Tushare | `waditu/tushare` | `whr1999/tushare` | `bc5388dcb339ce7e11515cab5cb6087b3724e74b` | Data SDK |

The machine-readable copy of these pins, upstream bases, fork URLs, and release
blockers is [`third_party/components.lock.json`](../third_party/components.lock.json).

## Fork lifecycle

1. Create a real GitHub fork from the official upstream.
2. Preserve an `upstream` remote pointing to the official repository and an
   `origin` remote pointing to `whr1999`.
3. Put FXAlpha changes on `codex/fxalpha-public`; do not rewrite upstream tags.
4. Run component tests, license review, secret scan, and dependency audit.
5. Push the fork branch, then update the main repository's submodule pin in a
   separate reviewed commit.
6. Record upstream base, local commit, test result, and migration notes.

Dependabot does not update these submodules. Its generic Git-submodule updater
follows a fork's default branch and cannot preserve the reviewed FXAlpha
integration-branch contract. Submodule updates therefore remain manual,
reviewed changes that must update both the Gitlink and
`third_party/components.lock.json` together.

## Current local candidates

- QuantGPT is a local clone of the deployed integration with secure defaults,
  portable FXAlpha root/config discovery, loopback binding, restricted
  development CORS, and no operator-specific absolute paths.
- Qlib is an unchanged pinned upstream checkout.
- Tushare is a byte-verified overlay of all 74 source files from the official
  `tushare-1.4.29-py3-none-any.whl` release. The wheel SHA-256 is
  `82554af953ea5ac3d8771d42330493181031c7e68dccce03a491c7356e9ba4b2`.
  The upstream repository does not advertise a matching `1.4.29` tag, and the
  local staging commit does not share upstream Git ancestry. This lineage gap is
  accepted for the first public release because the package payload, metadata,
  wheel hash, and license were verified independently. Create a real GitHub
  fork of `waditu/tushare`, publish the verified commit on the FXAlpha-owned
  branch, and preserve this provenance record. Official ancestry is preferred
  for a later refresh but is not a publication blocker.

Run the local topology check after any pin change:

```bash
python scripts/verify_publication_topology.py
```

Before the main upload, require public component reachability:

```bash
python scripts/verify_publication_topology.py --components-only
```

After the main upload, require a fresh unauthenticated recursive clone:

```bash
python scripts/verify_publication_topology.py --release
```

## Updating a fork

```bash
git fetch upstream --tags
git checkout codex/fxalpha-public
git rebase upstream/main
python -m pytest
git push --force-with-lease origin codex/fxalpha-public
```

Use the upstream repository's actual default branch if it is not `main`.
Force-push only the FXAlpha-owned integration branch, never an upstream branch.
