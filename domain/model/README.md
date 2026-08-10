# model

更新：2026-08-01

当前模型模块支持 research 与 production 两条明确主链。

- Research：`orchestrator.py` 调用 DeepSeek Flash 产生参数变化；`qlib_runner.py` 普通轮只执行 Seed 42；`scoring.py` 计算正式 Seed42 研究评分；`research_confirmation.py` 只给会话最优轮补 17/83 做审计，模型库仍只登记一个 Seed42 research 资产。
- Production：`walk_forward.py` 固定研究优胜轮参数，先 Seed 42、后按需 17/83 执行四折 expanding Rolling；`rolling_scoring.py` 负责初筛和稳定性准入评分。通过后只创建一个 campaign candidate，候选的正式收益指标取 Seed42，17/83 只保留为准入审计。
- Promotion：`production_refit.py` 只接受正式 Rolling candidate，固定 Seed 42 refit 为 production。

固定组合为 Top20/Drop2/Hold5。旧 forward test 与逐 Seed SOTA Gate 不在现行 MCP、API、orchestrator 和 candidate 入口中。资产状态为 `research / candidate / production / archived`。

Seed 展示合同：正式 Research、Rolling 曲线和 Production 全部固定 Seed42；Seed17/83 不作为顶层模型、不进入正式排名、不允许择优替代 Seed42。三 Seed 聚合结果只作为稳定性审计和 Candidate 准入证据。

提示词分别位于 `ORCH_PROMPT.md`（仅 DeepSeek 参数规划）和 `MCP_PROMPT.md`（双模式操作契约）。完整说明见 `docs/MODEL_RESEARCH_WORKFLOW_CURRENT.md` 与 `docs/MODEL_RESEARCH_PRODUCTION_RUNBOOK.md`。

## 正式入口与任务控制

- MCP 正式入口为 `fxalpha_model_orchestrator_start`；HTTP 正式入口为
  `POST /model/orchestrator/start`。HTTP 启动是异步的，成功受理后立即返回
  `status=accepted`，真实进度以 `GET /model/status` 为准。
- 平台同一时间只允许一个受管模型任务。重复启动不会创建第二个训练线程，而是返回
  `status=already_running` 和当前任务。
- `POST /model/jobs/stop` 发出协作式停止请求；Research 在当前 round 完成后停止，
  Rolling 在当前 seed 边界停止。`POST /model/jobs/resume` 只恢复
  `interrupted / failed` 任务，并复用已完成 round、seed 与 fold 证据。
- Research 未传 `feature_set_id` 时，后台先冻结当前 active values；若已有内容完全一致的
  不可变快照则复用，不重复生成同内容快照。

任务状态保存在 `runtime/model/jobs.sqlite`，启动日志保存在
`runtime/model/jobs/<job_id>.log`。production refit 通过后会原子更新
`runtime/model/active_production_model.json`；预测默认读取该指针。首次生产晋升前，
该文件不存在属于正常状态。
