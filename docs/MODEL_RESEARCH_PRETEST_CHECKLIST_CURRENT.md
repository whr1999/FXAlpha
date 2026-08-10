# FXAlpha 模型真实测试前检查表

更新：2026-08-01

本文只用于启动第一次真实 research / production Rolling 前的验收，不替代
`domain/model/PROMPT.md` 或生产运行手册。

## 1. 服务与协议

- `GET /health` 成功。
- `GET /model/status` 返回 `status=ready` 或当前真实的 `running`。
- `POST /model/orchestrator/start` 应立即返回 `accepted`；已有任务时应返回
  `already_running`，不能生成第二个训练线程。
- `POST /model/jobs/stop` 与 `/model/jobs/resume` 可用；停止只在安全边界生效，
  恢复必须沿用原 session 和已完成证据。
- MCP 存在 `fxalpha_model_confirm_research_round` 与
  `fxalpha_model_start_production_rolling`。
- MCP 不存在旧 `fxalpha_model_forward_test`、`fxalpha_model_sota_gate`。
- `/model/forward-tests` 只允许返回 410，表示历史入口已移除。
- `/model/research/current` 如仅有旧流程记录，必须标记
  `record_era=historical_pre_dual_mode`、`current_contract=false`，旧 LLM review
  不得保持 active。

## 2. 数据与 feature set

- `/model/preflight` 必须 `passed=true` 且
  `safe_to_freeze_feature_set=true`。
- active values 的 registry fingerprint 必须一致；正式输入使用不可变 feature
  manifest，模型模块不重新计算因子值。
- 核对 factor count、LABEL0 的 next-open 到 forward-open 合同、实际
  `label_exit_shift_days` 和 open 成交。
- 第一次真实测试使用启动当时的 all-active 因子快照；当前数量必须从 manifest、
  factor registry 和 active values 实时核对，不得在代码或手册中硬编码数量。
  诊断子集等不可变快照不能意外更新 active pointer；相同内容重复冻结应复用原快照。

## 3. 模型库

- `models.status` 只出现 `research / candidate / production / archived`。
- 每条 metadata 的 `asset_status` 与主状态一致。
- 普通研究结果写 `research`；只有正式四折 Rolling 达标才写一个聚合
  `candidate`。
- 三 Seed Research 只登记一个正式 Seed42 模型；Seed17/83 只能存在于状态库和
  审计证据，不得增加模型库可见数量。
- production refit 只接受 Rolling candidate，固定 Seed42，并且重复调用不重复
  创建 production。
- 新登记模型必须具备 feature set id、fingerprint、factor ids/count、feature count 与
  训练窗口来源。无法从已删除历史快照补全的旧记录只做显式遗留说明，不伪造字段。
- production refit 成功后必须原子更新
  `runtime/model/active_production_model.json`；没有 production 时该指针不存在是正常状态。

在第一次真实 Rolling 前，零 candidate、零 production 是预期状态。不要为了让
GUI 看起来“有结果”而恢复历史 candidate 或 production。

## 4. GUI

- 概览与模型库分别显示 Research、Rolling Candidate、Production 的真实数量；
  数量为 0 时不得使用 research 总数兜底。
- 模型研究卡显示 Seed42 研究分、优胜轮 Seed 审计和训练诊断；同参数模型只出现一次。
- 模型库主评分显示 Seed42 `research_score`；`confirmed_research_score` 只在审计区，
  `rolling_score` 显示为“Rolling 准入分”。不得显示 Forward、SOTA 或“最好 Seed”。
- Research 与 Rolling 正式曲线固定 Seed42；GUI 不提供 Seed17/83 正式回测切换。
- 没有 candidate 时，Rolling Candidate 显示 0/暂无；没有 production 时交易链
  保持阻塞。
- 历史日志可以保留旧 SOTA/forward 原文，但必须显示“历史旧流程”，不得进入当前
  判断、Seed 确认或候选区语义。

## 5. 第一次真实测试顺序

1. 只启动 `evaluation_mode=research`，使用当前不可变 all-active feature set。
2. 先跑基准轮，再让 DeepSeek Flash 做受约束参数研究。
3. 平台连续三轮无有效改善后停止，只给会话优胜轮补 Seed17/83。
4. 人工核对研究确认和 artifacts 后，再以该 round 启动 production Rolling。
5. Rolling 先跑 Seed42；初筛通过后才跑 Seed17/83。达标后应只新增一个
   aggregate candidate。
6. 先 dry-run production refit，确认来源、按最新快照动态生成的 segments、Seed42 和
   路径，再决定是否执行晋升；成功后核对生产指针与预测默认选择一致。

任一步出现 feature 漂移、窗口重叠、标签 purge 错误、预测不完整、可靠性检查
失败或状态身份不一致时立即停止，不进入下一阶段。
