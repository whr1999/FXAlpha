# FXAlpha 业务流程与计算逻辑说明书

[English](BUSINESS_WORKFLOWS.md) | **简体中文**

本文不是页面介绍，而是 FXAlpha 当前实现的业务合同：每个模块接收什么、经过多少
步骤、如何计算、什么条件允许继续、最终写入什么受控资产。新用户应先读本文，再按
具体任务进入对应 runbook。

> 适用边界：研究与模拟交易。公开仓库不包含市场数据、因子值、特征快照、训练
> 模型、账户状态或凭据。文中的日期、分数只用于解释算法，不代表任何生产结果。

## 1. 一条主链、五类受控资产

```text
Tushare 原始数据
  -> staging 数据包 -> production 数据集
  -> 因子候选 -> active 因子注册表与因子值
  -> 带注册表指纹的 feature snapshot
  -> research/candidate/production 模型
  -> 每日 prediction score -> target portfolio -> pending recommendation
  -> Qlib 模拟成交 -> 持仓、现金、净值与审计快照
```

业务上的关键点不是“某段程序运行结束”，而是受控资产是否完成了门禁与提交：

| 上游状态 | 不等于 | 真正的下游就绪条件 |
| --- | --- | --- |
| staging 构建完成 | production 数据可用 | 晋升成功，`post_promote_audit.status=passed`，`production_health.status=ready` |
| 因子研究 run 完成 | 因子已入库 | quality gate adopted，数值覆盖通过，Registry 有 active 行 |
| 模型训练完成 | production 模型可用 | 正式 Rolling 候选通过，生产重训及验证通过，active production pointer 已切换 |
| prediction 已生成 | 当日已交易 | 推荐仍是 pending；到 execution date 后由账户日流程执行 |
| 单个账户完成 | fleet 全部完成 | 每个 active 账户均完成且 fleet 后审计通过 |

GUI、CLI 和 MCP 只是三个入口。它们都调用 `services/`，最终由 `domain/` 的同一套
计算和门禁决定结果；前端不复制评分公式，也不能覆盖后端拒绝。

## 2. 数据底座：从供应商到可消费生产数据

### 2.1 生产日更共有 11 个业务步骤

| 步骤 | 输入 | 主要运算 | 输出 / 继续条件 |
| --- | --- | --- | --- |
| 1. 生产指针读取 | 当前 dataset manifest | 解析生产包、HDF、Qlib、QuantGPT 和最新交易日 | 指针及各路径必须存在且相互一致 |
| 2. 日更预检 | 当前最新日、目标日、Tushare 交易日历 | 选择已闭合目标交易日，计算 `replace_from_date` 与短窗 | 网络、凭据、内存、磁盘、锁、日期均可用 |
| 3. 短窗源重建 | 预检计划、Tushare APIs | 下载日线、daily basic、复权、财务慢字段、指数与交易约束字段 | 独立 source staging 包；不触碰 production |
| 4. 源兼容输出 | source silver 表 | 规范字段、价格、指数和元数据 | source compat HDF、日历、质量引用 |
| 5. 全历史合并 | production HDF + source delta | 删除 `replace_from_date` 之后旧窗口，拼入新窗口，临时文件校验后替换 staged 输出 | staged full-history HDF |
| 6. staged 日历 | staged HDF | 从有效交易日期去重排序 | staged `trade_calendar.txt` |
| 7. 下游视图构建 | staged HDF + production seed | Qlib 按窗口补丁更新，QuantGPT 重新导出所需股票与基准数据 | staged Qlib 与 QuantGPT 视图 |
| 8. staged 质量门 | staged 数据及源窗口报告 | `daily_compat` 检查刷新窗口；`deep_full` 检查完整生产表 | 两类报告都必须完整且通过 |
| 9. 原子晋升 | 完成的 staged package | 写晋升 journal、备份被替换资产、逐项替换、做 staged/production 等价检查、最后提交指针 | `promoted`；中途失败可按 journal 恢复 |
| 10. 晋升后生产审计 | 新 production 资产 | 按 pointer、日期、模式、消费者和抽样数值做分层审计 | `post_promote_audit.status=passed` |
| 11. 清理预览 | staging、backup、cache 状态 | 只计算可回收对象与保护原因 | 只生成 dry-run；不自动删除 |

