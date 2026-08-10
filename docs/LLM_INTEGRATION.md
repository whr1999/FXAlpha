# FXalpha LLM 接入决策

更新时间：2026-05-15

## 结论

FXalpha / QuantGPT 研究链的 **运行时 LLM 调用方式**，建议采用：

- **主方案：API**
- **辅方案：MCP**

也就是说：

1. QuantGPT 因子挖掘、多轮优化、cross-review、研究总结等**自动运行链**，统一走 API
2. MCP 只作为：
   - 开发辅助
   - 运维辅助
   - 人工研究辅助
   - GUI 外部智能助手能力

**不要让 QuantGPT 主运行时依赖 MCP。**

## 为什么主运行时要用 API

### 1. 当前代码天然就是 API 形态

本地 QuantGPT 当前 `llm_service.py` 已经是 OpenAI-compatible API 调用形态，直接依赖：

- `DEEPSEEK_API_KEY`
- `DEEPSEEK_BASE_URL`
- `DEEPSEEK_MODEL`

这意味着：

- 接 DeepSeek 最直接
- 后面切 OpenAI / 火山 / 硅基流动 / 其他 OpenAI-compatible 提供方也容易
- 不需要重写 QuantGPT 的主流程

### 2. API 更适合自动化调度

FXalpha 后面要走的是：

- CLI 调度
- HTTP API 调度
- 未来 Web GUI 调度
- 批量任务 / 定时任务 / 后台任务

这些都更适合稳定、可重试、可监控的 API 方式，而不是把运行时绑在 MCP session 上。

### 3. GUI 场景下，HTTP/API 边界更清晰

未来 Web GUI 最自然的链路是：

`GUI -> FXalpha HTTP API -> service -> QuantGPT -> LLM API`

如果把主运行时放到 MCP，会变成：

`GUI -> FXalpha -> MCP session -> Agent tool -> LLM`

这条链更适合“人机协作助手”，不适合“平台后台自动研究任务”。

### 4. API 更利于可观测性与成本控制

运行时要看的通常是：

- 请求次数
- latency
- token 消耗
- provider 错误率
- timeout / retry
- fallback provider

这些都更适合围绕 API 做统一治理。

## MCP 适合做什么

MCP 仍然很有价值，但角色应该更靠外层：

- 让开发代理辅助发起研究任务
- 让研究员用自然语言看研究结果
- 让 GUI 里的智能助手帮用户解释因子与模型
- 做半自动研究，而不是主流水线执行

一句话说：

**MCP 更像控制面，API 更像执行面。**

## 推荐架构

### 当前阶段

- QuantGPT 内核：继续直接走 OpenAI-compatible API
- FXalpha：继续通过 service / CLI / HTTP API 调 QuantGPT
- GUI：未来调 FXalpha HTTP API，不直接碰 LLM provider

### 后续建议

后面可以把 QuantGPT 的 LLM 调用再薄封装一层，形成统一 provider 适配：

- `deepseek`
- `openai`
- `openai-compatible`

但这层应该还是 **API provider abstraction**，不是 MCP abstraction。

## 对当前项目的直接建议

如果你问我“是 MCP 还是给你 API”：

**建议给 API。**

最理想是提供一套稳定的 OpenAI-compatible API 参数：

- `API_KEY`
- `BASE_URL`
- `MODEL`

这样我后面可以继续把：

- factor generation
- iterate refinement
- cross-review
- summary / knowledge write-back

都稳定接在同一套运行时上。

## 当前代码事实

当前 QuantGPT 的 LLM 实现位于固定子模块路径：

- `third_party/quantgpt/quantgpt/llm_service.py`

## 配置真源

现在 LLM 配置真源不再依赖 OpenClaw 配置文件，而是统一收口在：

- `./config.yaml`

其中当前研究链使用：

- `llm.quant_research.provider`
- `llm.quant_research.api_key`
- `llm.quant_research.base_url`
- `llm.quant_research.model`
- `llm.quant_research.cross_review_model`

这样 QuantGPT 和 model 都可以共用同一份 FXalpha 配置。

所以短期最省事、最稳的路径就是：

**保留 API 路线，不把主研究链改造成 MCP 依赖。**
