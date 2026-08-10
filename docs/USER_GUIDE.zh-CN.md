# FXAlpha 使用指南

[English](USER_GUIDE.md) | **简体中文**

这份指南回答一个最实际的问题：拿到 FXAlpha 源码以后，应该从哪里开始，如何把
数据、因子、模型和模拟交易串成一条可审计的工作流。

本指南解释“怎样操作”。如果要继续追到“每个模块共有多少步、每一步怎样计算、
因子如何评分入库、模型如何晋升、风险仓位怎样得到”，请同时阅读
《[业务流程与计算逻辑说明书](BUSINESS_WORKFLOWS.zh-CN.md)》。

> FXAlpha 是研究与模拟交易平台，不是开箱即用的数据包或实盘交易客户端。公开
> 仓库只提供源码、测试、脱敏配置示例和文档；市场数据、因子值、训练模型、账户
> 状态和凭据必须由使用者在 Git 之外自行准备。

## 1. 先认识三个入口

| 入口 | 适合谁 | 主要用途 | 是否建议执行写操作 |
| --- | --- | --- | --- |
| Web GUI | 研究员、平台值守人员 | 看平台总览、检查门禁、启动和观察工作流 | 可以，但必须先看预检结果并确认 |
| CLI | 开发者、故障排查人员、CI | 查看精确状态、做回归测试、诊断单个步骤 | 只建议作为人工或故障回退入口 |
| MCP | 受治理的智能体和自动化流程 | 调用平台、模型及 QuantGPT 工具并保留统一审计 | 生产治理的首选自动化入口 |

三个入口不会形成三套独立业务逻辑。它们最终调用同一服务层、领域门禁、注册表
和审计记录。GUI 显示“通过”并不代表另一个模块也已经就绪；数据、因子、模型、
预测和模拟交易分别有自己的 readiness gate。

## 2. 安装并打开平台

### 2.1 环境要求

- Linux 或 WSL2；
- Python 3.11 或 3.12；
- 支持 Git 子模块的 Git；
- 与所选数据和模型工作负载相匹配的磁盘和内存；
- 使用数据更新、LLM 因子研究时所需的合法账户和 API 凭据。

### 2.2 克隆第三方 Fork

```bash
git clone --recurse-submodules https://github.com/whr1999/FXAlpha.git
cd FXAlpha
```

如果克隆时漏掉了子模块：

```bash
git submodule update --init --recursive
```

`third_party/quantgpt`、`third_party/qlib` 和 `third_party/tushare` 都是固定提交的
Git 子模块。不要用 `pip install` 的任意最新版静默替换它们。

### 2.3 创建本地配置和环境

```bash
cp config.example.yaml config.yaml
./scripts/bootstrap_public_env.sh
```

将 Tushare Token 和 LLM Key 放在不会提交到 Git 的 `config.yaml`，或者通过
`FXALPHA_CONFIG_FILE` 指向仓库外、权限受限的配置文件。不要把真实值写进
`config.example.yaml`、README、Issue、日志截图或测试夹具。

只想检查源码时，可以保留示例里的空凭据。此时平台可以启动并显示“不就绪”或
“缺少数据”，但不能执行需要供应商或生产资产的工作流。

### 2.4 启动本地 API 与 GUI

```bash
PYTHONPATH=. .venv/bin/python api_server.py --host 127.0.0.1 --port 18081
```

浏览器打开：

```text
http://127.0.0.1:18081/gui/
```

先确认：

1. 左下角显示 API 在线；
2. 平台总览能够结束“读取中”；
3. 各模块显示真实的 ready、blocked、waiting 或未配置状态；
4. 在任何写操作之前先阅读对应模块的预检结果。

服务只应监听本机回环地址。源码仓库没有公网认证网关；不要把 18081 端口直接
暴露到互联网。

## 3. GUI 各页面怎么用