日更控制器在 GUI 中压缩为 6 个可观察 stage：`source_rebuild`、
`source_prepare_production`、`merge_production_hdf`、`merged_quality_check`、
`build_compat_outputs`、`completed`。它们是进度投影，不减少上表的业务验收步骤。

### 2.2 关键数据计算

- 复权研究价：原始 OHLC 乘以本地 `adj_factor` 形成 HFQ 研究列；不存在逐股票
  `pro_bar` 下载阶段。
- 正式 `pre_close`：优先使用 Tushare `stk_limit.pre_close`；只有官方字段缺失时
  才退回上一交易日收盘价。
- 慢字段：财务、股东、融资融券、筹码和资金流字段按可获得日期做 PIT 对齐，不能
  把未来披露值回填到过去。
- 缺失值：保留为 `NaN` / `pd.NA` / `NaT`；不能用零静默伪装完整性。
- Qlib：由 raw price 加显式 factor 生成；QuantGPT 使用调整后的研究价格。

质量门不仅检查“文件存在”，还检查行数、日期覆盖、代码覆盖、字段 schema、缺失率、
零价格、指数、涨跌停、复权一致性、上市/ST 元数据和下游日期对齐。完整字段与阈值
以 [`quality_check.py`](../domain/data_foundation/quality_check.py) 和
《[数据底座工作流](DATA_FOUNDATION_WORKFLOW_CURRENT.md)》为准。

## 3. 因子挖掘：从经济主线到 active 因子

### 3.1 标准成功路径共有 11 个阶段

这里的“11 个阶段”包含 1 个启动装载阶段和每轮 10 个业务阶段。失败可以回到
表达式、假设或主线层；系统错误进入 blocker，不会被记成因子质量差。

| # | 阶段 | 输入 | 本阶段运算与判断 | 正常输出 |
| --- | --- | --- | --- | --- |
| 0 | `protocol_load` | 运行配置、工具清单、预算、历史 checkpoint | 固定协议、支持字段/算子、日期窗、holding period 和恢复位置 | 进入本轮设计；不产生候选分数 |
| 1 | `thesis_design` | 研究目标、可用字段族、Factor Map、最近三轮事实 | 提出 1–3 个经济主线；判断主信息来源、作用机制和已有关系是否真正相同 | economic theses |
| 2 | `hypothesis_design` | 单个 thesis、字段/算子约束、上轮 handoff | 把主线变成可证伪关系，明确 main signal、confirmation、risk control 和方向 | 1–4 个 hypotheses |
| 3 | `expression_design` | hypothesis、算子签名、候选预算、禁止重复表 | 实现少量表达式；逐腿验算高值含义、负号、`where` 分支、窗口与复杂度 | 1–5 个 candidate drafts；定向修复仅 1–2 个 |
| 4 | `candidate_plan` | drafts、代码静态预检、语义说明 | 逐候选检查字段、语法、方向、完全重复和参数型批内重复 | 合法候选进入 Quick Score；其余回 expression/hypothesis |
| 5 | `score_review` | 表达式校验、正式 Quick Score 与回测摘要 | A/B 原则上推进；C/D 不进入 novelty；负 signed RankIC 只允许一次全局反号修正 | keepers 进入 novelty，或按 parent 价值回上游 |
| 6 | `novelty_review` | keeper、active 因子值、同批候选、ST 证据 | 计算 Pearson、Spearman 与 p90 拥挤度；执行 ST/combined guard | 新颖候选进入 deep；拥挤候选正交化或换主线 |
| 7 | `deep_validation_review` | 完整回测、anti-overfit、Rolling、adversarial | 检查四项数字证据、IC/IR、复杂度、Deep Score 与缺口 | gate-ready 候选提交质量门；否则补证据或定向变异 |
| 8 | `import_gate_review` | 正式 quality gate、名称和 metadata | 只接受代码判定 adopted；LLM 不得覆盖 gate reject | adopted 候选进入 import；其他回 deep/表达式或结束 |
| 9 | `import_review` | import result、Registry 与数值同步结果 | 确认真实 `imported`、factor_id、数值文件和同步状态 | 已入库引用；工程失败进入 repair/blocker |
| 10 | `round_synthesis` | 本轮正式结果、候选轨迹、最近历史 | 总结可保留机制、失败关系和下轮返回层级 | 下一轮从 thesis/hypothesis/expression 开始，或 checkpoint stop |

候选可以按以下三条可解释路径迭代：

