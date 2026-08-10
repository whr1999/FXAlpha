from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_paper_fleet_api_and_cli_contracts_are_exposed():
    api = (ROOT / "api_server.py").read_text(encoding="utf-8")
    cli = (ROOT / "cli.py").read_text(encoding="utf-8")

    for route in (
        '"/paper/status"',
        '"/paper/run"',
        '"/paper/replay"',
        '"/paper/fleet/status"',
        '"/paper/fleet/preflight"',
        '"/paper/replay/plan"',
        '"/paper/accounts"',
        '"/paper/accounts/status"',
        '"/paper/fleet/run"',
        '"/paper/replay/run"',
    ):
        assert route in api
    assert '"/data/benchmark-series"' in api
    assert 'compact_code == "000300SH"' in api
    assert "Avoid loading the full production HDF" in api
    assert "paper_write_confirmation_required" in api
    for command in (
        "paper-account-create",
        "paper-fleet-status",
        "paper-fleet-preflight",
        "paper-fleet-run",
        "paper-replay-plan",
        "paper-replay-run",
    ):
        assert command in cli


def test_paper_fleet_gui_exposes_accounts_comparison_and_replay_controls():
    html = (ROOT / "gui" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "gui" / "app.js").read_text(encoding="utf-8")

    for element_id in (
        "paper-fleet-overview",
        "paper-account-comparison",
        "paper-replay-center",
        "paper-account-create-form",
        "plan-paper-replay",
        "run-paper-replay",
        "paper-account-action-result",
        "paper-account-create-result",
    ):
        assert f'id="{element_id}"' in html
    assert "初始资金 1,000,000 · Top 20 · Drop 2" in html
    assert 'getJsonSafe(`/paper/status${wantsFullPaperFleet ? "" : "?compact=true"}`' in js
    assert 'postJson("/paper/run"' not in js
    assert 'postJson("/platform/automation-control"' in js
    assert 'postJson("/paper/replay"' in js
    assert 'postJson("/paper/accounts"' in js
    assert 'postJson("/paper/accounts/status"' in js
    assert "恢复前会检查账户、模型绑定和账本完整性" in js
    assert "暂停只停止后续自动日切" in html
    assert "const gapPlan = gapOutputs.plan || gapOutputs" in js
    assert "const plan = planOutputs.plan || planOutputs" in js
    assert "逐日分数质量" in js
    assert "unique_score_count" in js
    assert "equal_to_boundary" in js
    assert "PIT身份覆盖" in js
    assert "Write Lock" in js
    assert "confidence_cash_top20_drop2_hold5_open_v2" in html
    assert "Target / Actual Exposure" in js
    assert "目标现金" in js
    assert "strategy_contract_version" in js
    assert "一个生产模拟账户永久绑定一个模型" in html
    assert 'name="display_name"' not in html
    assert "paperModelTagBadges" in js
    assert "手工晋升" in js
    assert "永久绑定指定模型" in js
    assert 'already_current: "已是最新"' in js
    assert "latestFleetPreflight.target_date" in js
    assert "最近处理日" in js
    assert "自动检查完成 · 无需重复运行" in js
    assert "background-automation-guide" in html
    assert "任务完成后退出是正常行为" in html
    assert "未保留该次统计，不代表实际使用量为 0" in html
    assert "paper-console-decision" in js
    assert "以下是所选账户的等待或降级提示，不等于硬性阻断" in js
    assert "pendingRecommendations: pending" in js
    assert "paper-console-status-head" in html
    assert "4 个生产环节" in html
    assert ".paper-console-status .paper-console-decision-copy" in (ROOT / "gui" / "styles.css").read_text(encoding="utf-8")
    assert 'id="paper-replay-form"' in html
    assert 'id="reset-paper-replay-range"' in html
    assert "paperReplayFormPayload" in js
    assert "先检查缺口并生成精确补跑计划" in js
    assert "const canExecute = Boolean(accountIsActive && hasExactPlan && dates.length && blockers.length === 0)" in js
    assert ".paper-replay-control-panel" in (ROOT / "gui" / "styles.css").read_text(encoding="utf-8")
    assert ".paper-console-action-result" in (ROOT / "gui" / "styles.css").read_text(encoding="utf-8")


