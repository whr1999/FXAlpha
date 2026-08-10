# FXAlpha Platform Governance MCP

更新时间：2026-07-23

## 定位

`fxalpha-platform` 是 FXAlpha 平台级治理 MCP。它不负责 QuantGPT 因子挖掘，也不负责 RD-Agent 模型训练，只负责跨模块资产治理和运维能力。

当前能力：

- 因子库审计与聚类治理
- 因子地图只读状态与稳定信息区域谱系
- Feature Set 建议
- 只读 retire/watch 计划
- 平台维护状态与 safe cleanup 预览/执行

入口边界：

- 生产治理首选 `fxalpha-platform` MCP。
- GUI 通过同一服务的 HTTP API 做展示和显式人工操作。日常质量核查、
  信息簇核查和全部核查可以直接在因子库页面启动，不需要 Codex 介入。
- CLI 仅用于人工/故障 fallback 或本地回归检查，不作为主业务入口。

## MCP 工具

### 因子库治理

- `fxalpha_factor_library_audit(scope="all", save_report=true)`
  - `scope="quality"`：运行质量核查，检查 registry、因子值列、覆盖、缺失、常数和元数据完整性。
  - `scope="information"`：运行信息簇核查，计算真实因子值相关矩阵、强相关治理簇、完整信息家族和 Feature Set 建议。
  - `scope="all"`：在同一份 registry / factor-values 快照上生成两份独立报告。
  - 不再使用 `lightweight / full` 作为业务分类。
  - 质量核查覆盖全部 active 因子；缺少因子值的条目明确记为质量问题。
  - 信息簇核查只对具备可用值列的因子计算相关性，并在 `eligibility` 中列出排除项。
    这种“部分值仓库”本身不让报告永久 stale；注册表全集或值仓库清单发生变化时才失效。

- `fxalpha_factor_audit_status(scope="all")`
  - 分别读取最近质量报告、信息报告或组合视图，不重算。

- `fxalpha_factor_map_status(region_uid="")`
  - 读取 `factor_library_audit_v4` 生成的统一因子地图，不重算、不启动研究。
  - 地图区域只由真实因子值信息审计决定；`region_uid` 跨快照继承，
    `lineage_event` 明确记录 unchanged、membership_changed、split、merged 等变化。
  - stale 审计仍可展示，但 `available_for_research=false`，不得作为新的研究上下文。
  - HTTP 展示接口为 `GET /factor/map`；`POST /factor/map/refresh` 复用
    `scope=information` 的同一后台审计队列。

- `fxalpha_factor_feature_set_recommendations()`
  - 读取最近审计生成的 Feature Set 建议。
  - 如果没有 fresh 信息审计，返回 `status=missing/stale`，不会自动生成报告。

GUI 按钮统一调用 `POST /factor/library/audit/run`，并通过
`GET /factor/library/audit/run-status` 轮询后台状态：

- `质量核查` -> `scope=quality`
- `信息簇核查` -> `scope=information`
- `全部核查` -> `scope=all`

三者复用同一个 `factor_library_audit` 服务、同一份 registry / active values
事实源和同一套 latest/history 报告，不维护第二套 GUI 审计逻辑。运行期间重复请求
会返回 `factor_library_audit_already_running`，不会覆盖当前任务的 scope。

- `fxalpha_factor_retire_plan()`
  - 返回只读 retire/watch 建议。
  - 不修改 `factor_registry.db`。
  - 只有 fresh `information` 审计才会暴露 retire candidates；missing 或 stale
    信息审计返回空候选和 blocked reason。

报告事实源：

```text
runtime/factor_audit/quality/latest.json
runtime/factor_audit/information/latest.json
runtime/factor_audit/run_status.json
runtime/reports/factor_audit/{audit_id}_quality.json
runtime/reports/factor_audit/{audit_id}_information.json
```

