# Model MCP Operating Prompt

模型研究有两种显式模式：

- `research`：固定因子集的单时间段研究。每轮只训练 Seed 42，按研究评分迭代；会话结束后只给最优轮补 Seed 17/83 确认。没有 forward test，也不会产生 candidate。
- `production`：只能接收已通过三 Seed 研究确认的 round；固定参数执行四折 expanding Rolling。先跑 Seed 42，初筛通过后再跑 17/83。DeepSeek 不参与 Rolling。

研究主链：

`context -> protocol -> preflight -> feature_snapshot -> session_start -> submit_experiment -> run_round(seed42) -> research_score -> round_synthesis -> confirm_research_round(session_best)`

生产主链：

`start_production_rolling(source_round_group_id) -> seed42 four folds -> preliminary score -> seed17/83 four folds -> formal rolling score -> candidate gate`

固定契约：不可变 feature snapshot；LABEL0 为 adjusted open T+1 到 T+6；固定 processor 与 sample weight；唯一组合为 Top20/Drop2/Hold5，open 成交，000300sh 基准，封板开盘限价表达式不变；不得预移 pred.pkl。

资产状态只有 `research / candidate / production / archived`：一套参数只登记一个正式 Seed42 research；Seed17/83 只写确认审计，不作为模型。只有三 Seed 四折 Rolling 准入分和稳定性门槛都通过的 campaign 记录才是 candidate，其正式表现仍取 Seed42；production 由 candidate 以固定 Seed42 refit。只有数据泄漏、窗口/标签错误、训练失败、预测无效或产物损坏才 archived。不得挑选历史最好 Seed。

DeepSeek 只接收 experiment_plan 所需的 Seed 42 研究证据和真实训练诊断，不接收固定契约、forward 结果或生产 Rolling 决策。
