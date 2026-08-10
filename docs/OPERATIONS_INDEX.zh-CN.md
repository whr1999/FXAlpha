# FXAlpha 生产运维入口

本文是平台值守和生产变更的唯一导航页。业务算法与公式以
[`BUSINESS_WORKFLOWS.zh-CN.md`](BUSINESS_WORKFLOWS.zh-CN.md) 为准；本页只说明
如何判断状态、执行操作、处理异常和回滚。

## 每日值守顺序

1. 打开平台总览或调用 `/health`，确认 API 与 QuantGPT 在线。
2. 查看 `data-status`，确认生产数据集指针、质量审计和目标交易日一致。
3. 分别查看因子、模型、预测和模拟交易状态；健康的 API 不代表各业务 lane 已就绪。
4. 写操作前执行对应 preflight，严格按 blocker 停止，不绕过门禁。
5. 日更完成后确认 `post_promote_audit=passed` 和 `production_health=ready`。
6. 只有 production model、预测和交易 preflight 都通过时才推进模拟交易。

## 任务到文档

| 任务 | 必读文档 | 主要入口 |
| --- | --- | --- |
| 数据日更 | [`DATA_FOUNDATION_DAILY_RUNBOOK_CURRENT.md`](DATA_FOUNDATION_DAILY_RUNBOOK_CURRENT.md) | `data-daily-preflight`、`data-daily-routine` |
| 数据全量重建 | [`DATA_FOUNDATION_TUSHARE_REBUILD_RUNBOOK_CURRENT.md`](DATA_FOUNDATION_TUSHARE_REBUILD_RUNBOOK_CURRENT.md) | rebuild preflight / resume |
| 因子挖掘 | [`FACTOR_RESEARCH_OPERATIONS.md`](FACTOR_RESEARCH_OPERATIONS.md) | `factor-orch status/start/pause/resume` |
| 模型训练 | [`MODEL_RESEARCH_PRODUCTION_RUNBOOK.md`](MODEL_RESEARCH_PRODUCTION_RUNBOOK.md) | model preflight、ORCH/MCP、Rolling |
| 预测与模拟交易 | [`TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md`](TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md) | prediction、paper fleet preflight |
| 磁盘治理 | [`PLATFORM_OPS_RUNBOOK.md`](PLATFORM_OPS_RUNBOOK.md) | maintenance safe preview / execute |
| 部署与回滚 | [`LOCAL_DEPLOYMENT.md`](LOCAL_DEPLOYMENT.md) | 独立配置、影子端口、systemd |

## 写操作门禁

数据 promote、因子 import/retire、模型 promotion、预测写入、paper fleet、cleanup
execute 和 systemd 切换，都必须先确认对应 lane 没有活动任务或锁。不要删除锁文件来
伪造空闲；应恢复或终止拥有该锁的真实任务。

## 路径与状态真相

- `storage/paths.py` 是路径解析权威。
- `FXALPHA_CONFIG_FILE` 选择部署配置。
- `paths.runtime_root` 把可变运行状态放到 release 外部。
- API 启动的瞬态数据、因子评分、入库和 Orchestrator worker 会继承
  `runtime.env` 中经过白名单筛选的运行变量（包括
  `FXALPHA_CONFIG_FILE`）；修改该文件后应重启长驻服务，不要在不可变
  release 内另放一份生产配置。
- 生产数据集指针以及因子、模型、交易数据库是业务权威源；GUI 是投影，不是第二份状态。
- release 切换必须把四个 service、一个 target 和两个 timer 当成同一套单元安装；
  只有业务健康检查通过后才重新启动 timer，不能让 timer 定义继续依赖旧 checkout。

## 故障与回滚顺序

记录 lane、任务 ID、目标日期、stage 和 blocker；核对服务、进程、锁、任务数据库和
状态文件；优先恢复同一个持久任务；恢复后重新执行该 lane 的审计和下游身份检查。
影子验证使用独立 runtime、SQLite 副本和端口。切换失败时恢复旧 unit 和旧 release，
不对生产数据库做猜测式逆向修复。清理删除、服务切换和旧资产删除是三个独立审批点。

所有顶层文档的分类记录在
[`DOCUMENTATION_MANIFEST.yaml`](DOCUMENTATION_MANIFEST.yaml)。带日期的报告是时点证据，
不得覆盖当前 runbook；历史设计稿不得作为生产操作指令。