def test_paper_trading_gui_is_account_first_and_separates_execution_from_next_plan():
    html = (ROOT / "gui" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "gui" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "gui" / "styles.css").read_text(encoding="utf-8")
    api = (ROOT / "api_server.py").read_text(encoding="utf-8")

    for element_id in (
        "paper-account-switcher",
        "paper-account-context",
        "trading-summary",
        "trading-positions",
        "trading-risk-policy",
        "trading-picks",
        "trading-trades",
    ):
        assert f'id="{element_id}"' in html
    for tab in ("console", "overview", "risk", "plan", "trades"):
        assert f'data-paper-trading-tab="{tab}"' in html
        assert f'data-paper-trading-pane="{tab}"' in html
    assert 'data-paper-trading-tab="research"' not in html
    assert 'data-paper-trading-pane="research"' not in html
    assert "历史研究回测" not in html
    for console_tab in ("status", "automation", "accounts", "create", "replay", "diagnostics", "settings"):
        assert f'data-paper-console-tab="{console_tab}"' in html
        assert f'data-paper-console-pane="{console_tab}"' in html
    assert "paper-console-subnav" in html
    assert html.index('class="workspace-nav paper-trading-tabbar"') < html.index('class="surface paper-automation-summary"')
    assert html.index('class="workspace-nav paper-trading-tabbar"') < html.index('class="surface paper-account-context"')
    assert "--paper-shell-radius: 18px;" in styles
    assert "--paper-control-radius: 12px;" in styles
    assert "--paper-inner-radius: 10px;" in styles
    assert "/* Shared primary workspace navigation: factor mining, model training and paper" in styles
    assert "#panel-research .workspace-nav" in styles
    assert "#panel-model-research .workspace-nav" in styles
    assert "#panel-trading > .paper-trading-tabbar" in styles
    assert "#panel-trading .paper-primary-tabs .workspace-tab" in styles
    assert "border-radius: var(--fx-pill-radius) !important;" in styles
    assert "container-name: paper-console-status;" in styles
    assert "@container paper-console-status (max-width: 720px)" in styles
    assert "@container paper-console-status (max-width: 440px)" in styles
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in styles
    assert "#panel-trading .paper-console-status .paper-console-decision-copy > strong" in styles
    assert "GLOBAL + SELECTED ACCOUNT" in html
    assert "GLOBAL AUTOMATION" in html
    assert "NEW PAPER ACCOUNT" in html
    assert "RISK POLICY" in html
    assert "研究工具，不写入生产账户" not in html
    assert "function setPaperConsoleTab" in js
    assert "fxalpha.paperConsoleTabV1" in js
    assert 'setPaperConsoleTab(openOps.dataset.openPaperOps || "settings")' in js
    assert 'wantsOverview ? getJsonSafe("/pipeline/status", overviewReadOptions)' not in js
    assert 'getJsonSafe("/trade/status?compact=true", overviewReadOptions)' in js
    assert 'getJsonSafe(`/paper/status${wantsFullPaperFleet ? "" : "?compact=true"}`' in js
    assert 'wantsTrading ? getJsonSafe("/pipeline/status", overviewReadOptions)' not in js

    assert 'data-paper-trading-tab="positions"' not in html
    assert 'data-paper-trading-pane="positions"' not in html
    assert 'data-paper-trading-tab="ops"' not in html
    assert 'data-paper-trading-pane="ops"' not in html
    assert 'data-paper-trading-tab="replay"' not in html
    assert 'data-paper-trading-pane="replay"' not in html
    assert "paper-tools-menu" not in html
    assert html.index('data-paper-trading-tab="console"') < html.index('data-paper-trading-tab="overview"')
    assert html.index('data-paper-trading-pane="console"') < html.index('id="paper-fleet-overview"') < html.index('id="paper-replay-center"') < html.index('data-paper-trading-pane="overview"')
    assert html.index('data-paper-trading-pane="overview"') < html.index('id="trading-positions"') < html.index('data-paper-trading-pane="plan"')
    assert "账户净值与同期基准" in html
    assert "000300.SH" in js
    assert 'return `/data/benchmark-series?code=000300.SH&start=${start.toISOString().slice(0, 10)}`' in js
    assert "沪深 300" in js
    assert "paper-account-hover-panel" in js
    assert "data-paper-curve-date" in js
    assert "paper-position-list" in js
    assert "paper-position-list-head" in js
    assert "paper-position-list-row" in js
    assert "paper-position-card" not in js
    assert "paperDisplayInstrument" in js
    assert "security_names" in js
    assert "daily_trades" in js
    assert "account.latest_orders" in js
    assert "paper-plan-summary" in js
    assert "paper-target-list" in js
    assert "paper-target-list-head" in js
    assert "data-paper-target-filter" in js
    assert "paperTargetFilter" in js
    assert "保持不变" in js
    assert "当前筛选没有对应调仓指令" in js
    assert "max-height: 420px;" in styles
    assert "scrollbar-gutter: stable;" in styles
    assert ".paper-target-filter button.active" in styles
    assert "@media (min-width: 1181px)" in styles
    assert "grid-template-rows: auto auto minmax(0, 1fr);" in styles
    assert ".paper-target-portfolio .paper-target-list {\n    min-height: 0;\n  }" in styles
    assert "syncPaperTargetListHeight" in js
    assert "observePaperPlanLayout();" in js
    assert 'window.matchMedia?.("(min-width: 1181px)")' in js
    assert 'window.addEventListener("resize", syncPaperTargetListHeight)' in js
    assert "#panel-trading #trading-account" in styles
    assert "max-height: 520px;" in styles
    assert "max-height: 460px;" in styles
    assert "#panel-trading #trading-account::-webkit-scrollbar" in styles
    assert "paper-target-card" not in js
    assert "paper-summary-three-grid" in js
    assert "paper-summary-four-grid" not in js
    assert "paper-summary-next" not in js
    assert "paper-console-active" in js
    assert "paper-account-group-list" in js
    assert "舰队" not in html
    assert "paper-execution-rail" not in js
    assert "paper-execution-connector" not in js
    assert "paper-rebalance-stats" in js
    assert "paperPlanActionMeta" in js
    assert 'tone: "new"' in js
    assert 'tone: "add"' in js
    assert 'tone: "reduce"' in js
    assert 'tone: "exit"' in js
    assert "paper-action-badge" in js
    assert "paper-order-split" in js
    assert "paper-order-side" in js
    assert "Buy Orders" in js
    assert "Sell Orders" in js
    assert "paper-order-deferred" in js
    assert "paper-order-list-head" in js
    assert "paper-order-list-row" in js
    assert "accountDailyTrades" in js
    assert "paper-ledger-query-form" in js
    assert "paper-ledger-date-input" in js
    assert "paper-ledger-export" in js
    assert 'href="${escapeHtml(ledgerExportUrl)}" download' in js
    assert '"/trade/ledger/export"' in api
    assert 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' in api
    assert "paperLedgerQueryDate" in js
    assert "data-paper-ledger-date" in js
    assert "data-paper-ledger-latest" in js
    assert "paper-day-metric-grid" in js
    assert "paper-day-return-metric" in js
    assert '>${selectedDailyReturn >= 0 ? "+" : ""}${pct(selectedDailyReturn, 2)}</strong>' in js
    assert '.paper-ledger-query input[type="date"]' in styles
    assert "color-scheme: dark;" in styles
    assert "paper-day-trade-grid" in js
    assert "paper-ledger-record-grid" in js
    assert "paper-trade-action" in js
    assert "账户流水查询" in js
    assert "资产校验" in js
    assert "当日成交明细" in js
    assert "日结账本" in js
    assert "paper-performance-summary" in js
    assert ".paper-performance-metric-grid {" in styles
    assert "min-height: 72px;" in styles
    assert ".paper-performance-metric-grid .backtest-metric-card strong" in styles
    assert "tone-benchmark" in js
    assert "tone-excess" in js
    assert "tone-cost" in js
    assert "--paper-metric-accent: rgba(96, 165, 250, 0.46)" in styles
    assert ".paper-performance-metric-grid .tone-benchmark {" not in styles
    assert ".paper-performance-metric-grid .tone-excess {" not in styles
    assert ".paper-performance-metric-grid .tone-trading {" not in styles
    assert ".paper-performance-metric-grid .tone-cost {" not in styles
    assert ".paper-performance-metric-grid .tone-benchmark strong" not in styles
    assert "账户净累计收益" in js
    assert "累计收益额" in js
    assert "沪深 300 同期收益" in js
    assert "累计超额（净值差）" in js
    assert "当日交易成本" in js
    assert "累计交易成本" in js
    assert "font-size: clamp(20px, 2.2vw, 24px)" in styles
    assert "paper-summary-return-amount" in js
    assert "paperPositionCostBasis" in js
    assert "持仓盈亏" in js
    assert "holding_return" in js
    assert 'gross_exposure_budget_exhausted: "仓位预算已用尽"' in js
    assert 'gross_exposure_budget_rounding: "剩余预算不足一手"' in js
    assert 'not_tradable: "当日不可交易"' in js
    assert 'missing_deal_price: "缺少成交价格"' in js
    assert 'labels[key] || "其他执行限制"' in js
    assert "paperConstraintLabel(constraint)" in js
    assert "实际股票仓位</span>" not in js


