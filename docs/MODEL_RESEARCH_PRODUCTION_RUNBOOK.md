# FXAlpha 模型生产运行手册

更新：2026-08-01

入口：MCP `python3 -m mcp_servers.model_server --transport stdio`；HTTP 使用 `/model/*`；所有业务实现位于 `domain/model/`。

## 正式启动与控制

HTTP 正式入口为 `POST /model/orchestrator/start`。它是异步入口，成功只表示任务已
受理并返回 `status=accepted`，不表示 Qlib 已完成训练。平台全局只运行一个受管模型
任务；若已有活动任务，响应为 `status=already_running` 并返回该任务。

Research 示例：

```json
{
  "evaluation_mode": "research",
  "n_rounds": 3,
  "execute_qlib": true,
  "write_registry": true
}
```

省略 `feature_set_id` 时，worker 会先冻结当前 active values；如果内容合同与已有不可变
快照一致，则复用已有快照。启动后的唯一进度真相是 `GET /model/status`。worker 日志
位于 `runtime/model/jobs/<job_id>.log`。

控制接口：

- `POST /model/jobs/stop`：协作式停止，Research 在当前 round 结束后停，Rolling 在
  当前 seed 边界停。
- `POST /model/jobs/resume`：请求体传原 `job_id`，只恢复 `interrupted / failed`
  任务，并沿用原 session 和已完成证据。

不要在停止响应刚返回时启动第二个任务；应等待状态中的活动任务真正退出。

研究模式调用 `fxalpha_model_orchestrator_start(evaluation_mode="research", feature_set_id=..., n_rounds=..., execute_qlib=true, write_registry=true)`。普通轮只训练 Seed 42；DeepSeek 只看到正式 Seed42 研究评分、回测指标、参数变化和真实 LightGBM 训练诊断。会话结束时 orchestrator 自动对最优轮补 Seed 17/83，但它们只写审计证据，模型库仍只登记一个 Seed42 research。也可对已完成的最优轮显式调用 `fxalpha_model_confirm_research_round`。

Research 的单次默认窗口为 train `2022-01-04 ~ 2024-12-31`、valid
`2025-01-02 ~ 2025-06-30`、test `2025-07-01 ~ 2026-07-01`。普通研究自动继承；
操作员明确提交其他 segments 时允许覆盖。日期会在真实执行时按交易日和标签退出
边界做必要对齐。

生产模式调用 `fxalpha_model_orchestrator_start(evaluation_mode="production", source_round_group_id=..., write_registry=true)`，或 `fxalpha_model_start_production_rolling`。source round 必须已有 `research_confirmation.status=passed`。生产 Rolling 固定源 round 参数，四折窗口由 feature manifest 的结束日期和交易日历动态生成；每折标签边界按 manifest 的实际 exit shift purge。Rolling 中不调用 DeepSeek。

正式组合始终为 Top20/Drop2/Hold5、000300sh 基准、open 成交和既定开盘封板限价表达式。不得在研究或 Rolling 中测试 Top10/Top30、第二套基线、forward test 或最好 Seed 选择。

promotion 只接受带 `evaluation_mode=production`、`rolling_campaign_id` 和 `source_round_group_id` 的 candidate 聚合记录；refit 固定 Seed 42。先使用 dry-run 预览，再执行真实 Qlib refit。refit 的窗口按来源 feature snapshot 的最新交易日动态锚定，不复用过期的固定截止日。

真相源：feature sets 在 `data/model/features/feature_sets/`；注册表在 `data/model/model_registry.db`；round/session 在 `runtime/model/jobs.sqlite`；任务日志在 `runtime/model/jobs/`；训练产物在 `runtime/model/runs/`；Rolling campaign 在 `runtime/model/rolling/`；研究日志和 DeepSeek trace 在 `runtime/model/research_steps/` 与 `runtime/model/orchestrator_traces/`。production refit 成功后，当前生产模型指针位于 `runtime/model/active_production_model.json`，预测默认读取它；首次晋升前文件不存在属于正常状态。

模型库以数据库 `models.status` 为唯一生命周期权威，metadata 的
`asset_status` 必须与其一致。当前状态只允许 `research / candidate /
production / archived`；历史分数不得冒充新 `research_score`。在第一次正式
Rolling 通过之前，`candidate=0` 和 `production=0` 是正确状态，不是故障。

真实测试前必须完成 `MODEL_RESEARCH_PRETEST_CHECKLIST_CURRENT.md`。GUI 应准确
展示 Research、Rolling Candidate、Production 三类数量；不得把 research 总数
回退显示成 candidate，也不得出现 Forward 或 SOTA 入口。模型回测的人工选择范围
包括全部正式 Seed42 `research + candidate`，production 另设快捷入口；Seed17/83
不作为模型选项。research 模型可查看其单次研究回测，但不会因此获得 candidate 身份。生产 Rolling 页面直接读取
`runtime/model/rolling/*/campaign.json`，展示拼接指标、四折明细、六项初筛门槛、
已执行 Seed 和停止原因。该完整结论不得占用“研究现场”，只在“回测结果”中选择
对应 Rolling campaign 后显示。Rolling 正式曲线、四折明细、逐日持仓和主收益指标
固定使用 Seed42；17/83 只在折叠的稳定性审计表中显示。“研究现场”的流程区只展示两条业务链路：研究准备、
参数实验、Seed42 首轮评估、优胜轮三 Seed 确认、Research 资产登记；以及 Seed42
四折 Rolling 初筛、Seed17/83 Rolling 复核、Candidate 准入、Production 固定 Seed42
发布。生产链路在现场只显示 `已完成 / 进行中 / 未通过 / 未执行 / 等待`，点击
“查看 Rolling 详情”后切换到回测结果。已结束 job/session 只能标为“最近记录”，
不得显示成活动任务。

HTTP `/model/backtest` 的历史 `rolling_seed` 查询参数仅保留兼容，正式回测会忽略
非42取值并固定返回 Seed42。GUI 不发送该参数，也不提供 Seed17/83 曲线选择入口。
