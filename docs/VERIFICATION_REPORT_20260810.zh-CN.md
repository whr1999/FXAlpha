# GitHub 发布最终验收报告 — 2026-08-10

## 候选版本

- 公开候选分支：`codex/github-publication-final`
- 预定映射：候选 `HEAD` 上传为 GitHub `refs/heads/main`
- 历史形态：单一根提交，不继承施工历史
- QuantGPT 固定提交：`024818abcf76b35f0a8282f9a212c2309716defd`
- Qlib 固定提交：`d5379c520f66a39953bad76234a7019a72796fd0`
- Tushare 1.4.29 固定提交：`bc5388dcb339ce7e11515cab5cb6087b3724e74b`

公开候选位于独立 checkout。本次最终审计没有修改生产源码目录、服务、数据库、行情
数据、模型资产、因子值、账户状态或 vn.py 环境。

## 最终本地发布闸门

命令：

```bash
python scripts/run_release_preflight.py
```

结果：**通过**。

| 闸门 | 结果 |
| --- | --- |
| 公开工作树审计 | 通过；328 个路径、3 个子模块；零违规 |
| Git 历史审计 | 通过；1 个提交、321 个 blob；零违规 |
| 本地发布拓扑 | 通过；3 个 Gitlink 全部与锁文件一致 |
| Python 源码编译 | 通过 |
| 完整回归 | **1022 passed, 1 skipped，耗时 39.38 秒** |

唯一跳过项是针对私有生产 feature snapshot 的可选检查。公开 clone 不包含该快照，
因此现在只有显式提供外部 `FXALPHA_SANDBOX_FEATURE_SET_ID` 才运行，否则给出明确
原因后跳过。

## 资料和披露边界审计

公开工作树包含源码、测试、当前合同/runbook、脱敏示例、1 张架构 SVG、4 张经所有者
批准的界面截图，以及固定的第三方 Gitlink。它不包含 API Key、Token、真实配置、
行情数据集、机器可读因子值、模型文件、注册表、账户数据库、预测、日志、trace、
备份或 runtime 目录。

最终检查删除了使用平台不需要、但可能暴露本地状态的施工材料：

- 生产 prompt/canary 记录和因子工程时间线；
- 带真实因子/模型/run 标识或账户数值的实现报告；
- 硬编码私有 feature snapshot 的一次性模型诊断脚本；
- 部署专属的清理数量、磁盘统计和 runtime 报告路径。

历史闸门除密钥模式、个人路径、私有文件类型和超过 5 MiB 的 blob 外，现在还会阻断
生产形态的因子 ID、因子 run ID、模型 ID、production model run ID 和旧 feature
snapshot ID。施工历史已经替换为干净根提交，因此被删除的资料不会从计划上传到
`main` 的分支历史中恢复。

## 截图结论

项目所有者已明确批准 4 张原始运行界面用于公开文档。人工检查未发现 API Key、
Token、登录凭据、本地文件路径、EXIF、图像注释或设备元数据。文档明确把它们描述为
时点真实记录，而不是脱敏演示数据或收益证明。

[`assets/screenshots/manifest.json`](assets/screenshots/manifest.json) 固定每张图的路径、
尺寸、字节数和 SHA-256。新增未审查截图、删除但不更新清单，或替换图片却不更新哈希，
都会使公开工作树审计失败。最大的可达 blob 是 2,363,546 字节的模型研究截图，低于
5 MiB 历史上限。

## 第三方和联网闸门

QuantGPT、Qlib、Tushare 的本地拓扑已经通过。当前工作站的网络证据存在间歇性：
匿名 `git ls-remote` 官方 Qlib 仓库曾成功，以下预定公开目标的匿名 GitHub API
请求也曾明确返回 HTTP 404：

- `whr1999/QuantGPT`
- `whr1999/qlib`
- `whr1999/tushare`
- `whr1999/FXAlpha`

随后一次 `verify_publication_topology.py --release` 对四个 Git URL 都出现了
`gnutls_handshake`。因此每一次检查还不能稳定区分仓库可达性，但网络成功时的 API
结果已经证明这四个仓库对匿名用户不可用。发布必须同时满足两项：先创建或公开仓库并
上传锁文件记录的提交，再取得一次不中断的匿名组件检查和递归 clone 全部通过结果。

## 剩余人工决策和发布限制

1. 决定首个公开版本继续采用当前“保留全部权利、源码可见”条款，还是改用开源许可；
2. 按锁文件顺序创建并上传 3 个真实 fork，再运行匿名组件闸门；
3. 创建空的公开主仓库，只推送 `codex/github-publication-final:main`，绝不使用
   `git push --all`；
4. 开启 GitHub 安全和分支保护，等待 CI/CodeQL，通过全新匿名递归 clone 后再创建
   `v0.1.0` 标签。

本次审计没有执行生产切换、数据晋升、因子入库、模型训练、模拟交易、远程仓库创建、
push 或 release tag。准确命令和停止条件见
[`GITHUB_UPLOAD_RUNBOOK.md`](GITHUB_UPLOAD_RUNBOOK.md)。
