# FXAlpha 多模型生产级模拟交易设计与运行契约

Updated: 2026-08-09

## 1. 目标与边界

本模块在生产数据晋升完成后，使用已经晋升为 production 的 Qlib 模型进行逐日模拟交易。它不是模型训练、历史研究回测或真实券商下单系统。

生产链固定为：

```text
promoted data -> production model deployment -> prediction -> account recommendation
-> next trade-day Qlib execution -> account snapshot -> audit and GUI
```

正式执行器是 Qlib paper account。vn.py 已从公开候选架构退役，不再保留运行时 adapter。每日模拟交易不重训模型；模型可以连续服务多个交易日。生产 GUI 创建的 `fixed_model` 账户实行一个账户永久绑定一个模型：模型更换或模型并行比较必须创建新账户，禁止在原账户静默替换模型或续接账本。

## 2. 核心对象

| 对象 | 主键 | 责任 |
| --- | --- | --- |
| PaperAccount | `account_id` | 独立资金、策略合同、持仓、收益曲线和生命周期 |
| ModelDeployment | `deployment_id` | 把生产 `model_run_id` 绑定到一个账户及生效日期区间 |
| FleetRun | `fleet_run_id` | 一次数据包/目标日期下的全账户日切批次 |
| AccountRun | `account_run_id` | 一个账户在一个信号日和配置哈希下的可恢复状态机 |
| Recommendation | `recommendation_id` | 冻结信号、目标仓位、策略参数和预计执行日期 |
| Execution/Snapshot | execution id / account+date | 冻结成交、持仓、现金、净值与证据文件 |

同一模型可以绑定多个账户；一个生产 `fixed_model` 账户只能绑定一个 `model_run_id`。不同模型并行运行必须分别创建账户；不同账户即使使用相同模型，也必须使用不同 `account_id`，其运行目录、推荐、执行和账本完全隔离。

`account_id` 是不可变的技术主键，GUI 展示名由绑定模型自动生成，格式为“模型来源/角色 · 特征集 · 模型时间”。模型存在 `manual_promotion_exception` 时必须显示“手工晋升”；来源 campaign 仍为 research 时还必须显示“研究来源”。用户输入的策略名、置信度合同名不得作为账户展示名。`rolling_champion` 仅保留为非 GUI 的显式日期化 deployment 能力，不能用于生产 fixed-model 模型对比。

## 3. 策略合同

生产默认合同为 `top20_drop2_hold5_open_v1`：

- 初始资金：1,000,000 CNY
- `topk=20`
- `n_drop=2`
- `hold_thresh=5`
- `deal_price=open`
- 等权目标组合
- Qlib Exchange 负责费用、成交限制、整手约束和涨跌停交易判断

策略合同在账户产生第一条 snapshot 后不可原地修改。需要改变参数时，应创建新账户或新合同版本，避免前后收益不可比较。

生产同时支持独立的新合同 `confidence_cash_top20_drop2_hold5_open_v2`。该合同不修改旧账户和旧账本，规则如下：

- 数据未 ready、预测缺失/空值/常量、PIT 身份覆盖不足 95% 仍是 hard block。
- 低唯一分数、Top20 边界同分和低模型容量属于 weak signal，不再因任意代码排序而凑满 20 只。
- 边界并列时只保留严格高于 Top20 边界的股票；每个有效槽位权重为 `exposure_multiplier / topk`，不能改为 `1 / selected_count`。
- `target_stock_exposure = selected_count * exposure_multiplier / topk`，其余明确冻结为目标现金。
- V1 从 production run 的 `training_diagnostics.json` 读取模型树数；一棵树使用 0.5 模型乘数。表现置信度在没有足够已完全观测证据时保持 neutral，不读取尚未到期标签。
- 推荐冻结 `confidence_policy_version`、模型/选股/表现三层证据、目标股票/现金比例、原因及证据截止日期。

新合同由 `target_weight_v2` 执行器消费冻结 `target.csv`。执行器先卖出超出目标的数量，再买入目标缺口，支持部分卖出并保留剩余现金。模型置信度减仓可超过 `n_drop=2`，但仍受 `hold_thresh=5`；当市场/账户风控上限低于模型上限时，战略性 `hold_thresh=5` 也让位于降风险，停牌、涨跌停、整手和现金等交易约束始终保留。未能卖出的旧仓必须先占用当日总风险预算，实际仓位达到目标上限后禁止新买。新标的加仓按 Drop2 渐进。无法完成的目标必须记录为 target/actual exposure gap 和 execution constraints。

