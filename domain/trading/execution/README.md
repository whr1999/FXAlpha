# Trading execution 模块说明

Updated: 2026-08-09

本目录是 FXAlpha 唯一的生产模拟交易执行层，执行内核为 Qlib
Exchange/Position。不存在第二执行引擎或本地 fallback。

## 生产主路径

```text
services/paper_fleet_service.py
  -> services/trading_service.py
  -> domain/trading/execution/qlib_paper.py
```

正式入口：

- CLI：`paper-fleet-status`、`paper-fleet-preflight`、`paper-fleet-run`、`paper-replay-plan`、`paper-replay-run`
- HTTP：`/paper/fleet/*`、`/paper/replay/*`、`/paper/accounts`
- 账户、deployment、run、推荐、执行和 snapshot：`storage/trading_registry.py`

`qlib_paper.py` 负责 Qlib Exchange 交易约束、成交、持仓、现金、每日账本和冻结文件；fleet service 负责多账户编排、部署边界、幂等、断点续跑和审计。

## 依赖边界

- `qlib_paper.py` 只从活动 Python 环境导入 Qlib。
- 多账户、deployment、日切、replay 和幂等位于 `services/paper_fleet_service.py`。
- 账本、推荐、执行和 snapshot 证据位于 `storage/trading_registry.py`。
- 生产数据和运行状态不属于 Git 仓库。

完整契约见 `./docs/PRODUCTION_MULTI_MODEL_PAPER_TRADING_CURRENT.md`。
