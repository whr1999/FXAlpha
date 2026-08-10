# FXAlpha 生产模拟交易值守 Runbook

Updated: 2026-08-07

## 定位

本 runbook 只覆盖已经晋升的数据和 production model 之后的多账户 Qlib 模拟交易。完整架构与数据合同见 `PRODUCTION_MULTI_MODEL_PAPER_TRADING_CURRENT.md`。本链路不训练模型、不启动数据 staging/promote、不执行历史研究回测。

每日不需要重训：当前 production model 可连续预测多个交易日；新模型只有在模型生产晋升完成并建立带生效日期的 account deployment 后才进入模拟交易。

## 首次创建账户

```bash
cd <repo-root>
PYTHONPATH=<repo-root> .venv/bin/python cli.py paper-account-create \
  --account-id paper-prod-model-a \
  --account-mode fixed_model \
  --model-run-id <production_model_run_id> \
  --effective-from <YYYY-MM-DD> \
  --topk 20 --n-drop 2 --hold-thresh 5 --deal-price open \
  --strategy-contract-version confidence_cash_top20_drop2_hold5_open_v2
```

每个账户独立持有 100 万初始资金、持仓、pending、成交和净值。`fixed_model` 账户永久绑定首次登记的 `model_run_id`；同时运行或比较不同模型时必须分别创建新账户，不得复用同一 `account_id`。

账户展示名由模型自动生成，不填写 `--display-name`。模型登记含 `manual_promotion_exception` 时，GUI 显示“手工晋升”；来源 campaign 为 research 时同时显示“研究来源”。对已绑定的 fixed-model 账户提交不同模型会返回 `fixed_model_account_already_bound_create_new_account`。

## 每日值守

先做只读检查：

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py data-status
PYTHONPATH=<repo-root> .venv/bin/python cli.py paper-fleet-status
PYTHONPATH=<repo-root> .venv/bin/python cli.py paper-fleet-preflight
```

只有 preflight 为 `go` 时运行：

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py paper-fleet-run
```

预期结果：每个 active 账户完成到 Qlib latest；上一交易日 pending 在当日成交；当日产生下一交易日 pending。若 Qlib 尚无下一交易日，最新推荐保持 pending 且 execution_date 为空，这是等待数据而非失败。

## 数据中断后的补跑

先生成计划：

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py paper-replay-plan \
  --account-id <account_id> --from-date <YYYY-MM-DD> --to-date <qlib_latest>
```

确认计划中的交易日、跳过日期、deployment 和数据包后执行：

```bash
PYTHONPATH=<repo-root> .venv/bin/python cli.py paper-replay-run \
  --account-id <account_id> --from-date <YYYY-MM-DD> --to-date <qlib_latest> \
  --confirm-long-replay
```

回放会逐日补齐预测、推荐、成交和 snapshot；已 completed 的日期自动跳过。旧的 `trade-paper-backfill` 只补市值，不是生产历史补跑入口。

## 必须阻断的情况

- HDF5/Qlib/QuantGPT 最新日期不一致或 production health 非 ready。
- production model validation 非 ready。
- 目标日期没有有效 deployment。
- prediction/score 更新失败、为空、常量或 PIT 身份覆盖不足。
- pending 执行失败。
- 超过 5 个交易日的缺口未显式确认。

禁止绕过 gate、手改 SQLite/JSON 状态、删除失败记录，或从交易链启动数据更新和模型训练。

低唯一值或 Top20 边界同分对旧合同仍是阻断；对 `confidence_cash_*` 合同属于弱信号。值守时必须核对逐日 `confidence_state`、`selected_count`、`target_stock_exposure` 和 `target_cash_weight`，不能为了满仓从边界同分股票中任意补足 20 只。

新推荐还必须核对 `metrics.risk_policy`：配置指纹、三指数共同 `as_of_date`、20/60日广度与波动、市场压力确认状态、账户滚动回撤、模型/市场/账户三层仓位上限以及最终 binding layer。市场数据不齐时推荐应失败，禁止关闭风控后补生成。风控配置通过 `GET/POST /trade/risk-policy` 或 GUI 控制台维护；POST 必须 `confirm: true`，且只影响下一份新推荐。

## 证据与排障

检查顺序：

1. `paper-fleet-status` / `paper-fleet-preflight`。
2. `./runtime/trading/fleet/latest_status.json`。
3. `./data/trading/execution_log.db` 中的 fleet/account run、recommendation、execution、snapshot 表。
4. `./runtime/trading/paper_trading/{account_id}/executions/` 下的冻结文件。
5. `./runtime/trading/recommendations/{model_run_id}/{account_id}/` 下的 score、target、orders 和 recommendation JSON。
6. `./runtime/trading/risk_policy.json` 与每份推荐旁的 `risk_decision_<signal_date>.json`。

AccountRun failed 时，先读取 `paper_run_events` 的失败 stage 和 error；修复根因后重跑同一区间，系统从失败日期续跑。

若返回 `paper_operation_in_progress`，读取 `./runtime/trading/fleet/paper_operation.lock` 的持有进程并等待当前任务结束。不得删除锁文件或并发启动第二个回放。

## GUI 值守

模拟交易页必须同时展示：

- fleet 状态和数据最新日期；
- 多账户模型、资金、净值、收益、持仓、pending 和缺口横向比较；
- 模型自动名称以及“手工晋升/研究来源”等真实来源标签；
- 单账户历史净值、推荐、成交、持仓和冻结文件；
- replay 计划的新增/跳过/阻断日期；
- 三层置信度、目标/实际仓位、目标/实际现金及执行约束；
- 三层风控仓位上限、市场压力、账户回撤、binding layer 与生效配置；
- 高级区的账户创建、fleet run 和 replay run。

生产 Top20 / Drop2 / Hold5 / open 和初始资金在 GUI 中只读。写 API 必须携带 `confirm: true`。

## 收班标准

- fleet run 为 `completed`，没有 `partial_failed` 账户。
- 每个 active 账户在 Qlib latest 有 completed AccountRun 和 snapshot。
- 最新推荐属于该账户当日 deployment；最多保留一个合法 pending。
- 数据、模型、预测、账本和冻结文件均可追溯。
- 置信度账户的目标权重合计不超过 1，实际偏差均能归因于 hold5、不可交易或渐进加仓限制。