- `EXPLOIT`：保留有证据的 parent，只改变一个可归因角色；
- `RECOMBINE`：组合互补 parent；跨信息关系回 hypothesis，同假设内重组回 expression；
- `EXPLORE`：当前机制无 parent 价值时才回 thesis，选择新主信息来源。

### 3.2 表达式和标签如何计算

候选表达式先由代码解析器校验字段与算子。时间序列算子必须先使用股票的完整历史
计算，再裁剪输出日期；横截面算子在当日可交易、非 ST 的固定口径内计算。默认正式
评分参数是：

```text
universe = tradable_non_st
holding_period = 5
n_groups = 5
top_frac = 0.20
cost_rate = 0.003
benchmark = hs300
neutralize_cap = true
neutralize_industry = false
```

正式标签是信号日之后的 T+5 收益。Rolling v2 使用同一个“日历日期 T+N 收盘价”
合同；不能用每只股票简单行移位替代，因为停牌会改变真实 horizon。

每天的 IC 是因子值与 forward return 的相关性；RankIC 是 Spearman 横截面相关性，
IR 是日度 IC 序列的均值除以标准差。回测同时记录多空收益、单调性、top group
年化收益、Sharpe、最大回撤和换手率，但最终入库还必须满足 long-only 可用性。

### 3.3 Quick Score 如何计算

Quick Score 由 8 个 0–100 分量加权：

```text
Quick = 0.20 * IC_mean_score
      + 0.20 * IC_IR_score
      + 0.10 * RankIC_mean_score
      + 0.10 * RankIC_IR_score
      + 0.15 * AnnualReturn_score
      + 0.10 * Sharpe_score
      + 0.10 * MaxDrawdown_score
      + 0.05 * Turnover_score
```

归一化规则如下，所有线性分数都截断在 `[0, 100]`：

| 分量 | 100 分参考 | 具体规则 |
| --- | --- | --- |
| `IC_mean` | `abs(IC)=0.04` | `clip(abs(IC)/0.04*100)` |
| `IC_IR` | `abs(IR)=0.50` | `clip(abs(IR)/0.50*100)` |
| `RankIC_mean` | `abs(RankIC)=0.08` | `clip(abs(RankIC)/0.08*100)` |
| `RankIC_IR` | `abs(RankICIR)=0.75` | `clip(abs(RankICIR)/0.75*100)` |
| 年化收益 | `18%` | 只奖励正收益：`clip(max(r,0)/0.18*100)` |
| Sharpe | `0.65` | 只奖励正值：`clip(max(s,0)/0.65*100)` |
| 最大回撤 | `<=10%` | 100 分；`>=40%` 为 0；中间线性下降 |
| 换手率 | `10%..28%` | 区间内 100；低于 10% 按比例上升；28%..60% 线性下降；`>=60%` 为 0 |

等级是 A `>=85`、B `>=70`、C `>=55`、D `<55`。只有 A/B 原则上进入新颖性
检查。若 long-only 年化收益或 Sharpe 为负，即使加权分更高也会被封顶为 59.9、
等级 C，因此不能靠多空方向的好看结果进入深验。

### 3.4 新颖性为什么是门禁而不是加分项

候选同时与 active 因子池及本批候选比较。默认硬阈值为：

```text
max_existing_pearson < 0.75
max_existing_rank_corr < 0.80
p90_pearson < 0.70
p90_rank_corr < 0.75
```

任一达到阈值，或 `novelty_guard.allowed=false`，都会触发
`novelty_correlation_veto`。`novelty_score` 会保留在证据中，但不进入 Deep Score，
避免“高新颖性”补偿低质量。ST guard 可配置为 hard 或 advisory：hard 失败阻断，
advisory 只记录风险标签。

### 3.5 深度验证的四类证据

#### A. Anti-overfit（4 项测试）

1. IC stability：年度方向一致性、日度正 IC 比例和 `abs(IC)`；
2. Sub-sample stress：不同市场子样本的一致性与相对离散度；
3. Placebo：随机置换 95 分位与时间移位后的衰减；
4. Half-life：2–15 日半衰期和跨 horizon 衰减曲线。

四项子分权重为 `30% / 25% / 25% / 20%`。推荐等级为：`>=80 推荐`、
`>=60 谨慎`、`>=40 需改进`、`<40 不推荐`；最终质量门把分数 `<50` 或明确
“不推荐/reject/fail”视为 veto。

#### B. Rolling v2（带方向的跨期稳定性）