### 3.1 生产模拟交易风控覆盖层

所有新生成的生产模拟交易推荐在模型目标组合之后、订单预览之前统一经过 `market_resonance_account_brake_v1`。它只等比例缩放目标权重，不改变股票排名和入选名单：

- 沪深300、中证500、中证1000分别计算20日和60日对数收益；两个周期都至少有两个指数为负，且三指数等权组合20/60日年化波动率较大值不低于18%，构成原始市场压力。
- 原始压力连续2个交易日后确认压力状态；连续3个交易日恢复后退出。压力状态的市场股票仓位上限为75%。
- 账户读取最近60个已完成账本日的扣费净值；只有市场压力仍成立且滚动回撤达到8%时，账户上限才进一步降为50%。
- `final_stock_cap = min(model_cap, market_cap, account_cap)`，T日收盘计算并冻结在 recommendation，T+1开盘只能执行冻结目标。
- 市场基准缺失、共同最新日落后于 signal date 或历史少于61日时拒绝生成推荐，不静默跳过风控。

运行配置保存在 `./runtime/trading/risk_policy.json`，支持 `enforced` 和 `shadow`，配置变更只作用于下一份新推荐。每份推荐同时冻结 `risk_decision_<signal_date>.json`；配置指纹进入 recommendation contract hash，旧 pending 不会被原地改写。

## 4. 每日运行语义

生产后端只有一个会推进账本的核心动作：

```text
paper_account_day_run(account_id, signal_date)
```

它固定执行 `到期 pending -> 成交/盯市 -> 当日推荐 -> 账本审计 -> checkpoint`。每日舰队只选择 active 账户，历史补跑只选择缺失交易日；两者最终都逐次调用同一个核心动作，不再由日常流程调用“补跑引擎”。

`paper-fleet-run` 按账户串行处理，账户之间故障隔离：

1. 校验 HDF5、Qlib、QuantGPT 最新日期一致且 production health ready。
2. 读取所有 active 账户及目标日期对应的 deployment。
3. 为每个模型补齐目标区间预测缓存。
4. 历史缺口日期以 `catch_up_replay` 调用单日核心，目标日以 `on_time` 调用同一核心。
5. 写入账户 snapshot、冻结文件、AccountRun checkpoint 和事件日志。
6. 全部账户结束后写 FleetRun；部分账户失败时标记 `partial_failed`，不回滚其他已完成账户。

同一 `account_id + signal_date + config_hash` 生成稳定 AccountRun ID。已完成日期再次运行返回 `already_completed`；失败日期从该日期继续，不能重复覆盖已完成账本。

置信度合同的 config hash 额外包含 policy version、模型乘数阈值、选择规则和执行器版本。T 日生成的置信度与现金目标随 recommendation 冻结，T+1 只能执行该冻结目标，不能使用 T+1 新信息静默改写 pending。

Fleet run 与手工 account replay 共用 `./runtime/trading/fleet/paper_operation.lock` 跨进程排他锁。systemd、CLI 或 GUI 同时发起写任务时，只有一个任务进入；其余返回 `paper_operation_in_progress`，不得并发成交。

`paper_fleet_status` 是纯读取快照：读取数据日期、账户、deployment、账本、pending、AccountRun 和最近 FleetRun，不生成 replay plan、不扩展预测，也不执行分数质量检查。它与 preflight、单日日结共用轻量账本不变量，能够阻断未成交即被覆盖的推荐、缺少 execution 的已成交推荐、多条 pending 和资产恒等式错误。精确历史缺口和逐日质量证据只在显式请求 replay plan 时计算。`accounts` 兼容字段只返回 active 账户；完整生命周期分别由 `active_accounts`、`paused_accounts`、`retired_accounts` 暴露。paused 计划显示为冻结且不会执行；retired 账户的 pending 在退休事务内终止，账户仍以只读方式保留在管理视图。

账户创建时 PaperAccount 与首个 ModelDeployment 在同一个 SQLite 事务提交；任何一项失败都不留下半成品账户。账户状态切换同样是事务化的。每次舰队运行和状态切换都会先归一化遗留的 `running` AccountRun：只有同日 snapshot 与 recommendation 两项持久证据齐全时才恢复为 completed，否则标记为 interrupted/failed，绝不补造成交。

