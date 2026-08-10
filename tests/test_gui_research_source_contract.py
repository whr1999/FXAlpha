from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "gui" / "app.js"
INDEX_HTML = ROOT / "gui" / "index.html"


def _app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    marker = f"function {name}"
    start = source.index(marker)
    next_func = source.find("\nfunction ", start + len(marker))
    return source[start:] if next_func == -1 else source[start:next_func]


def test_live_research_digest_uses_console_as_live_source_and_marks_snapshot_offline():
    source = _app_js()
    body = _function_body(source, "liveResearchDigest")

    assert 'source: "live_console"' in body
    assert "is_live: true" in body
    assert "offlineResearchSnapshotDigest()" in body
    assert "state.factorOverviewSnapshot" not in body
    assert "snapshotDigest" not in body
    assert "factorStatus.runtime_view" not in body

    offline_body = _function_body(source, "offlineResearchSnapshotDigest")
    assert 'source: "offline_snapshot"' in offline_body
    assert "is_live: false" in offline_body


def test_research_steps_only_use_snapshot_when_console_is_not_live():
    body = _function_body(_app_js(), "researchSteps")

    assert "researchConsoleIsLive()" in body
    assert "offlineResearchSnapshotDigest().research_steps" in body
    assert "state.factorOverviewSnapshot" not in body
    assert "snapshotDigest" not in body


def test_gui_orchestrator_reads_are_current_run_scoped_without_history_polling():
    source = _app_js()
    events_body = _function_body(source, "orchestratorEventsUrl")
    traces_body = _function_body(source, "orchestratorTracesUrl")
    refresh_body = _function_body(source, "refreshResearchLive")

    assert "activeResearchRunIdForRequest()" in events_body
    assert "run_id=" in events_body
    assert "include_history=false" in events_body
    assert "activeResearchRunIdForRequest()" in traces_body
    assert "run_id=" in traces_body
    assert "include_history=false" in traces_body
    assert "orchestratorEventsUrl()" in refresh_body
    assert '"/factor/research/orchestrator-events?limit=140' not in refresh_body


def test_refresh_state_does_not_build_research_runtime_from_factor_status():
    source = _app_js()
    refresh_body = _function_body(source, "refreshState")

    assert 'state.backendMode = "offline_snapshot"' in refresh_body
    assert "offlineResearchConsoleFromSnapshot(state.factorOverviewSnapshot)" in refresh_body
    assert "statusOutputs.runtime_view" not in refresh_body
    assert "snapshotOutputs.runtime_view" not in refresh_body
    assert "statusOutputs.decision_view" not in refresh_body
    assert "snapshotOutputs.decision_view" not in refresh_body


def test_progress_blocker_color_uses_structured_state_not_candidate_explanation_text():
    source = _app_js()
    blocker_body = _function_body(source, "researchProgressIsBlocked")
    progress_body = _function_body(source, "renderResearchProgressBoard")

    assert 'structuredStates.includes("blocker")' in blocker_body
    assert 'tags.includes("tool_infrastructure_blocker")' in blocker_body
    assert "digest?.blocking_reason" in blocker_body
    assert "transition.why" not in blocker_body
    assert "step?.summary" not in blocker_body
    assert "step?.decision" not in blocker_body
    assert "researchProgressIsBlocked(step, transition, digest)" in progress_body


