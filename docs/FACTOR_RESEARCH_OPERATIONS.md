# FXAlpha 因子研究运行控制

本文件说明如何在不借助 Codex 的情况下启动、观察、干预、暂停、继续和结束生产 ORCH 因子研究。研究业务证据仍以现有文件为准：

- `runtime/factor_research/research_steps/current.jsonl`：面向 GUI、研究员和后续 LLM 的研究现场与 stage transition。
- `runtime/factor_research/orchestrator_events/current.jsonl`：ORCH 控制、检查点和 worker 事件。
- `runtime/factor_research/orchestrator_llm_traces/current.jsonl`：完整 LLM 请求与返回追踪。
- `third_party/quantgpt/quantgpt.db`：评分、回测和验证工具证据。

本次控制升级没有新增 manifest、SQLite 状态库或第二套研究流程。

## 一次安装

在 WSL 中执行：

```bash
cd <repo-root>
scripts/install_factor_research_services.sh --start
```

安装两个 loopback-only user services：

- `fxalpha-quantgpt-8003.service`
- `fxalpha-api-18081.service`

它们由 `fxalpha-factor-stack.target` 统一拉起，并在异常退出时自动恢复。服务日志使用 systemd journal：

```bash
systemctl --user status fxalpha-factor-stack.target
journalctl --user -u fxalpha-api-18081.service -n 100 --no-pager
journalctl --user -u fxalpha-quantgpt-8003.service -n 100 --no-pager
```

Windows 专用的一键启动脚本是：

```text
\\wsl.localhost\Ubuntu\home\<linux-user>\FXAlpha\scripts\start_factor_research_gui.ps1
```

可在 Windows PowerShell 中运行：

```powershell
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\<linux-user>\FXAlpha\scripts\start_factor_research_gui.ps1"
```

脚本启动 `fxalpha-factor-stack.target`，等待 18081/8003 同时健康后打开
`http://127.0.0.1:18081/gui/`。它没有合并进 FX Doctor。

`%USERPROFILE%\Documents\New project\fxalpha_doctor.ps1` 是独立的诊断/恢复工具：
`-Action open-gui` 会检查并尝试启动 18081 后打开 GUI，
`-Action recover-safe -IncludeQuantGPT` 用于 18081/8003 异常时的有界恢复。正常日常启动优先使用上面的
`start_factor_research_gui.ps1`，故障排查再使用 Doctor。

## GUI 操作

GUI 的“研究指令台”是唯一启动入口：

1. 填写研究指令和关键参数。
2. 查看 API、QuantGPT、当前 run 和控制状态。
3. 点击“预检并启动 Orchestrator”。
4. 运行中可以“暂停研究”或“结束本次研究”。
5. 暂停完成后可以“继续研究”；继续的是同一 run 和既有 durable checkpoint。
6. “研究干预”已整合在研究指令台底部，通过正式 guidance API 写入 `human_guidance`。
   干预是一次性消息：仅最新一条尚未送达的干预进入下一次 LLM 判断；请求 research step
   写入 `operator_guidance_delivery` 后立即消费，后续 stage 不再携带。`human_guidance`
   只保留作审计与 GUI 回执，不进入模型通用短期历史。GUI 默认只展示待执行项和最近结果，
   更早回执折叠保存，并用同一 `llm_trace_id` 关联实际模型结果。
   Guidance API 只接受能够继续产生 LLM 判断的 run（运行中、暂停中或可恢复阻塞），
   拒绝不存在、已结束、正在停止和不可恢复阻塞的 run。单条干预最多 500 字，GUI
   提交期间禁用按钮，避免重复写入。

“暂停”与“结束”不同：暂停保留同一 run 的恢复权；结束写入终态，下次启动创建新 run。两者都先返回 `*_requested`，只有 worker 到达安全检查点后才成为 `paused` 或 `completed`。

### 启动参数不是展示字段

研究指令台提交后，参数按同一条生产链生效：