`latest.json` 是程序读取路径，历史报告只追加。质量核查和信息簇核查互不覆盖；状态读取不触发修复、刷新或重算。
信息报告同时是因子地图底座，包含 `map_id`、稳定 `region_uid`、区域谱系和完整
`relation_graph`。因子地图服务只组合该报告，不维护第二套聚类结果。

## 因子地图与研究上下文边界

因子地图 `factor_map_v3` 统一承接三类只读事实：

- 信息区域及其稳定 `region_uid` 来自 fresh `factor_library_audit_v4` 信息审计；
  区域名称、核心字段用途和组合形式由表达式 AST 只读归纳，不采用代表因子名称，也
  不把窗口或外层单调包装当成新的业务结构。
- 研究轨迹从已完成的 `research_steps` 投影，只观察
  `novelty_review -> deep_validation_review -> import_gate_review -> import_review`；
  稳定 `trajectory_id` 把同一候选的后续结果串起来。
- 历史经验库保留在原归档中。迁移只生成
  `runtime/factor_map/legacy_experience_migration_v1.json` 审计清单，不改写归档；
  没有明确 registry `factor_id` 锚点的自然语言经验保持 unmapped，不能靠文本猜测
  挂到信息区域，也不进入模型 prompt。迁移已经完成并冻结；生产服务只报告归档
  marker 和迁移回执状态，不再扫描、校验、解析或投影旧经验正文。

DeepSeek 研究上下文只使用一个 `factor_map_context`：

- `thesis_design` 读取完整的精简区域清单；`hypothesis_design` 只读取核心字段与已选
  thesis 相交的区域。每个模型可见区域只包含业务名称、核心字段用途、组合形式、一个
  已验证代表因子、当前 run 漏斗统计和保守引导；active 数量只保留给 GUI/审计。
- `expression_design` 不读取因子地图，只根据已确定 hypothesis、可用字段/算子、
  本 run 表达式历史、upstream handoff 和候选级代码建议构造表达式；正式相似性仍由
  后续 novelty 工具判断。
- `candidate_plan` 不读取整张地图，只处理本轮候选、表达式预检查和候选级代码建议。
- `score_review`、`novelty_review`、`deep_validation_review`、质量门禁和导入阶段
  不读取地图上下文，继续以当前 run 的工具证据和正式门禁为准。
- `round_synthesis` 只读取已经形成 observe/action 引导的区域；action=none 的原始
  计数不进入本阶段 prompt。
- 地图始终是 advisory；不得改变 quick/deep 分数、数值 novelty、质量门禁或导入结果。
- 模型 prompt 不包含区域相关矩阵、成员表达式、原始研究事件或历史经验卡文本。

区域引导只有在跨 round 的重复证据达到门槛后才升级为 action：

- novelty observe：至少跨 2 个 round 拒绝 2 次且拒绝率至少 75%；action：至少检查
  3 次并拒绝 3 次、拒绝率至少 75%、覆盖至少 2 个 round，且同一语义结构至少重复
  拒绝 2 次；
- deep observe：至少跨 2 个 round 拒绝 2 次且相同失败组件至少出现 2 次；
  action：至少检查并拒绝 3 次、拒绝率至少三分之二、覆盖至少 2 个 round，且相同
  失败组件至少出现 3 次。

这些引导只要求停止窗口、常数或外层包装式变体，或回应明确的稳健性弱项；不允许
代码据此跳过正式 score、novelty、deep、gate 或 import。

GUI 的“因子地图”页面读取 `GET /factor/map`，展示信息区域、区域活动、研究轨迹和
历史迁移状态；旧经验归档不再作为可编辑或可直接送入 DeepSeek 的工作区。

### 平台运维

- `fxalpha_platform_maintenance_status()`
  - 返回磁盘审计、cleanup preview、服务健康状态。
  - `project_total` 是对项目根目录的 deduplicated project scan / real project usage，不再把 `runtime` 与 `runtime/data_foundation`、`data` 与 `data/model` 等父子路径重复相加。
  - 磁盘审计仍会单独展示 `pickle_cache`、`runtime/data_foundation/staging`、`runtime/data_foundation/production_backups`、`data/model` 等 key paths，方便定位膨胀来源。