def test_current_stage_monitor_displays_existing_model_fields_without_gui_translation():
    source = _app_js()
    progress_body = _function_body(source, "renderResearchProgressBoard")

    assert "function researchLlmWaitingNarrative(" in source
    assert "function researchRunningTaskNarrative(" in source
    assert "function directModelNarrative(" not in source
    assert "research_narrative" not in source
    assert "function researchNarrativeText(" not in source
    assert "function researchStageOutcomeNarrative(" not in source
    assert 'thesis_design: Object.freeze({ zh: "研究主线设计", en: "Thesis Design" })' in source
    assert 'start_next_round_at_expression_design: "保留当前主线与假设' in source
    assert '<span>正在判断</span>' not in progress_body
    assert '<span>正在做什么</span>' not in progress_body
    assert '<span>完成以后</span>' not in progress_body
    assert '{ label: "阶段摘要", value: step?.summary, emphasis: true }' in progress_body
    assert '{ label: "研究判断", value: completedTransition.judgment }' in progress_body
    assert '{ label: "判定依据", value: completedTransition.why }' in progress_body
    assert '{ label: "历史依据", value: completedTransition.history_used }' in progress_body
    assert '{ label: "下一阶段理由", value: completedTransition.reason }' in progress_body
    assert 'label: "研究结论"' not in progress_body
    assert 'label: "判断依据"' not in progress_body
    assert 'label: "关键证据"' not in progress_body
    assert 'label: latestIsLlmRequestProgress ? "请求上下文"' not in progress_body
    assert "researchStageHumanLabel(nextStageLabel)" in progress_body
    assert "runningTaskNarrative.title" in progress_body
    assert "runningTaskNarrative.doing" not in progress_body
    assert '<p class="progress-summary-block">' not in progress_body
    assert '<div class="stage-detail-row stage-expression-row">' in progress_body
    assert '<span>执行阶段 <b>' not in progress_body
    assert "const progressStageTitle = progress.hasRunning" in progress_body
    assert "const currentStageTitle = completedStageTitle" in progress_body
    assert "const completedTransition = progress.completedTransition" in progress_body
    assert "const requestIsActive = latestRequestStep" in source
    assert 'isLatest ? "最近记录" : "已完成"' in source
    assert '`下一阶段 ${researchStageHumanLabel(transition.next_stage)}`' in source
    assert "researchActionHumanLabel(step.decision)" not in source


def test_research_stage_names_use_one_bilingual_catalog_across_gui_surfaces():
    source = _app_js()
    flow_body = _function_body(source, "renderResearchFlowTrackerHtml")
    progress_body = _function_body(source, "renderResearchProgressBoard")
    overview_body = _function_body(source, "overviewPhaseLabel")

    assert "const RESEARCH_STAGE_CATALOG = Object.freeze({" in source
    for stage_contract in (
        'protocol_load: Object.freeze({ zh: "研究上下文加载", en: "Research Context Load" })',
        'score_review: Object.freeze({ zh: "快筛评审", en: "Quick Screening Review" })',
        'novelty_review: Object.freeze({ zh: "新颖性评审", en: "Novelty Review" })',
        'deep_validation_review: Object.freeze({ zh: "深度验证评审", en: "Deep Validation Review" })',
        'import_gate_review: Object.freeze({ zh: "入库门评审", en: "Import Gate Review" })',
        'import_review: Object.freeze({ zh: "入库结果评审", en: "Import Result Review" })',
        'round_synthesis: Object.freeze({ zh: "本轮研究总结", en: "Round Synthesis" })',
        'blocker: Object.freeze({ zh: "研究阻塞", en: "Research Blocker" })',
    ):
        assert stage_contract in source

    assert "return researchStageMeta(value).zh" in source
    assert "return researchStageMeta(value).en" in source
    assert "researchStepEnglishTitle(step)" in flow_body
    assert "researchStageEnglishLabel(nextStage)" in flow_body
    assert "completedStageEnglish" in progress_body
    assert "currentStageEnglish" in progress_body
    assert "researchStageMeta(key)" in overview_body
    for retired_visible_alias in (
        "快速评分复盘",
        "快筛复盘",
        "新颖性复盘",
        "深度验证复盘",
        "入库门复盘",
        "导入结果复盘",
    ):
        assert retired_visible_alias not in source


def test_candidate_board_defaults_to_latest_time_sort():
    source = _app_js()
    sort_body = _function_body(source, "candidateSortValue")
    board_body = _function_body(source, "renderCandidateResultTable")

    assert 'candidateSort: "time"' in source
    assert 'case "time"' in sort_body
    assert "candidate?.source_step_ts" in sort_body
    assert '["time", "时间"]' in board_body


def test_gui_uses_unified_orchestrator_control_endpoints_and_single_start_surface():
    source = _app_js()
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'getJsonSafe("/factor/research/control")' in source
    assert 'submitCommandControl("pause")' in source
    assert 'submitCommandControl("resume")' in source
    assert 'submitCommandControl("stop")' in source
    assert 'postJson("/factor/research/guidance"' in source
    assert 'postJson("/factor/tools/research-step"' not in _function_body(source, "refreshResearchLive") + source[source.index('document.getElementById("guidance-form")'):source.index('document.getElementById("load-auto-template")')]
    assert html.count('id="command-start-orchestrator"') == 1
    assert "同步到研究指令台" in html
    assert "发送给当前 ORCH" in html
    assert "所有操作始终显示；后台状态不允许时按钮会置灰。" not in html
