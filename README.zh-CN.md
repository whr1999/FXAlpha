# FXAlpha

[English](README.md) | **简体中文**

一套把市场数据、因子研究、模型训练、预测和 Qlib 模拟交易连接起来的可治理量化
研究平台。GUI、CLI、MCP 和后台调度共享同一服务、门禁、注册表与审计记录。

> **先读这里：** 如果要操作平台，请读《[FXAlpha 完整使用指南](docs/USER_GUIDE.zh-CN.md)》；
> 如果要理解每个业务到底经过多少步、每一步怎样计算、什么分数才能入库或晋升，
> 请读《[业务流程与计算逻辑说明书](docs/BUSINESS_WORKFLOWS.zh-CN.md)》。后者给出
> 因子 Quick/Deep Score、Rolling、模型评分、风险控仓和 Qlib 成交的现行公式。
> 生产值守、故障处理和回滚统一从《[生产运维入口](docs/OPERATIONS_INDEX.zh-CN.md)》开始。

> **Alpha 阶段，仅用于研究和模拟交易。** 项目不提供投资建议，不保证因子或模型
> 表现，也不包含可直接暴露公网的认证网关或实盘执行合约。

## 先用五分钟理解平台

FXAlpha 解决的不是“再写一套回测脚本”，而是把容易断裂的量化研究环节变成一条
可追踪的生产链：

1. 市场数据先进入 staging，质量和血缘通过后才能晋升；
2. 因子候选经过表达式校验、评分、查重、滚动验证和质量门禁后才能入库；
3. 模型只消费带指纹的 active feature snapshot，训练完成不自动等于生产晋升；
4. 预测和组合建议只能使用已晋升模型，并检查日期与来源一致性；
5. 模拟交易按账户和模型 deployment 隔离，由 Qlib 提供交易所语义；
6. GUI、CLI、MCP 或定时器发起的写操作都落到同一服务和审计证据。

公开仓库只包含源码、测试、脱敏示例和文档，**不包含可复用的市场数据、因子值、
训练模型、账户数据库或 API Key**。下方经所有者明确批准的界面截图是时点文档记录，
不是可导入的运行资产。使用者必须在 Git 之外配置自己的合法数据和凭据。

## 功能入口与详细文档

README 负责告诉你“平台有什么、从哪里进入”；具体业务算法不在 README 中重复，
统一由《[业务流程与计算逻辑说明书](docs/BUSINESS_WORKFLOWS.zh-CN.md)》维护：数据底座
见第 2 章，因子挖掘见第 3 章，模型训练见第 4 章，预测与推荐见第 5 章，Qlib
模拟交易见第 6 章。

| 你想做什么 | GUI 页面 | 第一步 | 完整说明 |
| --- | --- | --- | --- |
| 判断平台哪里有问题 | 平台数据总览 | 查看 API、数据日期和各 lane 的 blocker | [完整使用指南](docs/USER_GUIDE.zh-CN.md) · [平台运维手册](docs/PLATFORM_OPS_RUNBOOK.md) |
| 准备和更新市场数据 | 数据底座 | 先运行只读 preflight，再 staging，最后审核晋升 | [业务逻辑第 2 章](docs/BUSINESS_WORKFLOWS.zh-CN.md) · [数据工作流](docs/DATA_FOUNDATION_WORKFLOW_CURRENT.md) · [日更手册](docs/DATA_FOUNDATION_DAILY_RUNBOOK_CURRENT.md) |
| 挖掘并审核因子 | 因子研究 / 因子库 | 查看当前 run/round/stage 和阻断原因 | [业务逻辑第 3 章](docs/BUSINESS_WORKFLOWS.zh-CN.md) · [因子操作手册](docs/FACTOR_RESEARCH_OPERATIONS.md) · [因子总合同](domain/factor_research/README.md) |
| 训练和评估模型 | 模型研究 / 模型库 | 检查 active feature snapshot 和模型 preflight | [业务逻辑第 4 章](docs/BUSINESS_WORKFLOWS.zh-CN.md) · [模型工作流](docs/MODEL_RESEARCH_WORKFLOW_CURRENT.md) · [模型模块说明](domain/model/README.md) |
| 生成预测与组合建议 | 模型研究 / 模拟交易 | 确认 production model、预测日期和来源就绪 | [业务逻辑第 5 章](docs/BUSINESS_WORKFLOWS.zh-CN.md) · [模拟交易合同](docs/PRODUCTION_MULTI_MODEL_PAPER_TRADING_CURRENT.md) |
| 运行 Qlib 模拟账户 | 模拟交易 | 先看 fleet preflight 和 replay plan，再确认写操作 | [业务逻辑第 6 章](docs/BUSINESS_WORKFLOWS.zh-CN.md) · [值守手册](docs/TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md) · [Qlib 成交合同](docs/QLIB_LIMIT_TRADING_CONTRACT_CURRENT.md) |
| 通过智能体治理平台 | MCP | 配置 QuantGPT、Model 和 Platform MCP | [平台治理 MCP](docs/PLATFORM_GOVERNANCE_MCP_CURRENT.md) · [LLM 接入](docs/LLM_INTEGRATION.md) |
| 部署或准备发布 | CLI / 文档 | 使用隔离配置启动并运行 release preflight | [本地部署](docs/LOCAL_DEPLOYMENT.md) · [GitHub 上传手册](docs/GITHUB_UPLOAD_RUNBOOK.md) |