def test_paper_gui_uses_lifecycle_safe_writes_and_lazy_subpage_reads():
    api = (ROOT / "api_server.py").read_text(encoding="utf-8")
    html = (ROOT / "gui" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "gui" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "gui" / "styles.css").read_text(encoding="utf-8")

    assert "legacy_trading_write_endpoint_retired" in api
    assert '"replacement": legacy_trading_writes[self.path]' in api
    assert "const wantsPaperRisk" in js
    assert "const wantsDailyOps" in js
    assert "const wantsPaperBenchmark" in js
    assert "const wantsFullPaperFleet" in js
    assert "new AbortController()" in js
    assert "options.timeoutMs ?? 30000" in js
    assert 'retired: "已退休"' in js
    assert 'superseded: "已终止"' in js
    assert 'frozen: "已冻结"' in js
    assert 'id="paper-account-model-run-select"' in html
    assert "选择模型库生产模型" in html
    assert "正在读取模型库生产模型" in html
    assert "function paperProductionModelCatalog()" in js
    assert 'status=${wantsPaperModelCatalog ? "production" : "library"}&compact=true' in js
    assert 'state.paperConsoleTab === "create"' in js
    assert '模型库中没有已晋升的生产模型' in js
    assert 'productionModelSelect.disabled = modelCatalog.length === 0;' in js
    assert 'selectableValues.has(currentValue)' in js
    assert ".paper-position-pnl" in styles
    assert "grid-template-columns: 28px 96px 86px 60px 72px 60px 60px 54px minmax(84px, 1fr);" in styles
    assert "min-width: 680px;" in styles
    assert "所选账户的逐笔成交明细未加载到当前交易状态" not in js

    assert "最近完成" in js
    assert "下一步" in js
    assert "paperSelectedAccount(accounts)" in js
    assert 'latestExecution.account_id === selectedId' in js
    assert 'recommendation.account_id === trading.latest_recommendation?.account_id' in js
    assert 'account_id: form.get("account_id") || state.selectedPaperAccountId' in js
    assert "paperConfidenceSummary" in js
    assert "paperReasonLabel" in js
    assert 'const PAPER_TRADING_TABS = new Set(["console", "overview", "risk", "plan", "trades"])' in js
    assert 'postJson("/trade/sim"' not in js
    assert '"/trade/sim"' not in api
    assert "trading-sim-form" not in js
    assert "paper-research-active" not in js
    assert 'const PAPER_CONSOLE_TABS = new Set(["status", "automation", "accounts", "create", "replay", "diagnostics", "settings"])' in js
    assert 'data-paper-console-tab="create"' in html
    assert 'data-paper-console-pane="create"' in html
    assert "新建模拟交易任务" in html
    assert "配置与写操作" not in html
    assert "trading-form" not in html
    assert "dry-run-daily-ops-advanced" not in html
    assert "run-daily-ops-routine" not in html
    assert "risk-policy-form" in html
    assert 'postJson("/trade/risk-policy"' in js
    assert "paper-risk-summary-caps" in js
    assert "paper-risk-cap-grid" not in js
    assert ".paper-risk-summary {\n  display: grid;\n  grid-template-columns: minmax(230px, 1fr) minmax(360px, 1.5fr) minmax(112px, 0.42fr);" in styles
    assert ".paper-risk-summary-caps {\n  display: grid;\n  grid-template-columns: repeat(3, minmax(0, 1fr));" in styles
    assert "最终目标 / 现金" not in js
    assert "renderRiskLineChart" in js
    assert 'const capHistory = riskHistory.caps || [];' in js
    assert 'key: "model_cap", label: "模型仓位上限"' in js
    assert "模型仓位上限历史" in js
    assert "riskSparkline" not in js
    assert "Risk Layer History" in js
    assert 'getJsonSafe(`/trade/risk-policy?history_days=160' in js
    assert ".paper-risk-history-grid" in styles
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in styles
    assert "#trading-risk-policy.detail-block" in styles
    assert ".paper-risk-market-workspace > header" in styles
    assert "max-width: none" in styles
    assert 'if (tab === "ops" || tab === "replay") return "console"' in js
    assert 'setPaperTradingTab("console")' in js


