# Local deployment

## Prerequisites

- Linux or WSL2
- Python 3.11 or 3.12
- Git with submodule support
- sufficient local disk and memory for the selected data/model workload

## Installation

```bash
git clone --recurse-submodules https://github.com/whr1999/FXAlpha.git
cd FXAlpha
cp config.example.yaml config.yaml
./scripts/bootstrap_public_env.sh
```

Populate credentials locally. Never commit `config.yaml` or `.env` files.
An unconfigured development clone can import and inspect the platform with the
non-secret defaults in `config.example.yaml`. State-changing provider workflows
still require explicit credentials. If `FXALPHA_CONFIG_FILE` is set, that file
must exist; the example fallback is not used for a missing explicit path.

## Development start

```bash
PYTHONPATH=. .venv/bin/python api_server.py --host 127.0.0.1 --port 18081
```

Open `http://127.0.0.1:18081/`. The GUI is served by the same process.

## User service templates

Templates under `deploy/systemd/` use `%h/FXAlpha`, the repository `.venv`, and
an optional protected environment file at `%h/.config/fxalpha/fxalpha.env`.
Review paths and limits before installing them. The repository does not install,
enable, restart, or replace production services automatically.

Example preparation:

```bash
install -d -m 700 "$HOME/.config/fxalpha"
install -m 600 .env.example "$HOME/.config/fxalpha/fxalpha.env"
install -m 644 deploy/systemd/fxalpha-api-18081.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
```

Starting or switching a service is intentionally a separate operator decision.

## Immutable release layout

For a long-lived production host, do not run services from a mutable development
checkout. Use a versioned release directory and an atomic `current` symlink:

```text
~/fxalpha-deploy/
  current -> releases/<git-commit>/
  releases/<git-commit>/
~/fxalpha-data/
  raw/ qlib/ quantgpt/ factors/ model/ trading/ metadata/
~/.config/fxalpha/
  config.yaml
  runtime.env
~/fxalpha-state/
  runtime/
  quantgpt/
```

Templates in `deploy/systemd/release/` implement this layout. `runtime.env` is
required and must define the absolute `FXALPHA_CONFIG_FILE`; copy
`runtime.env.example`, replace `USER`, and set mode `0600`. The external config
must set `paths.data_root` to `~/fxalpha-data`, `paths.runtime_root` to
`~/fxalpha-state/runtime` (both expanded to absolute paths), and
`paths.quantgpt_db` to the external task ledger. `DATABASE_URL` in
`runtime.env` must select the same QuantGPT database file. Fine-grained path
overrides are supported for legacy migration but are not required for a new
standard layout. See [`PATH_LAYOUT.md`](PATH_LAYOUT.md).

Install the four release services, the stack target, and both timer files as
one versioned unit set. Enable the timers only after the API, QuantGPT, and all
business-lane checks pass. A release is incomplete if its services are updated
while an older checkout still owns either timer definition.

Create a new release without changing the live symlink, build its `.venv`, run
tests, then start shadow processes on QuantGPT `8004` and API `18082` with an
independent runtime and copied SQLite state. Only after every lane is ready may
the operator stop timers/services, atomically repoint `current`, install the
release templates, and restart. Retain the previous release and unit files for
rollback. Do not delete the old release during the cutover window.

## Configuration and state

- Set `FXALPHA_CONFIG_FILE` to use config outside the checkout.
- Background data, factor-scoring, import, and Orchestrator workers launched as
  transient user services receive the allowlisted values from `runtime.env`,
  including `FXALPHA_CONFIG_FILE`. Restart the long-lived service after changing
  that file; do not put a second `config.yaml` inside an immutable release.
- Set `paths.data_root` once to move raw HDF/metadata/calendar, Qlib, QuantGPT
  parquet, factor, model, metadata, and trading together. If a legacy config
  also contains fine-grained overrides, they take precedence and must be
  reviewed as a complete set.
- Dataset manifests retain portable repository-relative canonical paths. The
  runtime resolves the Tushare quality report next to the configured production
  HDF when that data tree is externally mounted.
- Back up `data/` and database files with application-consistent procedures.
- Treat `runtime/` as governed evidence/state; do not delete locks or active run
  directories manually.
- Data and model artifacts are not downloadable from this source repository.

## Verification

```bash
.venv/bin/python scripts/audit_public_repo.py
.venv/bin/python -m pytest
.venv/bin/python -m pip check
```

The publication audit must pass before a push. Production cutover additionally
requires a fresh backup, config diff, candidate service on a separate port,
readiness checks for every lane, and an explicit rollback rehearsal.

The validated blue/green pattern uses 8004 for a shadow QuantGPT process and
18082 for a shadow FXAlpha API, independent runtime directories, copied SQLite
registries, and read-only access to large production data. Do not point a
shadow service at writable production registries.

`GET /maintenance/status` is snapshot-first. Use
`GET /maintenance/status?deep=true` only for an explicit disk audit; it may be
slow on a workspace containing large staging and backup trees.