最近 48 个月分为五个不重叠区间：`0–6`、`6–12`、`12–24`、`24–36`、
`36–48` 月，基础权重为 `0.40 / 0.25 / 0.15 / 0.12 / 0.08`。至少需要 24
个月历史，前三个区间强制存在；每 6 个月默认至少 60 个有效 RankIC 日期。缺少可选
旧区间时剔除并重归一化权重。

```text
weighted_ic  = sum(effective_weight_i * signed_rank_ic_i)
weighted_std = sqrt(sum(effective_weight_i * (rank_ic_i - weighted_ic)^2))
robust_ic    = weighted_ic - 0.25 * weighted_std
rolling_score = clip(robust_ic / 0.08 * 100, 0, 100)
```

这里永远不取 `abs(IC)`、不在 Rolling 内翻向。近期负 IC、弱 IC、旧区间负贡献和
稳定性惩罚会形成 risk flags；低 Rolling 通过 20% 权重降低 Deep Score，但本身不
另设 hard veto。

#### C. Adversarial（4 项破坏性测试）

执行 label permutation、temporal block shuffle、random universe 和 noise
injection。四个子分等权平均为 adversarial score；`>=60` 才视为通过。其目的不是
证明因子必然有效，而是验证随机标签、时序破坏、换样本和加噪后信号是否按预期退化。

#### D. 完整正式回测

必须有非空 backtest summary、合法数值、与候选一致的 holding period，并至少提供
IC、ICIR、RankIC、RankICIR、年化收益、Sharpe、回撤和换手证据。诊断型的长短收益
或自相关不能替代正式门禁指标。

### 3.6 最终入库评分与硬门禁

Deep Score 的唯一正式公式是：

```text
Deep = 0.55 * Quick
     + 0.15 * AntiOverfit
     + 0.20 * Rolling
     + 0.10 * Adversarial
```

四个数值分量缺一时 Deep Score 记为 0；novelty 是 admission guard，不占分。候选
必须同时满足：

```text
Deep Score >= 80
abs(RankIC_mean，缺失时用 IC_mean) >= 0.02
abs(RankIC_IR，缺失时用 IC_IR) >= 0.30
Adversarial score >= 60
Anti-overfit 没有失败
Novelty / hard ST / combined guard 通过
表达式、回测、holding period、数值证据完整
long-only annual return >= 0 且 Sharpe >= 0
```

例如：

```text
Quick=88, AntiOverfit=82, Rolling=79, Adversarial=75
Deep = 88*0.55 + 82*0.15 + 79*0.20 + 75*0.10
     = 48.40 + 12.30 + 15.80 + 7.50
     = 84.00
```

84 分只说明分数条件通过。如果 RankIC=0.018、RankICIR=0.42，仍因 IC 低于 0.02
拒绝；如果 novelty 相关性越线也仍拒绝。反过来，IC/IR 很高但 Deep=78，同样不能
入库。最终决定是“分数阈值 AND 所有硬门禁”，不是二选一。

### 3.7 入库动作到底写了什么

Quality gate adopted 后，auto-import 仍执行第二层提交检查：gate score 与 deep
score 必须一致、四个分量必须存在、正式 Rolling 和 Registry 指标必须齐全、表达式
不能与 active 因子重复。随后：

1. 使用完整股票历史计算因子，再裁剪目标值窗口；
2. 审计日期覆盖和每日有效值数量；
3. 保存独立因子值 parquet；
4. 生成或修复可读 factor name、分类和不冲突的 `data_column`；
5. 向 Factor Registry 写入 `status=active`、表达式、universe、holding period、
   Quick/Deep/IC/IR/回测/novelty/深验/血缘和数值路径；
6. 历史 wide store 不在本事务中重写；active-values worker 在 Registry 提交后刷新
   当前 active feature store，并写 registry fingerprint。

因此，“研究 completed”“gate adopted”和“imported=1”是三个不同状态。只有最后一个
加上 Registry/数值证据，才是完成入库。

## 4. 模型训练：从 feature snapshot 到 production model

### 4.1 输入快照

模型不读取任意“最新因子文件”。Feature Set Builder 从 active Factor Registry
读取因子定义与值，生成 manifest、组合特征文件和 `registry_fingerprint`。预检必须
确认：数据 production ready、快照日期达到训练边界、factor IDs/列/文件一致、
fingerprint 未陈旧、label 与持有期合同匹配。