## 5. 历史缺口补跑

当数据中断后重新补齐，使用完整 replay，而不是旧的纯 mark-to-market backfill：

```text
paper-replay-plan -> explicit confirmation when gap > 5 days -> paper-replay-run
```

回放按 Qlib 交易日历升序推进，严格先成交旧 pending、再生成当日推荐。每个信号只读取 `as_of_date` 当日或以前的特征；模型版本由该日 deployment 决定。ST/退市过滤直接读取生产 HDF 中对应交易日的 point-in-time 状态，后续日期的身份变化不能反向影响历史候选。当前数据源没有保留每次晋升前的原始数据版本，因此价格和因子回放口径仍明确记录为 `latest_promoted_restated_asof_capped`：使用当前已晋升、可能经后续修订的数据，但特征和信号日期被 As-Of 截断，不读取未来日期。

自动日切只自动补 5 个及以内的交易日缺口；超过 5 日必须显式 `--confirm-long-replay`。计划阶段会显示将新增、将跳过、缺少 deployment 的日期和数据包 ID。旧合同仍把低分数区分度作为阻断；置信度合同只阻断无效预测，弱但有效的日期会生成低仓位/现金推荐。逐日 confidence evidence 使用相同版本策略并随 replay 结果冻结。

旧命令 `trade-paper-backfill` 已退役；生产缺口只能走账户级 replay 计划和同一个账户日切核心。

## 6. 数据库与运行证据

主数据库：`./data/trading/execution_log.db`

关键表：

- `paper_accounts`
- `paper_account_model_deployments`
- `paper_fleet_runs`
- `paper_account_runs`
- `paper_run_events`
- `recommendation_batches` / `recommendation_orders`
- `paper_executions`
- `paper_account_snapshots`

运行证据：

- fleet 最新状态：`./runtime/trading/fleet/latest_status.json`
- 账户状态：`./runtime/trading/paper_trading/{account_id}/state/account_state.json`
- 每日冻结目录：`./runtime/trading/paper_trading/{account_id}/executions/{date}_{event_id}/`
- 账户推荐：`./runtime/trading/recommendations/{account_id}/`
- 账户独立目标：`./runtime/trading/targets/{model_run_id}/{account_id}/`
- 生产账本备份：`./runtime/trading/production_backups/`

## 7. API、CLI 与 GUI

只读 API：

- `GET /paper/status`
- `GET /paper/fleet/preflight`
- `GET /paper/replay/plan`
- `GET /platform/automation-status`（轻量读取 WSL systemd 日更/模拟交易服务、定时器、最近结果与资源峰值）
- `GET /trade/risk-policy?account_id=<id>&history_days=160`：返回当前配置/冻结决策，以及由同一生产计算器重建的无前视市场广度、复合波动、账户回撤和四层仓位上限历史；GUI 不重复实现风控公式。

写 API（必须 `confirm: true`）：

- `POST /paper/accounts`
- `POST /paper/accounts/status`
- `POST /paper/run`
- `POST /paper/replay`
- `POST /trade/risk-policy`（更新下一份推荐使用的风控参数）

旧的 `/paper/fleet/status`、`/paper/fleet/run` 和 `/paper/replay/run` 暂时保留为兼容别名；GUI 和新调用方使用上述正式短路径。

旧的生产写入口 `/trade/paper`、`/trade/paper-vnpy-legacy`、`/trade/recommend`、`/trade/execute-pending`、`/trade/paper-backfill`、`/trade/daily-routine` 和 `/daily-ops/routine` 返回 HTTP 410。对应 CLI 名称只保留为可解释的退役桩，不再执行写操作。不存在“扫描全部 pending 并执行”的入口；所有计划执行必须绑定一个 active `account_id` 并通过 fleet/replay 的统一锁。

CLI：

```bash
python3 cli.py paper-account-create --account-id <id> --model-run-id <production_run_id> --effective-from <YYYY-MM-DD> --strategy-contract-version confidence_cash_top20_drop2_hold5_open_v2
python3 cli.py paper-account-status --account-id <id> --status active|paused|retired
python3 cli.py paper-fleet-status
python3 cli.py paper-fleet-preflight
python3 cli.py paper-fleet-run
python3 cli.py paper-replay-plan --account-id <id> --from-date <date> --to-date <date>
python3 cli.py paper-replay-run --account-id <id> --from-date <date> --to-date <date> --confirm-long-replay
```

