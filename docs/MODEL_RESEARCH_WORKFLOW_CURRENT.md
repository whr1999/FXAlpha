# FXAlpha 模型研究与生产 Rolling 工作流

更新：2026-08-01

模型模块只有两种显式评测模式，共用不可变 feature set、Qlib 0.6.27 执行器和 Top20/Drop2/Hold5 交易契约，但证据用途不同。

Research 单次测试默认窗口为：train `2022-01-04 ~ 2024-12-31`、valid
`2025-01-02 ~ 2025-06-30`、test `2025-07-01 ~ 2026-07-01`。普通 ORCH 和
DeepSeek 参数研究继承该默认值；操作员明确指定 experiment segments 时允许覆盖。
Production 继续使用独立的四折 expanding Rolling 窗口。

| 模式 | 参数研究 | Seed | 时间结构 | 输出状态 |
| --- | --- | --- | --- | --- |
| research | DeepSeek Flash 每轮提出一个参数组的 1–3 项变化 | 普通轮只跑 42；会话最优轮再跑 17/83 | 单一 train/valid/test | research |
| production | 不调用 DeepSeek，不调参 | 先 42 四折；初筛通过再跑 17/83 四折 | 四个 expanding、每折 6 个月 valid + 6 个月 test，按标签边界 purge | candidate 或保持 research |

研究链：`context_review -> experiment_plan -> train_backtest_seed42 -> research_score -> round_synthesis`。Seed 42 研究评分为 IR 40% + 成本后超额收益 30% + 最大回撤 20% + RankIC/RankICIR 10%。当前最优至少提高 1 分才重置未改善计数，连续三轮无改善平台停止。停止后只确认会话最优轮；不得按最好 Seed 选模型。

研究确认门槛：三 Seed 产物完整、至少 2/3 IR 为正、最差 Seed 回撤不超过 30%、IR 标准差不超过 0.60、收益标准差不超过 20 个百分点，且三 Seed 中位研究评分相对 Seed 42 不下降超过 10 分。通过只表示可进入生产 Rolling，状态仍为 research。模型库只登记并展示正式 Seed42；Seed17/83 只保留在状态库、产物和确认审计中，不作为独立模型、排名对象或回测选择项。

生产 Rolling 先跑 Seed 42 的四折并计算 `55% overall + 25% worst fold + 20% latest fold`。初筛分至少 60 且折级、最新折、回撤和可靠性门槛通过，才补 Seed 17/83。三 Seed 聚合结果在业务和 GUI 中称为“Rolling 准入分”；分数至少 70，并通过 Seed 离散度、至少三折正 IR 和最新折正 IR 等门槛，才写入一个 campaign candidate。Candidate 的正式收益、IR、回撤和回测曲线固定使用 Seed42，Seed17/83 只参与准入审计。

状态只有 `research / candidate / production / archived`。candidate 不是最好 Seed，而是通过多 Seed 审计的一套参数；正式展示与 production refit 都固定 Seed42。archived 只用于泄漏、标签或窗口错误、训练失败、预测无效、产物损坏或人工退役。

## 启动、停止与恢复

MCP `fxalpha_model_orchestrator_start` 与 HTTP
`POST /model/orchestrator/start` 是同一正式 ORCH 主链。HTTP 调用只负责受理任务，
返回 `accepted` 后由独立 worker 执行；任务是否真的训练、训练到哪一步，只能从
`GET /model/status`、状态库与产物判断，不能用接口已返回或服务在线代替。

平台全局只允许一个受管模型任务。`POST /model/jobs/stop` 是协作式停止：Research
完成当前 round 后停止，Rolling 完成当前 seed 边界后停止。被标记为
`interrupted / failed` 的任务可由 `POST /model/jobs/resume` 恢复同一 session；
已完成的 Research round、Rolling seed 与 fold 会被复用，不重复计算。

Research 未显式指定 `feature_set_id` 时，worker 在后台冻结当前 active values。
快照按内容去重：相同因子、日期、标签与策略合同复用原不可变 snapshot；内容变化才生成
新 snapshot。注册表同时保存 `feature_set_id`、fingerprint、factor ids/count、feature
count 和训练窗口，避免只凭名称判断模型来源。

注册表的 `models.status` 是唯一状态权威；metadata 中的 `asset_status` 只是同步
投影，不得反向覆盖主状态。历史模型可以保留 `legacy_sota_score` 供审计，但新
研究排序和 DeepSeek 调参只使用 Seed42 `research_score`。`confirmed_research_score`
是研究稳定性审计参考分，`rolling_score` 是 Rolling 准入分；二者不得替代 Seed42
正式表现或用于选择最高 Seed。

核心代码：`scoring.py`、`research_confirmation.py`、`walk_forward.py`、`rolling_scoring.py`、`orchestrator.py`。旧 `forward_test.py` 仅为历史导入兼容，协议明确禁用，不在 MCP、API 或 orchestrator 主链中。

Production refit 的 train/valid 窗口按来源 feature snapshot 的最新交易日动态锚定，
保持既有训练/验证长度并保留标签 purge。晋升成功后原子更新
`runtime/model/active_production_model.json`，保存当前模型和上一版引用；预测默认消费
该指针。没有 production 时指针不存在是正常状态，不允许用 research 或 candidate
自动顶替。

旧 research journal 不改写。读取接口会把含 SOTA/forward 旧契约的记录投影为
`record_era=historical_pre_dual_mode`、`current_contract=false`；GUI 只在明确的
“历史旧流程”区域展示这些原文，不把它们作为当前研究判断。
