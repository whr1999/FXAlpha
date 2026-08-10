# FXAlpha 路径与文件归属规范

本文规定代码、生产数据、运行证据、配置、备份和历史归档分别由哪个目录负责。
目标是让发布版本可替换、生产资产不随代码发布移动，并让每个文件只有一个权威位置。

## 1. 推荐的生产目录

```text
/home/USER/
├── fxalpha-deploy/
│   ├── current -> releases/<git-commit>/
│   └── releases/<git-commit>/       # 只读发布代码和固定的 third_party
├── fxalpha-data/                    # 持久业务数据
│   ├── raw/                         # Tushare 原始/兼容数据
│   ├── qlib/                        # Qlib provider 数据
│   ├── quantgpt/                    # 因子研究数据视图
│   ├── factors/                     # 因子注册表和 active values
│   ├── model/                       # feature snapshots、模型注册表和模型产物
│   ├── trading/                     # 模拟交易持久注册表
│   └── metadata/                    # 股票身份等共享缓存
├── fxalpha-state/
│   ├── runtime/                     # 任务、状态、trace、锁、日志和审计证据
│   ├── quantgpt/                    # QuantGPT SQLite、报告和研究笔记
│   ├── backups/                     # 受治理的恢复点
│   ├── operations/                  # 本机迁移台账和运维记录
│   └── test-tmp/                    # 可清理的测试临时文件
├── fxalpha-archive/                 # 已退役发布和人工确认的历史快照
└── .config/fxalpha/
    ├── config.yaml                  # 0600，真实路径与本地凭据
    └── runtime.env                  # 0600，服务环境变量
```

开发 clone 可以位于任意目录，但不得作为长期生产服务的工作目录。生产服务只执行
`fxalpha-deploy/current`；数据、状态和凭据不得写进 release。

## 2. 配置解析规则

`storage/paths.py` 是代码权威。路径优先级如下：

1. 具体文件或子目录覆盖项，例如 `factor_registry_db`；
2. `paths.data_root` 或 `paths.runtime_root` 派生的标准路径；
3. 未配置 clone 的仓库内 `data/`、`runtime/` 安全默认值。

因此新部署通常只需要一个数据根和一个运行根：

```yaml
paths:
  data_root: /home/USER/fxalpha-data
  runtime_root: /home/USER/fxalpha-state/runtime
  third_party_root: /home/USER/fxalpha-deploy/current/third_party
  quantgpt_code_root: /home/USER/fxalpha-deploy/current/third_party/quantgpt
  qlib_source_root: /home/USER/fxalpha-deploy/current/third_party/qlib
  quantgpt_db: /home/USER/fxalpha-state/quantgpt/quantgpt.db
  quantgpt_research_notes_dir: /home/USER/fxalpha-state/quantgpt/research_notes
```

`data_root` 自动派生 `raw/tushare`、`qlib`、`quantgpt`、`factors`、`model`、
`trading` 和 `metadata`。旧配置中的细粒度绝对路径继续有效，以便无停机兼容迁移；
完成核对后再逐项删去冗余覆盖项。

相对路径始终相对于 release 根目录，而不是调用命令时的当前目录。生产配置应使用
绝对路径，并由 `FXALPHA_CONFIG_FILE` 指向仓库外的 `config.yaml`。

## 3. 文件归属与生命周期

| 类别 | 权威位置 | Git | 备份/清理原则 |
| --- | --- | --- | --- |
| 源码、测试、公共文档 | release / 开发 clone | 纳入 | 由 Git commit 和 tag 管理 |
| 第三方源码 | release 的 `third_party/` 子模块 | 仅 Gitlink | 固定 commit，禁止写运行资产 |
| 原始、Qlib、因子、模型、交易数据 | `fxalpha-data/` | 排除 | 数据集/注册表按业务一致性备份 |
| 任务状态、trace、锁和日志 | `fxalpha-state/runtime/` | 排除 | 只通过维护服务的 retention policy 清理 |
| API Key 和环境变量 | `.config/fxalpha/` | 排除 | 权限 0600，不复制到 Issue 或日志 |
| SQLite 与关键清单备份 | `fxalpha-state/backups/` | 排除 | 使用 SQLite 在线 backup，并保存 SHA-256 |
| 退役发布与遗留快照 | `fxalpha-archive/` | 排除 | 先移动归档，验证恢复窗口后再决定删除 |

`docs/` 只保存当前公开产品文档。机器专属路径、切换时间、数据库校验和恢复脚本属于
`fxalpha-state/operations/` 或 `fxalpha-state/backups/`，不能进入公开仓库。

## 4. 安全迁移顺序

1. 记录当前 release、服务、timer、任务 `run_id` 和所有权；
2. 通过业务控制面让写任务在安全检查点暂停，并停止 timer；
3. 在线备份 SQLite、配置、systemd unit、MCP 配置和关键 manifest；
4. 在同一文件系统内原子移动 `data` 与 `runtime`；
5. 为仍含旧绝对路径的历史 manifest 保留明确的兼容符号链接；
6. 更新外部配置，先做导入/路径检查，再启动新 release；
7. 检查 API、数据、因子、模型、预测和模拟交易各 lane；
8. 恢复 timer，并从原 `run_id` 的持久检查点继续任务；
9. 观察期内保留前一个 release 和迁移前备份。

不得在写任务运行中移动目录，不得手工删除 active lock，不得用一个“看起来健康”的
API 状态代替数据、模型或交易 lane 的独立验证。

## 5. 本地开发与公开仓库边界

公开仓库允许提供目录结构和脱敏配置示例，但不得包含真实数据、因子值、模型、账户、
SQLite、日志、备份、绝对个人路径或密钥。完整边界见
[`SECURITY.md`](../SECURITY.md)，部署流程见 [`LOCAL_DEPLOYMENT.md`](LOCAL_DEPLOYMENT.md)，
运行文件清单见 [`RUNTIME_AND_DATA_PATHS_CURRENT.md`](RUNTIME_AND_DATA_PATHS_CURRENT.md)。
