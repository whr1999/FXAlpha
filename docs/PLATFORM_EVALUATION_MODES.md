# FXAlpha 平台评估模式合同

## 当前实施边界

平台有两个互斥的 `evaluation_mode`：

- `research`：因子发现使用较早的选择窗口，保留后续模型研究的干净留出证据，`evidence_class=clean_holdout`。
- `production`：因子发现顶到 profile 定义的最新完整周期，强调时效性，`evidence_class=discovery_conditioned_rolling`。

这与 `orchestration_mode` 完全不同。`orchestration_mode` 只表示因子研究由 `orchestrator` 还是人工监督的 `codex_mcp` 控制；`evaluation_mode` 表示证据语义、日期窗口和后续资产归属。

因子研究已经消费双模式 profile。模型模块也已经具备自己的显式
`research / production` 评测链：production 只接受研究确认通过的来源 round，执行正式
四折 Rolling，并在 candidate 晋升后写入生产模型指针。这里的模型
`evaluation_mode=production` 仍需由模型入口显式启动，不会因为平台默认 profile 切换而
自动触发；GUI 不得把普通研究回测包装成生产 Rolling。

## 真相源与解析顺序

1. `config.yaml -> platform_evaluation.profiles` 定义两个 profile。
2. `runtime/platform/evaluation_mode.json` 只保存“新任务默认使用哪个模式”。文件不存在时使用配置中的 `default_mode`。
3. `domain/platform_evaluation.py` 解析并校验 profile，生成稳定的 `config_snapshot_hash`。
4. 新因子任务把完整 profile 快照写入 `inputs`、ORCH `research_contract` 或 MCP `research_step.extra.research_contract`。
5. 运行中与已完成任务始终显示自己的任务快照。切换平台默认模式不能追溯修改它们；恢复任务继续使用原快照。

GET 状态接口不得顺便创建或修复 state 文件。只有显式 POST 切换才允许写入状态。

## API

```text
GET  /platform/evaluation-profile
POST /platform/evaluation-profile
```

POST 请求只需要 `evaluation_mode`，可选 `changed_by`。它只改变未来新任务的默认模式。

```text
POST /factor/research/start
```

启动请求携带 `evaluation_mode`。服务端不信任页面中的日期文本，而是重新解析该 mode 的 profile，并把解析后的选择窗口、因子值窗口、profile version、evidence class 与 hash 固化到任务。

## GUI 语义

全局模式条同时显示：

- 平台默认模式；
- 解析后的因子选择窗口和因子值窗口；
- 当前运行中因子任务的实际模式；
- 模型侧在本阶段是否已经消费该 profile。

日期字段是 profile 投影，只读且不能通过“保存默认参数”修改。若平台默认模式与当前任务模式不同，这是合法状态，表示用户已经为下一任务切换模式；当前任务不会受到影响。

## 资产边界

双模式第一阶段不拆分 research/production 因子库 membership；两种模式继续共享同一套经过统一复核的 active/retired 生命周期状态。全库重算、恢复或退役属于统一因子治理，不由模式切换自动触发。模型生产指针已经实现为
`runtime/model/active_production_model.json`；仍未完成的是 mode-scoped factor evidence
与 mode-scoped active feature membership。因而可以声称“模型正式 Rolling 与生产模型
选择已落地”，但不能声称因子到模型的整条链已经按平台 profile 完全隔离。

## 验收原则

- 同一配置下两个 profile 解析结果稳定且 hash 可复现。
- 非法模式不写 state。
- GET 为只读；POST 原子更新状态。
- 新任务记录 `evaluation_mode/profile_version/config_snapshot_hash/evidence_class` 和完整窗口快照。
- 恢复任务沿用原快照。
- GUI 切换后新任务日期随 profile 改变，运行中任务模式保持不变。
- 平台 profile 切换不自动启动或改写既有模型任务；模型 Research/production Rolling
  仍由模型正式入口和固化快照决定。