- `fxalpha_platform_cleanup_preview(profile="safe")`
  - 只做 dry-run，不删除文件。
  - 默认 safe 已直接增强，不存在 `safe_plus`。
  - safe 覆盖可再生 pickle cache、旧数据底座 staging/production backup、旧数据底座修复/诊断 backup、旧 reset backup、旧 model feature sets、旧 RD-Agent workspace、旧 trading prediction feature snapshots。
  - 当前生产引用、active model feature snapshot、最近保留项、48 小时内新 model/workspace、24 小时内新数据底座包、running/locked 资产会显示为 protected/blocked。
  - `retention_days_json` 支持合法 JSON object 字符串；非法 JSON 返回结构化 `invalid_retention_days_json`，不会抛未处理异常。

- `fxalpha_platform_cleanup_execute(profile="safe")`
  - 执行 cleanup。
  - 只能在用户明确确认后调用。
  - MCP execute 只允许 `profile="safe"`；非 safe 清理必须先 preview，并作为人工/故障 fallback 处理。
  - 复用 `services/maintenance_service.py` 和 `domain/platform_ops` 的 protected/manual-review 规则。
  - 定时 heartbeat 只能自动调用 preview，不能自动 execute。

## Feature Set 规则

Feature Set 来自因子库审计结果，不直接启动模型训练。
推荐集合只基于本次审计判定为 usable 的因子生成；缺少因子值列、全空、
常数/近常数等 `bad` 因子会保留在 `factor_checks` 里供治理查看，但不会进入
Feature Set 建议。

- `ALL_ACTIVE`
  - 全部 usable active 因子。
  - 用作树模型全量 baseline。

- `FAMILY_TOP1_PLUS_UNCLUSTERED8`
  - 每个信息家族只取入库分数最高代表因子。
  - 再补充未聚类高分因子，最多补 8 个。

- `FAMILY_TOP2_PLUS_UNCLUSTERED8`
  - 每个信息家族最多取 2 个高分因子。
  - 再补充未聚类高分因子，最多补 8 个。

- `FAMILY_TOP3_PLUS_UNCLUSTERED8`
  - 每个信息家族最多取 3 个高分因子。
  - 再补充未聚类高分因子，最多补 8 个。

- `QUALITY_TOP12`
  - 全库按入库后分数取前 12 个。

代表因子选择只看入库后分数：

```text
deep_score > quality_score > score > quick_score
```

如果某个 Feature Set 和 `ALL_ACTIVE` 完全相同，输出 `degenerate=true`，GUI 显示“未降维”。

每次 fresh 信息簇核查都生成 Feature Set。ORCH 新研究启动时为了固定本 run 的
信息簇上下文所做的刷新，也保留 Feature Set 输出，不允许用
`include_feature_sets=false` 的报告覆盖治理页面 latest。

## 全库关系图

信息审计在同一次 pairwise 相关矩阵上输出 `relation_graph`，GUI 不再为每个家族
复制一张相同的星形示意图：

- 所有 usable active 因子都作为节点出现；
- 家族代表与家族成员使用实测依赖度连接；
- 每个家族代表保留最强的两个跨家族代表关系；
- 线条粗细表示 `dependency_score`，完整 pair 数、展示边数和边选择策略写入
  `relation_graph.summary`。

这只是同一份信息审计结果的可视化投影，不重新计算相关性，也不改变正式 novelty
或因子研究质量门。

## 新窗口调用提示词

可以直接给新 Codex 窗口：

