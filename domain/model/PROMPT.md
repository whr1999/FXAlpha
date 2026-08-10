# Model Prompt Index

Model uses two separate prompt assets.

- `ORCH_PROMPT.md`: DeepSeek Flash research-planner system prompt. It receives
  only Seed 42 research evidence and real training diagnostics, then emits a
  constrained parameter-change plan. It never decides Seed confirmation or
  production Rolling.
- `MCP_PROMPT.md`: operating prompt for Codex/MCP mode. It describes the tool
  research/production mode switch, staged Seed policy, Rolling admission,
  status language and registry rules.

Do not merge these roles. ORCH mode should focus on research judgment and
parameter planning. MCP mode should focus on correct workflow execution and
state discipline.
