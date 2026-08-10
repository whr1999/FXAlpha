from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_background_workflow_status_is_present_on_operational_pages():
    index = (PROJECT_ROOT / "gui" / "index.html").read_text(encoding="utf-8")
    app = (PROJECT_ROOT / "gui" / "app.js").read_text(encoding="utf-8")

    assert 'id="overview-background-workflow-status"' in index
    assert 'id="trading-background-workflow-summary"' in index
    assert 'id="trading-background-workflow-status"' in index
    assert 'id="data-background-workflow-status"' in index
    assert 'renderBackgroundWorkflowStatus("overview-background-workflow-status"' in app
    assert "renderTradingBackgroundWorkflowSummary();" in app
    assert 'renderBackgroundWorkflowStatus("trading-background-workflow-status"' in app
    assert 'renderBackgroundWorkflowStatus("data-background-workflow-status"' in app
    assert "workflow.service" in app
    assert "timer.next_trigger" in app
    assert "本轮已完成 · 正常退出" in app
    assert "已启用 · 等待触发" in app
    assert "已追平生产数据" in app
    assert "等待下一交易日行情入库" in app
    assert 'hardBlocked ? "已阻断" : "运行正常"' in app
    assert "信号已生成" in app
    assert "模拟交易补检" in app
    assert "backgroundResourceLabel" in app
    assert "未提供本轮统计" in app
    assert "成功 · 退出码 0" in app
    assert 'data-background-workflow-action="run_now"' in app
    assert 'data-background-workflow-action="pause"' in app
    assert 'data-background-workflow-action="resume"' in app
    assert 'data-background-workflow-action="update_schedule"' in app
    assert 'postJson("/platform/automation-control"' in app
    assert 'id="background-automation-action-result"' in index

    styles = (PROJECT_ROOT / "gui" / "styles.css").read_text(encoding="utf-8")
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in styles
    assert ".paper-automation-summary-facts > span.is-danger" in styles

    summary_position = index.index('id="trading-background-workflow-summary"')
    console_position = index.index('data-paper-trading-pane="console"')
    detail_position = index.index('id="trading-background-workflow-status"')
    assert summary_position < console_position < detail_position


def test_data_and_trading_pages_use_lightweight_automation_status():
    app = (PROJECT_ROOT / "gui" / "app.js").read_text(encoding="utf-8")
    api = (PROJECT_ROOT / "api_server.py").read_text(encoding="utf-8")

    assert 'getJsonSafe("/platform/automation-status", { timeoutMs: 5000 })' in app
    assert 'state.automationStatus = keepPreviousOnReadFailure' in app
    assert 'if path == "/platform/automation-status":' in api
    assert 'self.path == "/platform/automation-control"' in api
    assert 'if (activePanel === "data-foundation") renderDataFoundation();' in app
    assert 'if (activePanel === "trading") renderTrading();' in app


def test_overview_prioritizes_paper_trading_and_uses_compact_reads():
    index = (PROJECT_ROOT / "gui" / "index.html").read_text(encoding="utf-8")
    app = (PROJECT_ROOT / "gui" / "app.js").read_text(encoding="utf-8")
    styles = (PROJECT_ROOT / "gui" / "styles.css").read_text(encoding="utf-8")
    api = (PROJECT_ROOT / "api_server.py").read_text(encoding="utf-8")

    assert index.index('id="overview-paper-highlight"') < index.index('id="overview-background-workflow-status"')
    assert 'type: "paper-performance"' in app
    assert 'title: "模拟交易"' in app
    assert 'label: "相对沪深 300"' in app
    assert 'modelResearchPaused ? "暂停复核" : "未运行"' in app
    assert 'factorControlState === "blocked" ? "已阻塞"' in app
    assert '?compact=true' in app
    assert 'platform_runtime_status(compact=_query_bool(query, "compact", False))' in api
    assert '.module-cockpit-card.variant-featured {' in styles
    assert '.overview-workflow-card {' in styles
    assert 'grid-auto-rows: 8px' not in styles


def test_overview_passes_model_registry_readiness_to_module_builder():
    index = (PROJECT_ROOT / "gui" / "index.html").read_text(encoding="utf-8")
    app = (PROJECT_ROOT / "gui" / "app.js").read_text(encoding="utf-8")

    builder_start = app.index("function buildOverviewModules(ctx)")
    builder_end = app.index("const models = modelRegistry.items", builder_start)
    builder_contract = app[builder_start:builder_end]
    assert "modelRegistryLoaded," in builder_contract

    call_start = app.index("const businessModules = buildOverviewModules({")
    call_end = app.index("});", call_start)
    call_contract = app[call_start:call_end]
    assert "modelRegistryLoaded," in call_contract
    assert "function signedPercent(value, digits = 2)" in app
    assert "const signedPercent =" not in app
    assert 'const GUI_BUILD_ID = "20260810-overview-v128";' in app
    assert 'src="/gui/app.js?v=20260810-overview-v128"' in index
    assert 'if (activePanel === "overview") renderOverviewFailure(error);' in app
    assert "平台状态已返回，但总览渲染失败" in app