```text
请使用 FXAlpha 的 fxalpha-platform MCP 做平台治理。先调用 fxalpha_platform_maintenance_status 查看 deduplicated project_total、pickle_cache、data_foundation/staging、production_backups、model_feature_sets、RD-Agent workspace、safe 可释放空间和最近 preview 时间。需要清理缓存时先调用 fxalpha_platform_cleanup_preview(profile="safe")，确认当前 production package、当前 promotion backup、active model feature snapshot、最近保留项、48 小时内新 model/workspace、24 小时内新数据底座包和 running/lock 状态都被 protected/blocked 后，只有我明确确认才可以调用 fxalpha_platform_cleanup_execute(profile="safe")。safe 已直接增强，不存在 safe_plus。CLI/HTTP/GUI 只是 fallback 或展示入口。不要通过这个 MCP 做 QuantGPT 因子挖掘或 RD-Agent 模型训练。
```

## 安全边界

- 自动 retire 永远关闭。
- 审计只输出建议，任何 registry 写操作必须另走人工确认流程。
- cleanup 不允许绕过 protected/manual-review 规则。
- 因子库、模型库、active feature snapshot、QuantGPT adopted mirror、canonical `data/` 生产行情和知识库均为受保护资产。
- 数据底座 `runtime/data_foundation/staging` 和 `runtime/data_foundation/production_backups` 是可治理的运行副本，不是生产读路径；safe 策略保护当前 production package、当前 promotion backup、最近 1 个额外包和 24 小时内新包。
- 数据底座 `runtime/data_foundation/backups` 是修复/诊断备份根目录；safe 可清理超过 2 天的旧目录，但 24 小时内新目录或 data-foundation running/lock 状态必须 blocked。
- 如果 `runtime/data_foundation/production_update.lock`、`update.lock`、`staging.lock`、`promote.lock` 存在，或 daily status 表明 staging/promote 正在运行，数据底座清理候选必须全部 blocked。
- 如果无法确认当前 production package 或当前 promotion backup，数据底座清理必须 fail-closed，全部 blocked。
- 旧 model feature sets 可纳入 safe candidate，但 active snapshot 引用、model registry 中 production/active/best/latest 仍引用的 feature set、最近 5 个目录、48 小时内新目录必须 protected/blocked。
- 旧 RD-Agent workspaces 可纳入 safe candidate，但最近 10 个 workspace、48 小时内新 workspace、running/lock workspace、当前/latest model status 或 registry 可追踪到的 workspace 必须 protected/blocked。
- 旧 trading prediction feature snapshots 可纳入 safe candidate，但最近 1 个 snapshot 必须保留。
- cleanup execute 必须由用户明确确认；自动化、GUI 刷新和 MCP status 只能触发 status/preview。

## 维护命令

以下命令只作为人工/故障 fallback 或本地回归检查；生产治理仍先走 MCP preview。

```bash
PYTHONPATH=. .venv-test/bin/pytest -q -s tests/test_factor_library_audit_service.py
python3 cli.py factor-audit status --scope all
python3 cli.py factor-audit run --scope quality
python3 cli.py factor-audit run --scope information
python3 cli.py factor-audit run --scope all
python3 cli.py maintenance status
python3 cli.py maintenance cleanup --profile safe
```

只有在人明确确认、且 MCP transport 不可用或需要故障 fallback 时，才允许使用：

```bash
python3 cli.py maintenance cleanup --profile safe --execute
```

执行前必须先看 dry-run 候选，确认当前 production package 和当前 promotion backup 只出现在 protected/blocked 列表中。

## Windows Doctor

Windows 侧 doctor 是服务恢复和人工排障入口，不是平台治理主入口：

```powershell
.\fxalpha_doctor.cmd -Action status
.\fxalpha_doctor.cmd -Action open-gui
.\fxalpha_doctor.cmd -Action recover-safe
```

`open-gui` 会先检查 18081 API，必要时通过正常 API 启动路径拉起服务，再打开 `http://127.0.0.1:18081/gui/`。

如果 Codex 侧 MCP 工具已暴露但调用返回 `Transport closed`：

1. 不要改走手工删除。
2. 先验证 `./.mcp.json` 中 `fxalpha-platform` 的 `cwd` 和 `PYTHONPATH`。
3. 再验证 `python3 -c "import mcp_servers.platform_server"`。
4. HTTP `GET /maintenance/status` 和 CLI 只能作为诊断 fallback。