def test_confidence_cash_contract_is_versioned_across_signal_execution_and_replay():
    confidence = (ROOT / "domain" / "trading" / "confidence.py").read_text(encoding="utf-8")
    signals = (ROOT / "domain" / "trading" / "signals.py").read_text(encoding="utf-8")
    execution = (ROOT / "domain" / "trading" / "execution" / "qlib_paper.py").read_text(encoding="utf-8")
    fleet = (ROOT / "services" / "paper_fleet_service.py").read_text(encoding="utf-8")

    assert 'CONFIDENCE_POLICY_VERSION = "confidence_cash_v2"' in confidence
    assert "strictly_above_tied_topk_boundary" in confidence
    assert "slot_weight" in signals
    assert '"target_weight_v2"' in execution
    assert "risk_reduction_overrides_n_drop" in execution
    assert '"confidence_policy"' in fleet
    assert "def paper_account_day_run(" in fleet
    assert "day = paper_account_day_run(" in fleet
    assert "status_mode\": \"snapshot_only" in fleet


def test_daily_data_success_triggers_paper_fleet_service():
    data_service = (ROOT / "deploy" / "systemd" / "fxalpha-data-daily.service").read_text(encoding="utf-8")
    fleet_service = (ROOT / "deploy" / "systemd" / "fxalpha-paper-fleet-daily.service").read_text(encoding="utf-8")
    fleet_timer = (ROOT / "deploy" / "systemd" / "fxalpha-paper-fleet-daily.timer").read_text(encoding="utf-8")

    assert "OnSuccess=fxalpha-paper-fleet-daily.service" in data_service
    assert "paper-fleet-run" in fleet_service
    assert "Persistent=true" in fleet_timer