1. `POST /factor/research/start` 在响应的 `inputs` 中返回本次启动回执；GUI 会逐字段比对请求与回执，不一致时直接提示停止检查。
2. `orchestrator_launch` 事件同时持久化 `inputs` 和 `research_contract`，供后台 worker、暂停恢复和审计读取。
3. 每个 LLM stage 的精简上下文都携带 `operator_research_direction` 和 `research_contract`。非 `auto` 指令是 thesis、hypothesis、expression、candidate 决策的约束边界；`auto` 才允许自主选题。
4. `universe`、评估日期、持有期、基准、中性化、`top_frac`、`cost_rate` 和再平衡锚点会传给评分/深验工具；目标入库数、每轮候选上限和最大轮次由 ORCH 状态机执行。
5. 运行时没有“经验沉淀”开关或经验卡写入器。Factor Map 只从正式因子库审计和已完成研究轨迹生成只读上下文；“同步 WQ”仍只在通过正式入库检查后执行。

Score、Novelty、Deep 阶段仍只接收诊断所需内容，不重新附带完整算子表；上面的运行契约是小型只读参数块，不改变 stage transition 或回退规则。

## HTTP API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/factor/research/status` | 读取因子研究、依赖与最近结果总状态 |
| `GET` | `/factor/research/control` | 读取权威控制状态和当前允许操作 |
| `GET` | `/factor/research/preflight` | 启动前服务与重复运行检查 |
| `GET` | `/factor/research/run-view?run_id=...` | 读取单个 run 的研究步骤、ORCH 与工具证据联合视图 |
| `GET` | `/factor/research/orchestrator-events` | 读取 ORCH 事件；完整审计时显式使用 `include_payload=true` |
| `GET` | `/factor/research/orchestrator-traces` | 读取 DeepSeek 请求/返回追踪；完整审计时显式使用 `include_payload=true` |
| `POST` | `/factor/research/start` | 启动一个新 ORCH run |
| `POST` | `/factor/research/pause` | 请求在安全检查点暂停 |
| `POST` | `/factor/research/resume` | 从同一 run 的持久化参数和检查点继续 |
| `POST` | `/factor/research/stop` | 请求结束当前 run |
| `POST` | `/factor/research/config-defaults` | 保存允许修改的后续 run 默认参数，不改变当前 run |
| `POST` | `/factor/research/guidance` | 写入人工研究指导 |

控制请求示例只需要 run id：

```bash
curl -s http://127.0.0.1:18081/factor/research/control
curl -s -X POST http://127.0.0.1:18081/factor/research/pause -H 'Content-Type: application/json' -d '{"run_id":"<run_id>"}'
curl -s -X POST http://127.0.0.1:18081/factor/research/resume -H 'Content-Type: application/json' -d '{"run_id":"<run_id>"}'
curl -s -X POST http://127.0.0.1:18081/factor/research/stop -H 'Content-Type: application/json' -d '{"run_id":"<run_id>"}'
```

## CLI

CLI 只调用同一套 18081 HTTP API，不直接启动第二套 runner：

```bash
python3 cli.py factor-orch status
python3 cli.py factor-orch start --direction auto --target-adopted 10 --n-rounds 0
python3 cli.py factor-orch pause --run-id <run_id>
python3 cli.py factor-orch resume --run-id <run_id>
python3 cli.py factor-orch guidance --run-id <run_id> --message "下一轮优先检查信号方向"
python3 cli.py factor-orch stop --run-id <run_id>
```

## 进程与恢复语义

ORCH worker 运行在独立 transient user service 中，18081 API 重启不会杀死正在运行的研究。API 和 worker 通过既有 orchestrator event journal 交换控制请求；worker 的业务进度继续写 research steps、events、traces 和 QuantGPT DB。

如果 systemd transient service 不可用，启动器退化为与 API 解耦的 detached process。该退化只改变进程托管方式，不改变研究业务链或状态事实源。

## 故障检查顺序

1. `systemctl --user status fxalpha-factor-stack.target`
2. `python3 cli.py factor-orch status`
3. 检查 18081 和 8003 health。
4. 检查最新 research step、orchestrator event 和对应 LLM trace。
5. 只有控制状态明确允许 `resume` 时才继续旧 run；正式 `completed` 后应启动新 run。