| 页面 | 先看什么 | 可以做什么 | 继续阅读 |
| --- | --- | --- | --- |
| 平台数据总览 | API、数据日期、模块状态、后台定时流程 | 快速判断哪个模块需要处理；不会替代模块级深度预检 | [平台运维手册](PLATFORM_OPS_RUNBOOK.md) |
| 数据底座 | 生产日期、覆盖率、质量状态、更新现场 | 查询单只标的、运行只读预检、创建 staging、审核后晋升 | [数据工作流](DATA_FOUNDATION_WORKFLOW_CURRENT.md) |
| 因子研究 | 当前 run/round/stage、候选与阻断原因 | 启动或恢复 Orchestrator、查看研究证据、人工指导 | [因子研究操作](FACTOR_RESEARCH_OPERATIONS.md) |
| 模型研究 | 特征快照、数据门禁、任务状态、Rolling 证据 | 启动研究或生产 Rolling、停止/恢复任务、审核模型结果 | [模型研究工作流](MODEL_RESEARCH_WORKFLOW_CURRENT.md) |
| 个股研究 | 已晋升数据和模型的只读研究投影 | 查看标的状态；不能绕过模型与预测 readiness | [数据字段字典](DATA_FOUNDATION_TUSHARE_FIELD_DICTIONARY_CURRENT.md) |
| 模拟交易 | 账户、deployment、pending、持仓、净值及 replay 缺口 | 创建独立账户、运行只读 preflight、确认 fleet/replay | [多模型模拟交易合同](PRODUCTION_MULTI_MODEL_PAPER_TRADING_CURRENT.md) |
| 因子库 | active/retired、质量、相关簇及来源 | 审计因子库、形成 feature-set 建议；退役操作需治理确认 | [平台治理 MCP](PLATFORM_GOVERNANCE_MCP_CURRENT.md) |
| 模型库 | research、Rolling、production 模型分类与血缘 | 审核晋升状态和模型来源；不把训练完成等同于生产可用 | [模型命名合同](MODEL_NAMING_CONTRACT.md) |
| 数据底座/运维 | 磁盘、受保护资产、可清理预览 | 默认只做 safe dry-run；执行前必须人工确认 | [运行路径](RUNTIME_AND_DATA_PATHS_CURRENT.md) |

## 4. 从数据到模拟交易的标准路径

本章给出操作顺序；完整业务步骤和公式统一见
《[业务流程与计算逻辑说明书](BUSINESS_WORKFLOWS.zh-CN.md)》第 2–6 章。

### 4.1 数据底座

目标是把供应商数据先写入独立 staging，再经过完整性、日期、字段、价格和下游
兼容性检查后晋升；生产路径在晋升之前保持不变。

推荐顺序：

```text
data-status
  -> data-daily-preflight
  -> data-stage-update
  -> 审阅 staged quality / lineage
  -> data-promote-staged
  -> data-production-audit
```

安全的只读 CLI 示例：

```bash
.venv/bin/python cli.py data-status
.venv/bin/python cli.py data-daily-preflight
.venv/bin/python cli.py data-production-audit
```

不要在 preflight 阻断时强制晋升，不要直接覆盖生产 HDF、Qlib 目录或 QuantGPT
股票文件。完整的日更、全量重建、直连网络和质量合同见：

- [业务逻辑第 2 章：数据底座](BUSINESS_WORKFLOWS.zh-CN.md)
- [数据底座日更手册](DATA_FOUNDATION_DAILY_RUNBOOK_CURRENT.md)
- [Tushare 全量重建手册](DATA_FOUNDATION_TUSHARE_REBUILD_RUNBOOK_CURRENT.md)
- [直连网络与质量策略](DATA_FOUNDATION_DIRECT_NETWORK_AND_QUALITY_POLICY_CURRENT.md)

### 4.2 因子研究与因子库