## 系统架构与模块串联

![FXAlpha 端到端系统架构与模块数据流](docs/assets/fxalpha-system-architecture.svg)

主链路按受控资产而不是按页面串联：

- 数据底座把 Tushare staging 审核后晋升，并同时生成 QuantGPT 与 Qlib 数据视图；
- 因子研究消费 QuantGPT 数据，候选通过质量门禁后进入因子库；
- 因子库生成带 registry fingerprint 的 active feature snapshot，交给模型训练；
- 模型经过 Qlib 训练、Rolling、forward test 和晋升门禁后进入模型库；
- production model 与最新 Qlib 数据生成预测和组合建议，再由 Qlib 模拟账户执行。

更详细的边界、存储所有权和已知技术债见《[架构说明](docs/ARCHITECTURE.md)》。

## 真实系统界面

![FXAlpha 平台数据总览](docs/assets/screenshots/platform-overview.jpeg)

这是经项目所有者批准公开的时点运行界面。因子研究、模型研究和 Qlib 模拟交易的
完整截图及每个页面对应的业务含义见《[系统界面说明](docs/SCREENSHOTS.zh-CN.md)》；
具体计算逻辑仍以《[业务流程与计算逻辑说明书](docs/BUSINESS_WORKFLOWS.zh-CN.md)》
为准。

## 克隆与第一次启动

支持 Linux/WSL2、Python 3.11 或 3.12，以及 Git 子模块。

```bash
git clone --recurse-submodules https://github.com/whr1999/FXAlpha.git
cd FXAlpha
cp config.example.yaml config.yaml
./scripts/bootstrap_public_env.sh
```

如果克隆时没有初始化第三方 fork：

```bash
git submodule update --init --recursive
```

将凭据保存在被 Git 忽略的 `config.yaml`，或使用 `FXALPHA_CONFIG_FILE` 指向仓库
外的受保护配置。不要修改示例文件去存真实 Token。

启动 API 和 GUI：

```bash
PYTHONPATH=. .venv/bin/python api_server.py --host 127.0.0.1 --port 18081
```

打开 `http://127.0.0.1:18081/gui/`。第一次进入后按以下顺序：

1. 在“平台数据总览”确认 API 已在线，读取过程能够结束；
2. 在“数据底座”确认是否已有合法数据，以及 production health 是否 ready；
3. 数据未就绪时先停在数据层，不启动因子、模型或交易写操作；
4. 数据就绪后依次检查因子库、模型 feature snapshot、production model；
5. 只有交易 preflight 通过后，才创建或推进模拟账户。

源码 clone 本身没有生产数据。因此一个全新环境显示“未配置”“waiting”或
“blocked”是正常的；这比用示例数据伪装成功更安全。

## 常用只读检查

```bash
.venv/bin/python cli.py data-status
.venv/bin/python cli.py factor-status
.venv/bin/python cli.py model-status
.venv/bin/python cli.py pred-status
.venv/bin/python cli.py paper-fleet-status
.venv/bin/python cli.py paper-fleet-preflight
```

查看全部入口：

```bash
.venv/bin/python cli.py --help
```

创建 staging、导入因子、晋升模型、创建账户、运行 fleet/replay 和清理 execute
都是写操作。不要只复制命令执行；先阅读模块 preflight、确认对象与日期边界，并
按对应 runbook 操作。

## 三种操作方式