默认标签是 `LABEL0`，按 next-open 到 forward-open 的 5 日收益；组合合同是
top20 / drop2 / hold5 / open deal price，基准为沪深 300。

### 4.2 研究、确认、Rolling 和晋升

一条完整成功路径如下：

| 阶段 | 主要运算 | 业务结果 |
| --- | --- | --- |
| protocol/context/feature snapshot | 加载合同、历史、production 数据与快照血缘 | 固定本 session 的数据和特征输入 |
| experiment plan | 生成基准轮或单一可归因的参数实验 | 带 signature 的 round |
| Seed 42 train/backtest | Qlib LGBM 训练、early stopping、预测与含成本组合回测 | `pred.pkl`、`ret.pkl`、metrics、manifest |
| research score | 归一化 IR、超额收益、回撤、RankIC/IR | research 资产；不等于 candidate |
| registry write | 写 research 模型及完整 artifact refs | 可审计研究行 |
| round synthesis | 与会话最优轮比较；无提升时决定继续或停止 | 下轮参数动作或最优轮 |
| research confirmation | 仅对会话最优轮补跑 Seed 17、83 | 固定 `42/17/83` 三种子确认 |
| production Rolling | 三个种子分别做 4 折 expanding、6m valid、6m test、5 日 purge | 每折和 stitched 组合证据 |
| Rolling gate | 计算 campaign 分并执行稳定性硬门 | candidate 或 research |
| production refit | candidate 用固定 Seed 42、随最新快照平移的 train/valid 段重新训练 | 全新 production artifact，不直接复用测试模型 |
| production validation/pointer | 审计 manifest、metrics、pred、ret 与 lineage 后原子写 pointer | 唯一 active production model |

### 4.3 研究分如何计算

先将指标线性映射到 0–100：

```text
IR_score       : 0.50 -> 0, 1.50 -> 100
Return_score   : 10%  -> 0, 60%  -> 100
Drawdown_score : 10%  -> 100, 30% -> 0
RankIC_score   : 0.02 -> 0, 0.05 -> 100
RankICIR_score : 0.20 -> 0, 0.50 -> 100
RankSignal     = 0.5*RankIC_score + 0.5*RankICIR_score
ResearchScore  = 0.40*IR_score + 0.30*Return_score
               + 0.20*Drawdown_score + 0.10*RankSignal
```

含成本超额年化收益 `<10%` 或含成本超额 IR `<0.50` 是 research hard flaw。普通轮只
跑 Seed 42；系统不会从三个测试种子中挑最好看的一个。

### 4.4 正式 Rolling 候选分如何计算

每个 fold 的组合质量：

```text
PortfolioQuality = 0.45*IR_score + 0.35*Return_score + 0.20*Drawdown_score
```

单种子有且仅有 4 折：

```text
SeedRolling = 0.55*overall + 0.25*worst_fold + 0.20*latest_fold
```

正式 campaign 必须包含 Seed `{42,17,83}`。先对各 seed overall 取中位数、对每个
fold 跨 seed 取中位数，再计算：

```text
CampaignRolling = 0.55*overall_median
                + 0.25*worst_fold_median
                + 0.20*latest_fold_median
```

候选要求 `CampaignRolling >= 70` 且所有稳定性门同时通过：至少两个 stitched IR
为正、IR 标准差不超过配置上限、收益标准差不超过上限、中位回撤不超过上限、至少
三个 fold 的中位 IR 为正、最新 fold IR 为正。分数过线但任一硬门失败仍不是
candidate。

## 5. 预测和组合建议：从 production model 到 pending

标准流程共有 8 步：

1. 解析 active production pointer 和 Registry 行；
2. 核对 model run、feature set、artifact、数据日期和 lineage；
3. 对陈旧因子按完整 warm-up 历史重算 runtime-only feature cache，不修改训练快照；
4. 用 production model 生成目标日横截面 score；
5. 拒绝退化分数：记录数足够时，unique score 必须达到
   `min(max(topk*3,20), max(record_count//20,1))` 且标准差不能近零；
6. 用目标日 PIT 身份信息排除退市/ST；匹配覆盖率必须至少 95%；
7. 排序、置信度控仓和三层风险控仓，生成 target portfolio；
8. 生成唯一 `recommendation_id`、订单预览和冻结的合同/风险证据，状态写为 pending。

默认置信度合同在 top-k 边界并列时只选择严格高于边界的股票，不用证券代码顺序
破同分。若模型只构建不超过 1 棵树，模型 multiplier 为 0.5；否则为 1。当前表现
multiplier 默认为 1：