默认控制器是 FXAlpha Orchestrator。它组织 LLM 判断，同时复用 QuantGPT/FXAlpha
的表达式校验、快速评分、查重、深度验证、质量门禁和受控入库。MCP 模式用于显式
调试和证据复核，不是另一套放宽标准的旁路。

最小观察顺序：

```bash
.venv/bin/python cli.py factor-status
.venv/bin/python cli.py factor-orch status
.venv/bin/python cli.py factor-audit status
```

一条候选只有在 novelty、rolling、anti-overfit、adversarial 和最终 quality gate
证据满足合同后，才能写入 active 因子库。研究任务显示 completed 不等于已经入库，
必须单独检查 gate 结果、import 记录和 active snapshot。

继续阅读：

- [业务逻辑第 3 章：11 阶段因子挖掘与入库评分](BUSINESS_WORKFLOWS.zh-CN.md)
- [因子研究总合同](../domain/factor_research/README.md)
- [Orchestrator 运行合同](../domain/factor_research/ORCHESTRATOR_README.md)
- [因子研究操作手册](FACTOR_RESEARCH_OPERATIONS.md)

### 4.3 模型研究与模型库

模型不能直接读取“某个看起来最新的因子文件”。它必须消费带指纹的 active feature
snapshot，并记录数据、因子注册表和 feature set 的血缘。

推荐顺序：

```text
model-status
  -> active feature snapshot ready
  -> research / production Rolling preflight
  -> train + validation + forward evidence
  -> registry review
  -> explicit promotion
```

安全的只读 CLI 示例：

```bash
.venv/bin/python cli.py model-status
.venv/bin/python cli.py model-runs
.venv/bin/python cli.py model-registry
.venv/bin/python cli.py model-production
```

“任务已接受”只代表后台任务已创建，“训练完成”也不自动等于“生产模型已晋升”。
必须检查完整状态、评估证据、registry 行和 production pointer。

继续阅读：

- [业务逻辑第 4 章：模型研究、Rolling 与晋升](BUSINESS_WORKFLOWS.zh-CN.md)
- [模型模块说明](../domain/model/README.md)
- [模型生产工作流](MODEL_RESEARCH_WORKFLOW_CURRENT.md)
- [首次真实测试检查表](MODEL_RESEARCH_PRETEST_CHECKLIST_CURRENT.md)

### 4.4 预测、组合建议和 Qlib 模拟交易

公开架构不使用 vn.py。FXAlpha 负责账户、推荐、deployment、幂等运行和审计；
Qlib 负责成交价、手续费、交易单位、涨跌停与交易所语义。

运行前必须同时满足：

- 数据底座 production health ready；
- production model 已晋升；
- prediction 与目标日期对齐；
- active 账户有明确的 model deployment；
- paper fleet preflight 通过。

安全的只读 CLI 示例：

```bash
.venv/bin/python cli.py pred-status
.venv/bin/python cli.py paper-fleet-status
.venv/bin/python cli.py paper-fleet-preflight
```

创建账户、运行 fleet 或长区间 replay 都是写操作。先在 GUI 查看计划；CLI/API
调用也必须使用明确账户、日期边界和确认参数。不要修改 SQLite、JSON 或账户状态
文件来绕过 blocker。

继续阅读：

- [业务逻辑第 5–6 章：预测、风险和 Qlib 模拟成交](BUSINESS_WORKFLOWS.zh-CN.md)
- [生产多模型模拟交易](PRODUCTION_MULTI_MODEL_PAPER_TRADING_CURRENT.md)
- [模拟交易值守手册](TRADING_RECOMMENDATION_RUNBOOK_CURRENT.md)
- [Qlib 涨跌停与成交合同](QLIB_LIMIT_TRADING_CONTRACT_CURRENT.md)

## 5. MCP 怎么接入

仓库中的 `.codex/config.example.toml` 只提供相对路径示例，不包含个人配置或凭据。
可选 MCP 分工如下：