| 入口 | 推荐用途 | 不应该做什么 |
| --- | --- | --- |
| GUI | 人工观察、预检、审批、恢复和结果解释 | 不在前端复制业务公式或绕过后端门禁 |
| CLI | 本地开发、CI、只读诊断和明确的故障回退 | 不作为无审计的生产快捷脚本 |
| MCP | 受治理的因子/模型研究和平台自动化 | 工具缺失时不替换成 curl 或临时胶水脚本 |

`.codex/config.example.toml` 提供相对路径示例。个人 Codex 配置、Skill、记忆、
会话、审批设置和凭据都不属于本仓库。

## 公开仓库与本地资产边界

除上述经所有者逐张批准的静态界面记录外，本仓库不会发布：

- 市场数据集及衍生因子值；
- active 因子/模型注册表、feature snapshot 和训练模型；
- 可下载或复用的预测、推荐、持仓、账户快照、净值和收益数据；
- SQLite、任务证据、trace、日志、备份和运行目录；
- API Key、Token、`.env`、真实 `config.yaml` 或个人绝对路径；
- 个人 Codex Skill、记忆、会话和本地工具状态。

这些文件既被 Git 忽略，也会被公开树与完整 Git 历史审计拒绝。

## 文档导航

### 新用户

- [完整使用指南](docs/USER_GUIDE.zh-CN.md)
- [生产运维入口](docs/OPERATIONS_INDEX.zh-CN.md)
- [业务流程与计算逻辑说明书](docs/BUSINESS_WORKFLOWS.zh-CN.md)
- [系统界面说明](docs/SCREENSHOTS.zh-CN.md)
- [本地部署](docs/LOCAL_DEPLOYMENT.md)
- [路径与文件归属规范](docs/PATH_LAYOUT.zh-CN.md)
- [架构说明](docs/ARCHITECTURE.md)

### 平台操作

- [数据底座日更](docs/DATA_FOUNDATION_DAILY_RUNBOOK_CURRENT.md)
- [因子研究操作](docs/FACTOR_RESEARCH_OPERATIONS.md)
- [模型研究工作流](docs/MODEL_RESEARCH_WORKFLOW_CURRENT.md)
- [模拟交易值守](docs/TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md)
- [平台运维](docs/PLATFORM_OPS_RUNBOOK.md)

### 开发与 GitHub 发布

- [项目结构](docs/PROJECT_STRUCTURE_CURRENT.md)
- [生产路径规范](docs/PATH_LAYOUT.zh-CN.md)
- [工程变更护栏](docs/CODEX_ENGINEERING_GUARDRAILS.md)
- [第三方 Fork 策略](docs/THIRD_PARTY_FORKS.md)
- [安全策略与部署边界](SECURITY.md)
- [vn.py 退役说明](docs/VNPY_RETIREMENT.md)
- [发布就绪状态](docs/GITHUB_PUBLICATION_READINESS.md)
- [GitHub 上传手册](docs/GITHUB_UPLOAD_RUNBOOK.md)
- [验证报告](docs/VERIFICATION_REPORT_20260810.zh-CN.md)
- [全部当前文档索引](docs/DOCUMENTATION_INDEX_CURRENT.md)

## 发布前验证

```bash
.venv/bin/python scripts/run_release_preflight.py
```

统一闸门检查公开目录、可达 Git 历史、第三方 fork 拓扑、源码编译和完整测试。
联网 release 模式还要求固定的公开 fork 提交可以匿名获取，并通过一次全新的递归
克隆验收。

## 第三方组件、许可与安全

- QuantGPT fork 提供因子研究及 MCP 工具；
- Microsoft Qlib fork 提供模型、评估及模拟交易所语义；
- Tushare fork 提供市场数据 SDK。

各组件保留自己的许可证，详见 [NOTICE](NOTICE) 和子模块许可证。FXAlpha 当前以
“保留全部权利”的项目许可证公开源码，因此不应在现有条款下描述为开源软件。

发现漏洞请使用 GitHub 私有安全公告。不要在公开 Issue 中粘贴凭据、生产数据、
因子值、模型文件、未经审查的账户截图或私有日志。

受支持的本地部署默认只把 FXAlpha API 绑定到 `127.0.0.1`，MLflow 使用本地
`file://` 跟踪目录，不会启动 MLflow HTTP 服务。任何远程部署前请先阅读
[SECURITY.md](SECURITY.md)。
