# Current project structure

The public repository separates application source, pinned third-party source,
operator-owned data, and runtime evidence.

| Path | Purpose |
| --- | --- |
| `api_server.py`, `cli.py` | HTTP/GUI and CLI surfaces |
| `domain/data_foundation/` | Tushare staging, validation, promotion |
| `domain/factor_research/` | Factor orchestration, gate, import contracts |
| `domain/model/` | Snapshot, training, rolling evaluation, promotion |
| `domain/trading/` | Prediction, recommendation, Qlib paper trading |
| `services/` | Shared application services used by all surfaces |
| `storage/` | Path and registry ownership |
| `integrations/tushare/` | FXAlpha-owned Tushare orchestration helpers |
| `third_party/quantgpt/` | QuantGPT Git submodule |
| `third_party/qlib/` | Qlib Git submodule |
| `third_party/tushare/` | Tushare Git submodule |
| `mcp_servers/` | FXAlpha MCP entrypoints |
| `gui/` | Static operator console |
| `tests/` | source-contract and regression tests |
| `deploy/systemd/` | operator-reviewed user-service templates |
| `docs/` | current public product, workflow, architecture, and operations contracts |

Generated `data/`, `runtime/`, `artifacts/`, `mlruns/`, logs, databases, local
configuration, and virtual environments are excluded from Git. vn.py has no
source or runtime ownership in the current structure.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for system flow and
[`THIRD_PARTY_FORKS.md`](THIRD_PARTY_FORKS.md) for dependency ownership. The
host-level separation of immutable releases, durable data, runtime evidence,
configuration, backups, and archives is normative in
[`PATH_LAYOUT.md`](PATH_LAYOUT.md).