GUI 的生产模拟交易页展示 active、paused、retired 三类账户，模型自动名称、模型晋升/研究来源标签、账户净值/持仓/pending 和运行日志；非 active 账户仍可查看，paused 可恢复，retired 只读。账户创建区只允许选择当前 production model，明确提示“一账户一模型”，不接受自定义展示名。状态刷新只显示“已追平”或“需要生成计划”，用户进入补跑页并显式点击后才生成精确日期和逐日质量证据。置信度账户额外展示 confidence state、有效槽位、目标/实际股票仓位、目标/实际现金、模型/选股证据和约束偏差。“风控策略”菜单展示四条仓位上限迷你走势、三指数20/60日上涨广度、20/60日复合波动和账户滚动回撤；压力区间与阈值直接来自 `services.trading_service.trading_risk_policy_status`，底层统一调用 `domain.trading.risk_policy`，前端只画图。控制台可修改有限且经过范围校验的风控参数，保存必须二次确认。历史研究回测页及其生产 GUI 固定入口已移除。

前端按子页加载：所有模拟交易页只取 compact trading status；控制台取 compact fleet，账户总览/风控/调仓/交易页才取完整账户数据；基准曲线、160 日风控历史和 daily-ops 诊断只在对应子页请求。所有 POST 使用 30 秒超时、非 2xx 错误回显和 `finally` 恢复控件。

## 8. 调度与恢复

`fxalpha-data-daily.service` 只有成功完成后才触发 `fxalpha-paper-fleet-daily.service`。另有 `fxalpha-paper-fleet-daily.timer` 在周二至周六 07:30 提供持久化兜底；模拟交易临时失败后每 30 分钟重试。所有入口执行同一幂等 fleet 命令，因此不会重复记账。

当目标日期已完成且没有待补日期时，preflight 返回 `already_current`，fleet 直接成功结束，不创建空的账户运行或重复生成推荐。平台总览、数据底座和模拟交易页面统一读取轻量 automation status；完整 LLM/磁盘运行状态仍只由平台总览按需读取。

Qlib 执行先完整写入每日冻结目录和 execution meta，最后原子发布账户状态；SQLite 将 execution、snapshot 和 recommendation 状态放在同一个事务提交。若进程在发布账户状态后、SQLite 提交前中断，下一次单日运行会从冻结 meta 恢复提交，禁止重复成交。

恢复顺序：

1. 读取 `paper-fleet-status` 和 `paper-fleet-preflight`。
2. 若为数据健康或日期不一致，先停止交易链并修复数据链。
3. 若为 deployment 缺失，补齐明确生效边界后重新生成 plan。
4. 若 AccountRun failed，保留失败事件和冻结文件，修复根因后从同一日期重跑。
5. 不删除 SQLite 行、不手工改 account state、不绕过 production model/data gate。

## 9. 上线验收标准

- 至少一个 active 生产账户绑定实际 production model。
- 数据库旧结构迁移不丢记录，策略参数可回读。
- 多账户推荐、执行目录、snapshot 和 pending 相互隔离。
- 同一日期重跑不会重复成交或重复 snapshot。
- systemd、CLI 与 GUI 的 Fleet/Replay 写任务不能并发进入。
- 不同生产模型可在不同账户同时运行；`fixed_model` 账户再次绑定不同 `model_run_id` 必须被后端阻断并要求创建新账户。
- 长缺口必须确认，deployment 缺口必须阻断，失败日期可续跑。
- GUI、API、CLI 和 systemd 指向同一 fleet 服务。
- 旧 `/trade/*` 写入口返回 410，任何无 `account_id` 的 pending 执行返回 `paper_account_id_required`。
- paused 账户在 GUI 可见且计划冻结；retired 账户 pending 为 0 且只读。
- 目标历史区间内每个可用交易日都有 completed AccountRun；最新交易日只允许存在一个等待下个交易日的 pending。
- 不允许存在“未成交即 superseded 且账户已推进到后续日期”的推荐。
- 最终 production health、模型状态、账本行数、日期连续性和冻结文件哈希通过审计。
- 置信度账户 target 权重总和不得超过 1；边界 7 只且模型乘数 0.5 时必须为每只 2.5%、股票 17.5%、现金 82.5%。
- 执行结果必须证明 `target_weight_v2` 消费冻结目标，现金守恒，未减旧仓占用风险预算，且 target/actual 偏差有明确约束原因。