| MCP | 责任 |
| --- | --- |
| QuantGPT MCP | 因子表达式、评分、回测、诊断和研究证据 |
| Model MCP | 模型上下文、研究任务、Rolling、状态与晋升证据 |
| FXAlpha Platform MCP | 跨模块治理、因子库审计、维护预览和平台服务状态 |

MCP 是受治理入口，不是绕过服务门禁的脚本通道。因子生产研究要求原生 QuantGPT
MCP 工具可见；工具缺失时应停止并修复配置，而不是换成 curl、临时 Python 或
HTTP 胶水脚本。

配置与运行原则见：

- [平台治理 MCP](PLATFORM_GOVERNANCE_MCP_CURRENT.md)
- [LLM 接入决策](LLM_INTEGRATION.md)
- [工程变更护栏](CODEX_ENGINEERING_GUARDRAILS.md)

## 6. 常见状态应该怎样理解

| 状态 | 含义 | 下一步 |
| --- | --- | --- |
| `ready` | 当前模块的已定义门禁通过 | 仍需检查下游模块自己的门禁 |
| `waiting` | 上游日期、资产或计划尚未就绪 | 保留当前状态，先处理明确上游 |
| `blocked` | 有可追踪的硬阻断 | 查看 blocker、run/package/account ID，不要绕过 |
| `accepted` | 异步任务已接收 | 继续检查 task status、日志和产物，不等于完成 |
| `completed` | 当前任务流程结束 | 单独检查 promotion/import/audit/production health |
| `already_current` | 幂等任务已经追平 | 不要重复创建运行或重复记账 |

## 7. 出现问题时的检查顺序

1. 确认 `GET /health` 和 GUI 是否在线；
2. 在平台总览确定是哪个 lane 异常；
3. 打开模块页面读取明确 blocker，而不是只看颜色；
4. 用对应的只读 CLI/MCP status 复核；
5. 检查当前 run、package、model 或 account 的证据，不创建平行任务；
6. 修复根因后恢复同一任务，再检查下游日期和注册表是否闭环。

生产运维和安全清理见 [平台运维手册](PLATFORM_OPS_RUNBOOK.md)。清理必须先 dry-run，
且 active、production、recent、running 和 locked 资产始终受保护。

## 8. 公开仓库与本地资产边界

允许提交：源码、测试、脱敏示例、说明文档、公开安全示意图、第三方 Gitlink 和
锁文件。

只有截图清单中经所有者逐张批准的静态运行界面可以作为例外。除此之外禁止提交：

- API Key、Token、`.env`、真实 `config.yaml`；
- 市场数据、因子值、active snapshot 和训练模型；
- 可复用的预测、推荐、持仓、净值、收益曲线及未经审查的账户截图；
- SQLite、日志、trace、任务证据、备份和运行目录；
- 个人路径、Codex Skill、记忆、会话或工具状态。

提交前运行：

```bash
.venv/bin/python scripts/run_release_preflight.py
```

发布过程见 [GitHub 上传手册](GITHUB_UPLOAD_RUNBOOK.md)。

## 9. 文档导航

- 想理解平台协作边界：看 [架构](ARCHITECTURE.md)。
- 想在新机器启动：看 [本地部署](LOCAL_DEPLOYMENT.md)。
- 想操作数据、因子、模型或模拟交易：使用本指南第 4 节对应 runbook。
- 想参与开发：看 [项目结构](PROJECT_STRUCTURE_CURRENT.md)、[贡献指南](../CONTRIBUTING.md)
  和 [工程变更护栏](CODEX_ENGINEERING_GUARDRAILS.md)。
- 想发布 GitHub：看 [发布就绪记录](GITHUB_PUBLICATION_READINESS.md)、
  [验证报告](VERIFICATION_REPORT_20260810.zh-CN.md) 和 [上传手册](GITHUB_UPLOAD_RUNBOOK.md)。
- 想查全部当前文档：看 [文档索引](DOCUMENTATION_INDEX_CURRENT.md)。