```text
exposure_multiplier = min(model_multiplier, performance_multiplier)
slot_weight = exposure_multiplier / topk
model_stock_cap = slot_weight * selected_count
```

随后三层风险上限取最小值：

```text
final_stock_cap = min(model_stock_cap, market_cap, account_cap)
```

市场压力要求沪深300、中证500、中证1000在 20 日和 60 日口径的上涨宽度都不超过
1/3，且最大年化波动不低于 18%；连续 2 日进入、连续 3 日退出，压力期 market
cap 为 75%。只有市场压力同时成立且账户 60 日窗口回撤达到 -8% 时，account cap
降为 50%。shadow 模式只记录不缩仓；enforced 模式按比例缩放所有目标权重。

## 6. Qlib 模拟交易：pending 如何变成成交和净值

FXAlpha 当前不使用 vn.py。每个 active 账户绑定明确的 model deployment 和策略
合同，标准单日顺序是：

```text
账户完整性检查
  -> 执行 execution_date <= 当日的旧 pending
  -> 发布成交后账户状态
  -> 生成当日 score / target / 新 pending
  -> 账户日后审计
  -> 标记 account run completed
```

信号日 T 生成的推荐默认在 Qlib 日历的下一交易日 T+1 执行。没有 T+1 日历时保持
pending，不能用 T 日价格提前成交。执行时：

- 目标股数约为 `target_weight * pretrade_account_value / deal_price`，再按 Qlib
  trade unit 向下取整；A 股通常是 100 股，但以 Exchange factor 为准；
- 默认 open cost 0.15%、close cost 0.25%、最低费用 5；
- 默认 top20、每日最多引入 drop2 对应的新标的、hold5；
- 风险缩仓可以覆盖战略 `n_drop/hold5`，但不能覆盖停牌、涨跌停、交易单位、价格、
  现金或可交易性限制；
- 先卖后买；每笔 order、fill、constraint、cash、position 和 hash 都写入审计资产；
- 同一 account/date/config 生成确定性 account run id，已完成运行返回
  `already_completed`，避免重复记账。

Fleet 只是依次治理多个隔离账户，不混合资金和持仓。replay 超过 5 个交易日必须显式
确认；任一账户 blocker 都保留具体 account、date、stage 和恢复证据。

## 7. 应该到哪里看“真相”

| 问题 | 先看 | 最终证据 |
| --- | --- | --- |
| 数据是否真的可用 | `data-status` / GUI 数据底座 | production pointer + 同包 post-promote audit + production health |
| 因子为什么没入库 | `factor-orch status` | stage evidence + quality gate + import result + Factor Registry |
| 模型是否可上线 | `model-status` | feature fingerprint + research confirmation + Rolling campaign + production refit validation |
| 为什么没推荐 | `pred-status` / trading preflight | production model/date/provenance + score diversity + ST/risk evidence |
| 为什么没成交 | paper account/fleet status | pending execution date + Qlib constraint/fill + account run audit |

最常见的误判是只看顶层 `completed`。每一条链都应继续检查最末端受控资产及其
审计状态。

## 8. 文档分层与代码权威

- 本文：业务步骤、运算逻辑、公式和验收条件；
- [`USER_GUIDE.zh-CN.md`](USER_GUIDE.zh-CN.md)：页面、入口和日常使用顺序；
- [`DATA_FOUNDATION_WORKFLOW_CURRENT.md`](DATA_FOUNDATION_WORKFLOW_CURRENT.md)：数据实现与运维；
- [`domain/factor_research/README.md`](../domain/factor_research/README.md)：因子领域合同；
- [`domain/factor_research/ORCHESTRATOR_README.md`](../domain/factor_research/ORCHESTRATOR_README.md)：因子状态机；
- [`domain/model/README.md`](../domain/model/README.md)：模型领域合同；
- [`PRODUCTION_MULTI_MODEL_PAPER_TRADING_CURRENT.md`](PRODUCTION_MULTI_MODEL_PAPER_TRADING_CURRENT.md)：账户与 fleet；
- [`TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md`](TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md)：交易值守；
- [`ARCHITECTURE.md`](ARCHITECTURE.md)：模块边界和系统架构。

若文档和代码出现冲突，当前 `domain/` 计算、`services/` 路由和合同测试为执行权威；
这应被视为文档缺陷并修正文档，而不是让操作者猜测。
