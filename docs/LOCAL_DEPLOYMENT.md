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

## Configuration and state

- Set `FXALPHA_CONFIG_FILE` to use config outside the checkout.
- If durable data lives outside the release checkout, map the complete path
  set together: raw HDF/metadata/calendar, Qlib, QuantGPT parquet, factor,
  model, `metadata_root`, and trading. Mapping only the large datasets but not
  `metadata_root` can make a status request rebuild the stock-identity cache.
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
