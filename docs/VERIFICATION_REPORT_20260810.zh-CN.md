# GitHub 发布最终验收报告 — 2026-08-10

## 发布结论

- 公开仓库：[whr1999/FXAlpha](https://github.com/whr1999/FXAlpha)
- 默认分支：受保护的 `main`
- 首个正式版本：`v0.1.0`
- 许可口径：保留全部权利、源码可见，不宣传为开源项目
- 生产影响：无；生产 checkout 和运行环境均未改动

公开历史从干净根提交开始，后续只包含经过检查的 squash merge。施工历史、本地审计
包、生产标识符和私有 runtime 状态从未上传。

## 已验证的第三方拓扑

| 组件 | 公开 fork | 锁定提交 |
| --- | --- | --- |
| QuantGPT | `whr1999/QuantGPT` | `024818abcf76b35f0a8282f9a212c2309716defd` |
| Qlib | `whr1999/qlib` | `d5379c520f66a39953bad76234a7019a72796fd0` |
| Tushare 1.4.29 | `whr1999/tushare` | `bc5388dcb339ce7e11515cab5cb6087b3724e74b` |

`scripts/verify_publication_topology.py --release` 的结果为
`status=passed`：检查 3 个组件，`release_blockers=[]`、`violations=[]`。该闸门会从
公开 fork 获取精确锁定提交，并对主仓库执行全新匿名递归 clone，不接受本地路径替代。

## 自动化发布闸门

| 闸门 | 验证结果 |
| --- | --- |
| 公开工作树审计 | 328 个跟踪路径、3 个子模块、零违规 |
| 可达 Git 历史审计 | 所有可达提交和 blob 均已检查；零违规；最大 blob 小于 5 MiB |
| 发布拓扑 | 3 个 Gitlink 与锁文件一致，且可匿名获取 |
| Python 源码编译 | 通过 |
| Python 3.11 GitHub CI | 1034 passed、1 skipped、1 个第三方 warning |
| Python 3.12 GitHub CI | 1034 passed、1 skipped、1 个第三方 warning |
| CodeQL | 成功完成；默认分支最新分析结果为 0 |

唯一跳过项是针对私有生产 feature snapshot 的可选检查。公开 clone 按设计不包含该
快照；只有显式提供外部 `FXALPHA_SANDBOX_FEATURE_SET_ID` 时才运行。

## 安全和披露边界审计

最终核验时，公开仓库状态如下：

- Dependabot 开放告警：**0**；26 条已被新版依赖覆盖的公告记录为 fixed
- `main` 上 CodeQL 开放告警：**0**
- Secret Scanning 开放告警：**0**
- Secret Scanning 与 Push Protection：已启用
- Dependabot Security Updates：已启用

依赖图解析到已经验证的直接版本，包括 MCP 1.28.1、MLflow 3.11.1、PyArrow
23.0.1、Requests 2.34.2 和 PyTables 3.11.1。MLflow 的受支持边界仅为本地
`file://` tracking；本项目不会启动 MLflow server。

公开工作树只包含源码、测试、当前合同/runbook、脱敏示例、1 张系统架构 SVG、4 张
经所有者批准的界面截图及固定第三方 Gitlink。它不包含 API Key、Token、本地真实
配置、行情数据集、机器可读因子值、模型文件、注册表、账户数据库、预测、日志、
trace、备份或 runtime 目录。

工作树和历史闸门会阻断凭据模式、个人路径、生产形态的因子/模型/run 标识、生成态
目录、数据/模型/压缩包格式、未审查截图、未固定 Actions、脏 Gitlink 以及超过
5 MiB 的 blob。密钥检查报告只输出固定类别和位置，不输出命中文本或检测器标签。

## 截图结论

项目所有者已明确批准 4 张原始运行界面用于公开文档。检查未发现 API Key、Token、
登录凭据、本地文件路径、EXIF、图像注释或设备元数据。这些图片被说明为时点记录，
不是脱敏演示数据，也不是收益证明。

[`assets/screenshots/manifest.json`](assets/screenshots/manifest.json) 记录每张截图的
路径、尺寸、字节数和 SHA-256。新增未审查截图、删除但不更新清单，或替换图片却不
更新哈希，都会让公开工作树审计失败。

## 仓库治理

`main` 严格要求 Python 3.11、Python 3.12 和 CodeQL 检查。管理员同样受规则约束；
还要求会话解决和线性历史，并禁止 force push 与分支删除。Actions 使用完整提交 SHA
及最小读取权限。通用 Git 子模块 Dependabot 更新已关闭，机器人不能静默替换经过
审查的 fork pin。

公开发布不等于生产迁移。本次没有执行生产切换、数据晋升、因子入库、模型训练、预测
运行、模拟交易、服务切换或 runtime 清理。运行和回滚边界继续以
[`GITHUB_UPLOAD_RUNBOOK.md`](GITHUB_UPLOAD_RUNBOOK.md) 为准。
