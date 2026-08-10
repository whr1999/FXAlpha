# FXAlpha Codex Engineering Guardrails

更新时间：2026-05-30

## Before Changing Code

- Start from the real entrypoint: API route, CLI command, MCP tool, GUI action, or scheduled automation.
- Trace into `services/`, then `domain/`, then `storage/paths.py`.
- Prefer updating existing service/domain boundaries over adding a new script.
- If a path changes, update `config.yaml`, `storage/paths.py`, docs, and all callers in the same pass.

## Directory Rules

- `domain/`: FXAlpha business rules only.
- `services/`: API/CLI orchestration only.
- `mcp_servers/`: FXAlpha-owned MCP tools only.
- `external/`: third-party engines, SDK mirrors, and thin adapters.
- `scripts/`: startup and explicit ops tools only.
- `data/`: durable assets only.
- `runtime/`: jobs, sessions, logs, and regenerable state only.

## Do Not Reintroduce

- Do not add a new Prompt Host or pseudo-MCP runner for factor research.
- Do not add a second hand-written model training runner beside the model/direct-Qlib MCP path.
- Do not import heavy external SDKs directly from GUI or API route handlers.
- Do not hard-code `project virtual environment` or `./external/*` outside `storage.paths` or explicit startup docs.
- Do not delete or rewrite factor/model/trading registries without an offline integrity check and a dedicated migration note.

## Validation Minimum

- Python syntax check for touched Python modules.
- `node --check gui/app.js` if GUI is touched.
- Endpoint smoke for the touched module: `/factor/console/live`, `/model/status`, `/trade/status`, or `/data/status`.
- MCP import smoke when touching `.mcp.json`, `mcp_servers/`, QuantGPT, or model tools.
