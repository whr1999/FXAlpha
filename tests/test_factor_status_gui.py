import json
from pathlib import Path

from services import factor_research_service


class _FakeRegistry:
    def summary(self):
        return {"total": 0, "active": 0, "retired": 0, "avg_icir": 0}

    def list_active(self, min_icir=-1e9, holding_period_days=5):
        return []


def test_factor_status_exposes_rolling_validation_defaults(monkeypatch):
    monkeypatch.setattr(factor_research_service, "FactorRegistry", lambda: _FakeRegistry())
    monkeypatch.setattr(factor_research_service, "_factor_readiness", lambda url: {})
    monkeypatch.setattr(factor_research_service, "_read_recent_research_steps", lambda limit=20: [])
    monkeypatch.setattr(factor_research_service, "_fetch_quantgpt_recent_tasks", lambda limit=80, **kwargs: [])

    result = factor_research_service.factor_research_status()

    assert result.ok
    assert result.outputs["runtime_defaults"]["selection_end_date"] == "2026-06-30"
    assert result.outputs["runtime_defaults"]["value_end_date"] == "2026-06-30"
    rolling = result.outputs["runtime_defaults"]["rolling_validation"]
    assert rolling == {
        "schema_version": "rolling_validation_v2",
        "score_policy_version": "rolling_ic_recency_robust_v1",
        "max_history_months": 48,
        "min_history_months": 24,
        "period_weights": [0.40, 0.25, 0.15, 0.12, 0.08],
        "stability_penalty": 0.25,
        "rank_ic_full_score": 0.08,
        "min_dates_per_6m": 60,
        "trailing_horizons_months": [6, 12, 24, 36, 48],
    }


def test_research_step_candidate_watch_preserves_compact_rolling_v2_evidence():
    compact = factor_research_service._compact_projection_candidate_watch(
        {
            "candidate_id": "c1",
            "deep_score_policy_version": "deep_score_v2_55_15_20_10",
            "rolling_score": 72.4,
            "rolling_grade": "B",
            "rolling_policy_version": "rolling_ic_recency_robust_v1",
            "rolling_status": "ok",
            "rolling_6m_ic": 0.031,
            "rolling_12m_ic": 0.044,
            "rolling_24m_ic": 0.051,
            "rolling_48m_ic": 0.058,
            "rolling_weighted_ic": 0.047,
            "rolling_weighted_std": 0.009,
            "rolling_robust_ic": 0.04475,
        }
    )

    assert compact["rolling_policy_version"] == "rolling_ic_recency_robust_v1"
    assert compact["rolling_6m_ic"] == 0.031
    assert compact["rolling_robust_ic"] == 0.04475


def test_rolling_task_status_distinguishes_ready_history_and_contract_failures():
    base = {"task_type": "rolling_validation", "status": "completed"}

    assert factor_research_service._task_stage({**base, "result": {"status": "ok"}}) == "rolling_validation_ready"
    assert factor_research_service._task_stage({**base, "result": {"status": "insufficient_history"}}) == "rolling_validation_insufficient_history"
    assert factor_research_service._task_stage({**base, "result": {"status": "label_contract_error"}}) == "rolling_validation_contract_error"


def test_factor_status_read_path_does_not_restart_quantgpt(monkeypatch):
    calls = []

    def fake_ensure(url, *, allow_restart=True):
        calls.append(allow_restart)
        return {"reachable": False, "url": url, "error": "offline"}

    monkeypatch.setattr(factor_research_service, "FactorRegistry", lambda: _FakeRegistry())
    monkeypatch.setattr(factor_research_service, "_ensure_quantgpt_api_reachable", fake_ensure)
    monkeypatch.setattr(factor_research_service, "_read_recent_research_steps", lambda limit=20: [])

    result = factor_research_service.factor_research_status()

    assert result.ok
    assert calls
    assert all(call is False for call in calls)
    assert result.outputs["readiness"]["quantgpt_api"]["service_recovery"]["mode"] == "read_only_probe"


def test_quantgpt_gui_tasks_are_scoped_to_research_run():
    current = {
        "id": "current",
        "params": json.dumps({"run_id": "run-current", "round_id": "run-current:r0002"}),
    }
    old = {
        "id": "old",
        "params": json.dumps({"run_id": "run-old", "round_id": "run-old:r0009"}),
    }
    unlinked = {"id": "unlinked", "params": "{}"}

    assert factor_research_service._quantgpt_task_context(current)["round_id"] == "run-current:r0002"
    assert [item["id"] for item in factor_research_service._quantgpt_tasks_for_research_run(
        [current, old, unlinked], "run-current"
    )] == ["current"]
    assert [item["id"] for item in factor_research_service._quantgpt_tasks_for_research_run(
        [current, old, unlinked], ""
    )] == ["current", "old", "unlinked"]


def test_factor_readiness_labels_explicit_quantgpt_recovery(monkeypatch):
    calls = []

    def fake_ensure(url, *, allow_restart=True):
        calls.append(allow_restart)
        return {"reachable": True, "url": url}

    monkeypatch.setattr(factor_research_service, "_ensure_quantgpt_api_reachable", fake_ensure)

    readiness = factor_research_service._factor_readiness("http://127.0.0.1:8003", allow_quantgpt_restart=True)

    assert calls == [True]
    assert readiness["quantgpt_api"]["service_recovery"] == {
        "allow_restart": True,
        "mode": "explicit_startup_or_recovery",
    }


def test_quantgpt_running_task_kept_when_orch_worker_is_live(monkeypatch):
    summary = {
        "running_count": 1,
        "running_tasks": [
            {
                "id": "score-1",
                "task_type": "score",
                "status": "running",
                "expression": "rank(close)",
            }
        ],
        "latest_task": {"id": "score-1", "status": "running"},
        "latest_non_running_task": {"id": "score-0", "status": "completed"},
        "by_type": {"score": {"running": 1}},
    }
    readiness = {"quantgpt_api": {"active_tasks": 0}}
    monkeypatch.setattr(
        factor_research_service,
        "_live_orchestrator_tool_workers",
        lambda: [
            {
                "pid": 123,
                "tool": "score_factor",
                "candidate_id": "c1",
                "expression": "rank(close)",
                "elapsed": "00:01",
                "rss_mb": 512.0,
            }
        ],
    )

    result = factor_research_service._reconcile_quantgpt_summary_with_readiness(summary, readiness)

    assert result["running_count"] == 1
    assert result["live_active_tasks"] == 1
    assert result["running_reconciled"] == "kept_by_live_orchestrator_tool_worker"
    assert result["running_tasks"][0]["orchestrator_worker_active"] is True
    assert "stale_running_count" not in result


def test_quantgpt_running_task_marked_stale_without_live_worker(monkeypatch):
    summary = {
        "running_count": 1,
        "running_tasks": [
            {
                "id": "score-1",
                "task_type": "score",
                "status": "running",
                "expression": "rank(close)",
            }
        ],
        "latest_task": {"id": "score-1", "status": "running"},
        "latest_non_running_task": {"id": "score-0", "status": "completed"},
        "by_type": {"score": {"running": 1}},
    }
    readiness = {"quantgpt_api": {"active_tasks": 0}}
    monkeypatch.setattr(factor_research_service, "_live_orchestrator_tool_workers", lambda: [])

    result = factor_research_service._reconcile_quantgpt_summary_with_readiness(summary, readiness)

    assert result["running_count"] == 0
    assert result["stale_running_count"] == 1
    assert result["running_reconciled"] == "cleared_by_quantgpt_health_active_tasks_0"


def test_factor_tool_context_exposes_same_rolling_defaults(monkeypatch):
    monkeypatch.setattr(factor_research_service, "FactorRegistry", lambda: _FakeRegistry())
    monkeypatch.setattr(factor_research_service, "_factor_readiness", lambda url, skip_quantgpt_probe=False: {})
    monkeypatch.setattr(factor_research_service, "_latest_stage_transition", lambda: ({}, {}))
    monkeypatch.setattr(factor_research_service, "_quantgpt_field_context", lambda: {})
    monkeypatch.setattr(
        factor_research_service,
        "factor_map_context",
        lambda: {"available": True, "map_id": "fm-test", "regions": []},
    )
    monkeypatch.setattr(
        factor_research_service,
        "factor_map_design_context",
        lambda value, run_id="": {**value, "run_id": run_id or None},
    )

    result = factor_research_service.factor_tool_context(
        skip_quantgpt_probe=True,
        run_id="run-test",
    )

    assert result.ok
    assert result.outputs["config"]["rolling_validation"] == {
        "schema_version": "rolling_validation_v2",
        "score_policy_version": "rolling_ic_recency_robust_v1",
        "max_history_months": 48,
        "min_history_months": 24,
        "period_weights": [0.40, 0.25, 0.15, 0.12, 0.08],
        "stability_penalty": 0.25,
        "rank_ic_full_score": 0.08,
        "min_dates_per_6m": 60,
        "trailing_horizons_months": [6, 12, 24, 36, 48],
    }
    assert result.outputs["config"]["st_exposure_guard_mode"] == "advisory"
    assert result.outputs["config"]["st_exposure_guard_scope"] == "counterfactual_all_market"
    assert result.outputs["config"]["st_exposure_guard_label"] == "distress_proxy_exposure"
    assert result.outputs["config"]["st_exposure_guard_default_behavior"] == "advisory_risk_tag_not_hard_veto"
    assert result.outputs["factor_map_context"] == {
        "available": True,
        "map_id": "fm-test",
        "regions": [],
        "run_id": "run-test",
    }


def test_factor_tool_context_read_path_does_not_restart_quantgpt(monkeypatch):
    calls = []

    def fake_ensure(url, *, allow_restart=True):
        calls.append(allow_restart)
        return {"reachable": False, "url": url, "error": "offline"}

    monkeypatch.setattr(factor_research_service, "FactorRegistry", lambda: _FakeRegistry())
    monkeypatch.setattr(factor_research_service, "_ensure_quantgpt_api_reachable", fake_ensure)
    monkeypatch.setattr(factor_research_service, "_latest_stage_transition", lambda: ({}, {}))
    monkeypatch.setattr(factor_research_service, "_quantgpt_field_context", lambda: {})
    monkeypatch.setattr(factor_research_service, "factor_map_context", lambda: {"available": False})
    monkeypatch.setattr(
        factor_research_service,
        "factor_map_design_context",
        lambda value, run_id="": value,
    )

    result = factor_research_service.factor_tool_context()

    assert result.ok
    assert calls == [False]
    assert result.outputs["readiness"]["quantgpt_api"]["service_recovery"]["mode"] == "read_only_probe"


def test_quantgpt_task_candidates_preserve_stable_score_evidence():
    tasks = [
        {
            "task_id": "score-1",
            "task_type": "score",
            "status": "completed",
            "expression": "rank(close)",
            "created_at": "2026-06-11T10:00:00",
            "completed_at": "2026-06-11T10:00:10",
            "params": {"holding_period": 5},
            "result": {
                "score": 88.0,
                "quick_score": 88.0,
                "grade": "A",
                "component_scores": {"ic_mean": 90.0},
                "backtest_summary": {"ic_mean": 0.05, "ic_ir": 0.8, "annual_return": 0.2, "sharpe": 1.1},
            },
        },
        {
            "task_id": "rolling-1",
            "task_type": "rolling_validation",
            "status": "completed",
            "expression": "rank(close)",
            "created_at": "2026-06-11T10:01:00",
            "completed_at": "2026-06-11T10:01:20",
            "params": {"holding_period": 5},
            "result": {
                "score": 72.0,
                "status": "ok",
                "summary": {"n_windows": 3, "mean_test_ic": 0.03, "mean_test_ir": 0.55},
                "decay_analysis": {"status": "stable", "mean_decay": 0.12},
                "windows": [{"test_ic": 0.03, "test_ir": 0.55}],
            },
        },
        {
            "task_id": "anti-1",
            "task_type": "anti_overfit",
            "status": "completed",
            "expression": "rank(close)",
            "created_at": "2026-06-11T10:02:00",
            "completed_at": "2026-06-11T10:02:20",
            "params": {"holding_period": 5},
            "result": {
                "score": 78.0,
                "recommendation": "acceptable",
                "passed_count": 3,
                "total_count": 4,
                "test_scores": {"ic_stability": 74.0},
                "tests": [{"name": "IC Stability", "passed": True, "details": {"score": 74.0}}],
            },
        },
        {
            "task_id": "adv-1",
            "task_type": "adversarial_validation",
            "status": "completed",
            "expression": "rank(close)",
            "created_at": "2026-06-11T10:03:00",
            "completed_at": "2026-06-11T10:03:20",
            "params": {"holding_period": 5},
            "result": {
                "score": 67.0,
                "recommendation": "borderline",
                "passed_count": 2,
                "total_count": 4,
                "test_scores": {"label_permutation": 70.0},
                "tests": [{"name": "Label Permutation", "passed": True, "details": {"score": 70.0}}],
            },
        },
        {
            "task_id": "gate-1",
            "task_type": "quality_gate",
            "status": "completed",
            "expression": "rank(close)",
            "created_at": "2026-06-11T10:04:00",
            "completed_at": "2026-06-11T10:04:20",
            "params": {"holding_period": 5},
            "result": {
                "adopted": [
                    {
                        "expression": "rank(close)",
                        "quick_score": 88.0,
                        "deep_score": 80.5,
                        "component_scores": {
                            "quick_core": 88.0,
                            "anti_overfit": 78.0,
                            "rolling": 72.0,
                            "adversarial": 67.0,
                            "novelty_bonus": 4.0,
                        },
                        "veto_reasons": [],
                        "rolling_validation": {"score": 72.0, "summary": {"n_windows": 3}},
                        "deep_validation": {
                            "deep_score": 80.5,
                            "score_parts": {
                                "component_scores": {
                                    "quick_core": 88.0,
                                    "anti_overfit": 78.0,
                                    "rolling": 72.0,
                                    "adversarial": 67.0,
                                    "novelty_bonus": 4.0,
                                }
                            },
                        },
                        "gate_result": {"passed": True, "score": 80.5, "deep_score": 80.5},
                        "screening": {"decision": "adopt"},
                    }
                ],
            },
        },
    ]

    candidates = factor_research_service._quantgpt_task_candidates(tasks, factor_library={"items": []}, limit=10)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["quick_score"] == 88.0
    assert candidate["deep_score"] == 80.5
    assert candidate["rolling_validation"]["summary"]["n_windows"] == 3
    assert candidate["anti_overfit_summary"]["test_scores"]["ic_stability"] == 74.0
    assert candidate["adversarial_validation"]["test_scores"]["label_permutation"] == 70.0
    assert candidate["gate_result"]["score"] == 80.5
    assert candidate["deep_validation"]["score_parts"]["component_scores"]["rolling"] == 72.0


def test_quantgpt_task_candidates_compute_deep_score_before_gate():
    tasks = [
        {
            "task_id": "score-1",
            "task_type": "score",
            "status": "completed",
            "expression": "rank(close)",
            "created_at": "2026-06-11T10:00:00",
            "completed_at": "2026-06-11T10:00:10",
            "result": {
                "score": 88.0,
                "quick_score": 88.0,
                "grade": "A",
                "quality_decision": "deep_validate",
                "backtest_summary": {"ic_mean": 0.05, "ic_ir": 0.8, "annual_return": 0.2, "sharpe": 1.1},
            },
        },
        {
            "task_id": "novelty-1",
            "task_type": "novelty_check",
            "status": "completed",
            "created_at": "2026-06-11T10:01:00",
            "completed_at": "2026-06-11T10:01:10",
            "result": {
                "keepers": [
                    {
                        "expression": "rank(close)",
                        "novelty_guard": {
                            "allowed": True,
                            "novelty_score": 0.8,
                            "max_existing_pearson": 0.2,
                            "max_existing_rank_corr": 0.3,
                        },
                    }
                ]
            },
        },
        {
            "task_id": "backtest-1",
            "task_type": "backtest",
            "status": "completed",
            "expression": "rank(close)",
            "created_at": "2026-06-11T10:02:00",
            "completed_at": "2026-06-11T10:02:20",
            "result": {
                "backtest_summary": {"ic_mean": 0.05, "ic_ir": 0.8, "annual_return": 0.2, "sharpe": 1.1},
                "metrics": {"ic_mean": 0.05, "ic_ir": 0.8, "annual_return": 0.2, "sharpe": 1.1},
            },
        },
        {
            "task_id": "anti-1",
            "task_type": "anti_overfit",
            "status": "completed",
            "expression": "rank(close)",
            "created_at": "2026-06-11T10:03:00",
            "completed_at": "2026-06-11T10:03:20",
            "result": {"score": 78.0, "recommendation": "acceptable", "passed_count": 3, "total_count": 4},
        },
        {
            "task_id": "rolling-1",
            "task_type": "rolling_validation",
            "status": "completed",
            "expression": "rank(close)",
            "created_at": "2026-06-11T10:03:30",
            "completed_at": "2026-06-11T10:03:50",
            "result": {
                "status": "ok",
                "score": 72.0,
                "summary": {"status": "ok", "n_windows": 3},
                "windows": [{"test_ic": 0.04, "test_ir": 0.6}],
            },
        },
        {
            "task_id": "adv-1",
            "task_type": "adversarial_validation",
            "status": "completed",
            "expression": "rank(close)",
            "created_at": "2026-06-11T10:04:00",
            "completed_at": "2026-06-11T10:04:20",
            "result": {"score": 67.0, "recommendation": "borderline", "passed_count": 2, "total_count": 4},
        },
    ]

    candidates = factor_research_service._quantgpt_task_candidates(tasks, factor_library={"items": []}, limit=10)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["deep_score"] == 81.2
    assert candidate["deep_validation"]["score_parts"]["deep_score_policy_version"] == "deep_score_v2_55_15_20_10"
    assert candidate["official_grade"] == "B"
    assert candidate["deep_validation"]["source"] == "console_quality_gate_preview"
    assert candidate["deep_validation"]["score_parts"]["missing_components"] == []


def test_candidate_records_merge_orchestrator_monitoring_deep_score():
    steps = [
        {
            "ts": "2026-06-14T10:00:00",
            "stage": "deep_validation_review",
            "decision": "deep_score 69.8<80",
            "monitoring": {
                "candidate_watch": [
                    {
                        "candidate_id": "c1",
                        "expression": "rank(close)",
                        "quick_score": 60.5,
                        "grade": "B",
                    }
                ],
                "evidence_watch": [
                    {
                        "candidate_id": "c1",
                        "tool": "deep_validation",
                        "deep_score": 69.8,
                        "deep_reason": "deep_score_lt_80",
                        "anti_overfit_score": 85.0,
                        "adversarial_score": 74.0,
                    }
                ],
            },
        }
    ]

    records = factor_research_service._candidate_records_from_research_steps(steps, limit=10)

    assert len(records) == 1
    assert records[0]["expression"] == "rank(close)"
    assert records[0]["deep_score"] == 69.8
    assert records[0]["anti_overfit_score"] == 85.0
    assert records[0]["adversarial_score"] == 74.0


def test_candidate_plan_precheck_projection_reaches_candidate_records():
    event = {
        "ts": "2026-07-01T10:00:00",
        "run_id": "fr_test",
        "round_id": "fr_test:r0001",
        "stage_id": "fr_test:r0001:s05_candidate_plan",
        "stage_seq": 5,
        "stage": "candidate_plan",
        "summary": "candidate_plan complete",
        "decision": "validate kept candidates",
        "stage_transition": {"next_stage": "score_review", "next_action": "validate_and_score_candidates"},
        "candidate_lanes": [
            {
                "candidate_id": "c1",
                "candidate_lane": "precheck_blocked",
                "screening_stage": "precheck_blocked",
                "status": "blocked",
                "expression": "rank(foo)",
                "precheck_warnings": ["unsupported_fields:foo"],
                "precheck_instruction": "drop_candidate",
                "status_label": "表达式预检拦截",
            },
            {
                "candidate_id": "c2",
                "candidate_lane": "planned_for_score",
                "screening_stage": "candidate_plan",
                "status": "planned_for_score",
                "expression": "rank(close)",
            },
        ],
        "evidence_refs": [{"tool": "candidate_plan_code_precheck", "fatal_count": 1, "warning_count": 1, "fatal_candidate_ids": ["c1"]}],
        "tags": ["orchestrator", "candidate_plan"],
    }

    projected = factor_research_service._orchestrator_event_projection(event)
    records = factor_research_service._candidate_records_from_research_steps([projected], limit=10)

    by_expr = {item["expression"]: item for item in records}
    assert by_expr["rank(foo)"]["screening_stage"] == "precheck_blocked"
    assert by_expr["rank(foo)"]["status"] == "blocked"
    assert by_expr["rank(foo)"]["precheck_warnings"] == ["unsupported_fields:foo"]
    assert by_expr["rank(close)"]["status"] == "planned_for_score"


def test_candidate_records_merge_orchestrator_event_deep_score(monkeypatch, tmp_path):
    event_file = tmp_path / "events.jsonl"
    event_file.write_text(
        json.dumps(
            {
                "ts": "2026-06-14T10:00:00",
                "round_id": "fr_test:r0025",
                "stage_id": "fr_test:r0025:s08_deep_validation_review",
                "stage": "deep_validation_review",
                "decision": "deep_score 69.8<80",
                "candidate_lanes": [
                    {
                        "candidate_id": "c1",
                        "expression": "rank(close)",
                        "quick_score": 60.5,
                        "grade": "B",
                    }
                ],
                "evidence_refs": [
                    {
                        "candidate_id": "c1",
                        "tool": "deep_validation",
                        "deep_score": 69.8,
                        "anti_overfit_score": 85.0,
                        "adversarial_score": 74.0,
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(factor_research_service, "FACTOR_ORCHESTRATOR_EVENTS_FILE", event_file)

    records = factor_research_service._candidate_records_from_orchestrator_events(limit=10)

    assert len(records) == 1
    assert records[0]["expression"] == "rank(close)"
    assert records[0]["round_id"] == "fr_test:r0025"
    assert records[0]["candidate_id"] == "c1"
    assert records[0]["deep_score"] == 69.8


def test_gui_candidate_table_exposes_rolling_validation_summary():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")

    assert "function rollingValidationLabel" in app_js
    assert "function candidateIdentityParts" in app_js
    assert "candidate-expression-brief" in app_js
    assert "<th>Rolling</th>" in app_js


def test_gui_candidate_blockers_use_danger_tone_and_pinned_expression_wraps():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    styles = project_root.joinpath("gui/styles.css").read_text(encoding="utf-8")

    assert 'if (/失败|异常|入库拒绝|拦截|预检|reject|veto/.test(value)) return "danger";' in app_js
    assert 'if (/拦截|预检/.test(value)) return "warn";' not in app_js
    assert 'class="pinned-candidate-expression-label">表达式</span>' in app_js
    assert '<code>${escapeHtml(candidate.expression || identity.subtitle || "等待表达式")}</code>' in app_js
    expression_css = styles.split(".pinned-candidate-expression {", 1)[1].split(".pinned-metrics {", 1)[0]
    assert "grid-template-columns: auto minmax(0, 1fr)" in expression_css
    assert "overflow-wrap: anywhere" in expression_css
    assert "white-space: normal" in expression_css
    danger_css = styles.split(".stage-chip.tone-danger,", 1)[1].split(".stage-chip.tone-ok,", 1)[0]
    assert "rgba(159, 18, 57, 0.26)" in danger_css


def test_gui_research_steps_render_precheck_and_code_keeper_evidence():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")

    assert "function evidenceRefLabel" in app_js
    assert "candidate_plan_code_precheck" in app_js
    assert "表达式预检" in app_js
    assert "candidate_plan_llm_budget_triage" in app_js
    assert "候选规划去重" in app_js
    assert "code_advice_keeper" in app_js
    assert "代码硬证据放行" in app_js
    assert "precheck_blocked" in app_js
    assert "表达式预检拦截" in app_js
    assert "precheck_warnings" in app_js
    assert "deep_validation?.rolling_validation" in app_js
    assert "LS诊断" not in app_js


def test_gui_flow_tracker_shows_compact_run_and_round_identity_card():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    styles = project_root.joinpath("gui/styles.css").read_text(encoding="utf-8")

    assert "function compactResearchRunRoundIdentity" in app_js
    assert "research-flow-current-row" in app_js
    assert "research-flow-id-card" in app_js
    assert 'const runDateKey = runDateMatch ? `${runDateMatch[2]}${runDateMatch[3]}` : "----"' in app_js
    assert "runValue: runDateKey" in app_js
    assert 'roundValue: latestResearchRound ? String(latestResearchRound.roundNo).padStart(4, "0")' in app_js
    assert 'const latestStageState = [' in app_js
    assert '? "STOP"' in app_js
    assert 'stageValue,' in app_js
    assert "research-flow-id-segment stage" in app_js
    assert ".research-flow-current-row" in styles
    assert ".research-flow-id-card" in styles
    assert ".research-flow-id-segment" in styles
    assert "grid-template-columns: max-content minmax(0, 1fr)" in styles
    assert "width: max-content" in styles


def test_gui_flow_tracker_uses_current_round_and_collapses_request_result_pairs():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")

    assert "function researchStepDisplayPriority" in app_js
    assert "function dedupeResearchStepsForFlow" in app_js
    assert "dedupeResearchStepsForFlow(flowStepsForCurrentRound()" in app_js
    assert "return steps.slice(-limit);" in app_js


def test_gui_compact_system_strip_omits_research_step_total_chip():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")

    assert '<span>研究步骤 ${escapeHtml(text(stepTotal, "0"))}</span>' not in app_js


def test_model_gui_exposes_rolling_and_research_candidate_backtests():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    index_html = project_root.joinpath("gui/index.html").read_text(encoding="utf-8")

    assert 'new Set(["research", "candidate", "production"])' in app_js
    assert "生产 Rolling · 四折拼接" in app_js
    assert "正式 Rolling 未通过" in app_js
    assert "三Seed审计轮" in app_js
    assert "function modelRollingBacktestOptions(campaigns = [])" in app_js
    assert "function mergeRollingCampaignCatalog(...sources)" in app_js
    assert "const rollingCampaigns = mergeRollingCampaignCatalog(" in app_js
    assert "backtest.rolling_campaigns," in app_js
    assert "model.rolling_campaigns," in app_js
    assert 'params.set("rolling_campaign_id", selectedModelRunId)' in app_js
    assert "const backtestOptions = [...rollingBacktestOptions, ...registryBacktestOptions]" in app_js
    assert "visibleBacktestOptions.map(renderBacktestMenuOption)" in app_js
    assert "选择模型或 Rolling" in app_js
    assert 'data-model-backtest-role="rolling_campaign"' in app_js
    assert "const rollingCandidateRows = rollingCampaigns.slice(0, 10).map" in app_js
    assert "Seed42 拼接年化" in app_js
    assert "Rolling 稳定性复核未通过" in app_js
    assert "不生成候选模型" in app_js
    assert 'rollingCampaignComplete\n      ? "failed"' in app_js
    assert 'params.set("rolling_seed"' not in app_js
    assert 'data-rolling-seed=' not in app_js
    assert "Rolling 准入分" in app_js
    assert "正式曲线：Seed42" in app_js
    assert "Seed17/83 只做审计" in app_js
    assert "加载 Seed42 逐日明细" in app_js
    assert "逐日持仓与贡献默认不加载" in app_js
    assert 'backtestModel.role === "rolling_campaign" ? ""' in app_js
    assert "dailyReturn - dailyBenchmarkReturn" in app_js
    assert "Seed 稳定性审计" in app_js
    assert "Seed17/83 不作为正式模型列出" in app_js
    assert 'modelRunId: "",\n        label: "选择模型或 Rolling",\n        role: ""' in app_js
    assert 'document.querySelector(".backtest-select-panel:not(.is-hidden)")' in app_js
    runtime_template = app_js.split('document.getElementById("model-runtime-detail").innerHTML = `', 1)[1].split("`;", 1)[0]
    assert "rollingCampaignPanel" not in runtime_template
    assert "modelLifecyclePanel" in runtime_template
    assert "model-stage-flow-v3" not in runtime_template
    assert "四折滚动初筛" in app_js
    assert "跨种子滚动复核" in app_js
    assert "生产模型发布" in app_js
    assert "查看 Rolling 详情" in app_js
    assert 'setModelWorkspace("backtest");\n    await loadSelectedModelBacktest();' in app_js
    assert 'const isRollingBacktest = backtestModel.role === "rolling_campaign";' in app_js
    assert 'class="surface model-library-feature-sets model-library-secondary-catalog"' in index_html


def test_model_result_cards_show_feature_set_and_use_layered_layout():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    index_html = project_root.joinpath("gui/index.html").read_text(encoding="utf-8")
    styles = project_root.joinpath("gui/styles.css").read_text(encoding="utf-8")

    assert "const candidateFeatureSetId = text(" in app_js
    assert "seed.metadata?.feature_set_id" in app_js
    assert "const rollingFeatureSetId = text(" in app_js
    assert "campaign.feature_set_id" in app_js
    assert app_js.count('class="model-candidate-feature-set"') == 2
    assert "Feature Set：${candidateFeatureSetId}" in app_js
    assert "Feature Set：${rollingFeatureSetId}" in app_js
    assert ".model-candidate-feature-set {" in styles
    assert "#model-live-assets .model-candidate-title {" in styles
    assert "#model-live-assets .model-candidate-metrics {" in styles
    assert 'src="/gui/app.js?v=' in index_html
    assert "/gui/styles.css?v=20260809-paper-console-layout-v126" in index_html


def test_model_backtest_gui_separates_qlib_annualized_nav_and_rolling_diagnostics():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    styles = project_root.joinpath("gui/styles.css").read_text(encoding="utf-8")

    assert "真实净值表现" in app_js
    assert "相对基准表现" in app_js
    assert "交易成本与执行" in app_js
    assert "Rolling 验证诊断" in app_js
    assert "策略净值 ÷ 基准净值 − 1" in app_js
    assert "总年化 − 成本后超额年化" in app_js
    assert "累计收益来自逐日复利，不用年化值倒推" in app_js
    assert "成本后策略累计" in app_js
    assert "相对基准累计" in app_js
    assert "selectedRollingFolds" in app_js
    assert "最差折质量分" in app_js
    assert "最新折质量分" in app_js
    assert ".backtest-metric-section" in styles
    assert ".chart-fold-boundary" in styles


def test_model_backtest_sort_controls_reorder_and_explain_the_visible_list():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    styles = project_root.joinpath("gui/styles.css").read_text(encoding="utf-8")

    assert 'modelBacktestSortDirection: "desc"' in app_js
    assert 'function sortModelBacktestOptions(options = [], sortKey = "time", direction = "desc")' in app_js
    assert "item.finished_at || item.created_at || item.started_at || item.updated_at || item.train_end" in app_js
    assert 'state.modelBacktestSortDirection = currentSort === sortKey' in app_js
    assert 'state.modelBacktestSortDirection === "asc" ? "desc" : "asc"' in app_js
    assert "rankedBacktestOptions(researchBacktestOptions, \"research\")" in app_js
    assert 'aria-pressed="${backtestSort === key}"' in app_js
    assert 'class="backtest-menu-sort-status" aria-live="polite"' in app_js
    assert 'class="backtest-menu-option-rank"' in app_js
    assert 'class="backtest-menu-option-sort-value"' in app_js
    assert ".backtest-menu-sort-row .tiny-button.active {" in styles
    assert ".backtest-menu-option-sort-value {" in styles


def test_model_research_date_helper_is_initialized_before_review_rendering():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")

    helper_definition = app_js.index("const compactDateId = (value) => {")
    review_rendering = app_js.index("const reviewExecutionMeta = isResearchConfirmationFailure")

    assert helper_definition < review_rendering


def test_global_mini_metrics_fall_back_to_factor_status_on_model_pages():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")

    assert "const registry = factorConsole.registry_summary || factorStatus.registry_summary || {};" in app_js
    assert "const runtime = factorConsole.runtime_view || factorStatus.runtime_view || digest.runtime_view || {};" in app_js
    assert "const runView = factorConsole.run_view || factorStatus.run_view || serviceOutputs(state.factorRunView);" in app_js
    assert "const wantsFactorStatus = !wantsOverview && wantsFactorLibrary;" in app_js
    assert 'wantsFactorStatus ? getJsonSafe("/factor/status", overviewReadOptions)' in app_js


def test_gui_research_projection_and_factor_map_workspace_contract():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    index_html = project_root.joinpath("gui/index.html").read_text(encoding="utf-8")

    assert "function researchProjection() {\n  const factorConsole = serviceOutputs(state.factorConsole);" in app_js
    assert "function isCurrentOrchestratorMode() {\n  const factorConsole = serviceOutputs(state.factorConsole);" in app_js
    assert 'src="/gui/app.js?v=' in index_html
    assert '{ kind: "workspace", workspace: "knowledge", label: "因子地图"' in app_js
    assert 'getJsonSafe("/factor/map"' in app_js
    assert "function renderFactorMapWorkspace()" in app_js
    assert "活跃因子库的信息家族关系，与本轮研究在各家族的覆盖轨迹" in app_js
    assert "地图仅作研究辅助，不参与评分、门禁或导入" in app_js
    assert "主要结构：" in app_js
    assert "因子家族关系图" in app_js
    assert "区域研究热度" in app_js
    assert "data-factor-map-region" in app_js
    assert "历史经验迁移" not in app_js
    assert 'data-factor-map-refresh' in app_js
    assert "state.factorMapLoading" in app_js


def test_gui_orchestrator_live_strip_reuses_deepseek_official_balance():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    index_html = project_root.joinpath("gui/index.html").read_text(encoding="utf-8")

    assert "function deepSeekOfficialBalanceDisplay(balance = {})" in app_js
    assert "function researchDeepSeekBalanceChipHtml()" in app_js
    assert 'if (!isCurrentOrchestratorMode()) return "";' in app_js
    assert '<b>DeepSeek 余额</b>' in app_js
    assert 'getJsonSafe(`/platform/runtime-status${wantsOverview ? "?compact=true" : ""}`' in app_js
    assert 'if (isCurrentOrchestratorMode()) {\n        getJsonSafe("/platform/runtime-status"' in app_js
    assert app_js.count("deepSeekOfficialBalanceDisplay(") >= 4
    assert 'src="/gui/app.js?v=' in index_html
    assert "/gui/styles.css?v=20260809-paper-console-layout-v126" in index_html


def test_gui_orchestrator_model_switch_is_run_pinned_and_receipt_verified():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    index_html = project_root.joinpath("gui/index.html").read_text(encoding="utf-8")
    styles = project_root.joinpath("gui/styles.css").read_text(encoding="utf-8")
    api_server = project_root.joinpath("api_server.py").read_text(encoding="utf-8")
    command_actions_rule = styles.split(".command-actions {", 1)[1].split("}", 1)[0]
    command_button_rule = styles.split(".command-actions button {", 1)[1].split("}", 1)[0]

    assert 'data-command-llm-model="deepseek-v4-pro"' in index_html
    assert 'data-command-llm-model="deepseek-v4-flash"' in index_html
    assert 'name="llm_model" type="hidden"' in index_html
    assert 'class="command-execution-head"' in index_html
    assert 'class="command-action-board"' in index_html
    assert 'class="command-action-label">运行操作</span>' in index_html
    assert 'class="command-action-label">辅助操作</span>' in index_html
    assert index_html.index('id="command-control-note"') < index_html.index('class="command-llm-model-control"')
    assert "function syncCommandLlmModelControl(" in app_js
    assert "function commandLlmModelContractReady(defaults = {})" in app_js
    assert '"running", "pause_requested", "paused", "resume_requested", "stop_requested"' in app_js
    assert 'llm_model: normalizeCommandLlmModel(data.get("llm_model"))' in app_js
    assert '"orchestration_mode", "evaluation_mode", "llm_model"' in app_js
    assert "button.disabled = locked" in app_js
    assert "startButton.disabled = !canStart || !llmModelContractReady" in app_js
    assert "后端模型选择契约待安全重载；当前禁止启动新任务" in app_js
    assert 'statusRow("当前运行", runStatusChips)' in app_js
    assert 'statusRow("启动检查", readinessChips)' in app_js
    assert ".command-llm-model-switch button.is-active" in styles
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in command_actions_rule
    assert "height: 46px" in command_button_rule
    assert "white-space: nowrap" in command_button_rule
    assert 'llm_model=str(body.get("llm_model") or "").strip() or None' in api_server


def test_gui_factor_map_replaces_legacy_experience_library():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    styles = project_root.joinpath("gui/styles.css").read_text(encoding="utf-8")
    factor_map_css = styles.split(".factor-map-surface {", 1)[1].split(".thesis-card-list {", 1)[0]

    assert 'class="ghost refresh-action factor-map-refresh-action' in app_js
    assert 'class="factor-map-kpi-grid"' in app_js
    assert 'class="factor-map-visual-grid"' in app_js
    assert 'class="factor-map-network-panel factor-map-global-panel"' in app_js
    assert 'class="factor-map-global-graph"' in app_js
    assert 'class="factor-map-heat-grid"' in app_js
    assert 'class="factor-map-region-grid"' in app_js
    assert 'class="factor-map-trajectory-list"' in app_js
    assert "border-radius: var(--radius-xl)" in factor_map_css
    assert "border-radius: var(--radius-lg)" in factor_map_css
    assert "border-radius: var(--radius-md)" in factor_map_css
    for retired_token in (
        "experienceLibrary",
        "experience-filter-form",
        "experience-audit",
        "experience-card",
        "data-experience-",
    ):
        assert retired_token not in app_js
    for off_brand_purple in ("139, 92, 246", "167, 139, 250", "76, 29, 149", "109, 40, 217"):
        assert off_brand_purple not in factor_map_css


def test_gui_research_lifecycle_controls_remain_visible_and_use_backend_permissions():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    index_html = project_root.joinpath("gui/index.html").read_text(encoding="utf-8")

    for control_id in (
        "command-start-orchestrator",
        "command-pause-orchestrator",
        "command-resume-orchestrator",
        "command-stop-orchestrator",
    ):
        tag = index_html.split(f'id="{control_id}"', 1)[0].rsplit("<button", 1)[-1]
        assert " hidden" not in tag
    assert 'pauseButton.hidden = false' in app_js
    assert 'pauseButton.disabled = !canPause' in app_js
    assert 'resumeButton.hidden = false' in app_js
    assert 'resumeButton.disabled = !canResume' in app_js
    assert 'stopButton.hidden = false' in app_js
    assert 'stopButton.disabled = !canStop' in app_js
    assert "上一轮研究已结束：可以启动新 run" in app_js
    assert 'workspace: "guidance", label: "研究干预"' not in app_js
    assert 'id="command-open-run"' in index_html
    assert 'id="command-open-guidance"' in index_html
    assert "候选数是上限，不要求模型每轮凑满" in index_html
    assert "action === \"stop\" && !window.confirm" in app_js
    assert '(wantsOverview || wantsResearchPanel) ? getJsonSafe(`/factor/research/control${wantsOverview ? "?compact=true" : ""}`' in app_js
    assert "state.factorResearchControl = keepPreviousOnReadFailure" in app_js


def test_gui_evaluation_mode_switch_is_scoped_to_research_command_console():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    index_html = project_root.joinpath("gui/index.html").read_text(encoding="utf-8")

    brand_start = index_html.index('class="brand"')
    nav_start = index_html.index('id="platform-nav"')
    indicator_position = index_html.index('id="evaluation-mode-indicator"')
    command_start = index_html.index('id="workspace-command"')
    switch_position = index_html.index('id="evaluation-mode-bar"')
    command_form_position = index_html.index('id="orchestrator-command-form"')
    command_end = index_html.index('id="workspace-run"')

    assert brand_start < indicator_position < nav_start
    assert command_start < switch_position < command_form_position < command_end
    assert index_html.count('id="evaluation-mode-bar"') == 1
    assert 'data-evaluation-mode="research"' in index_html
    assert 'data-evaluation-mode="production"' in index_html
    assert 'document.getElementById("evaluation-mode-indicator")' in app_js
    assert 'indicator.dataset.mode = activeMode' in app_js


def test_model_research_has_a_real_command_console_workspace():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    index_html = project_root.joinpath("gui/index.html").read_text(encoding="utf-8")

    panel_start = index_html.index('id="panel-model-research"')
    command_tab = index_html.index('data-model-workspace="command"', panel_start)
    live_tab = index_html.index('data-model-workspace="live"', panel_start)
    command_section = index_html.index('id="model-workspace-command"', panel_start)
    command_form = index_html.index('id="model-command-form"', command_section)
    live_section = index_html.index('id="model-workspace-live"', command_section)
    guidance_form_start = index_html.index('id="model-form"', live_section)
    guidance_form_end = index_html.index('</form>', guidance_form_start)
    guidance_form = index_html[guidance_form_start:guidance_form_end]

    assert command_tab < live_tab
    assert command_section < command_form < live_section
    assert '研究指令台' in index_html[command_tab:live_tab]
    for field in (
        "evaluation_mode", "feature_set_id", "model_orch_rounds", "max_stage", "execute_qlib",
        "write_registry", "source_round_group_id", "production_write_registry",
    ):
        assert f'name="{field}"' in index_html[command_form:live_section]
    for mode in ("research", "production"):
        assert f'data-model-evaluation-mode="{mode}"' in index_html[command_section:live_section]
        assert f'data-model-mode-pane="{mode}"' in index_html[command_form:live_section]
    for parameter in (
        "learning_rate", "n_estimators", "early_stopping_rounds", "num_leaves", "max_depth",
        "min_data_in_leaf", "feature_fraction", "bagging_fraction", "bagging_freq", "lambda_l1",
        "lambda_l2", "bin_construct_sample_cnt",
    ):
        assert f'data-model-param="{parameter}"' in index_html[command_form:live_section]
    for control_id in ("start-model-orch", "stop-model-orch", "resume-model-orch", "refresh-model-command"):
        assert f'id="{control_id}"' in index_html[command_form:live_section]
        assert f'id="{control_id}"' not in guidance_form
    assert 'localStorage.getItem("fxalpha.activeModelWorkspace") || "command"' in app_js
    assert 'document.getElementById("model-command-form")' in app_js
    assert 'evaluation_mode: "research"' in app_js
    assert 'evaluation_mode: "production"' in app_js
    assert "baseline_model_params: baselineOverrides" in app_js
    assert 'source_round_group_id: sourceRoundGroupId' in app_js
    assert 'function validateModelCommandBaselineParams()' in app_js
    assert "contract.qlib_official_alpha158_lgbm_params" in app_js
    assert 'data-model-param-preset' in index_html[command_form:live_section]
    assert 'max_stage: form.get("max_stage") || "round_synthesis"' in app_js
    assert 'getJsonSafe(`${MODEL_API_PREFIX}/preflight${query}`)' in app_js


def test_model_research_command_console_selects_catalog_or_custom_factor_snapshot():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    index_html = project_root.joinpath("gui/index.html").read_text(encoding="utf-8")
    styles = project_root.joinpath("gui/styles.css").read_text(encoding="utf-8")
    api_server = project_root.joinpath("api_server.py").read_text(encoding="utf-8")

    command_start = index_html.index('id="model-workspace-command"')
    command_end = index_html.index('id="model-workspace-live"', command_start)
    command = index_html[command_start:command_end]

    for marker in (
        'data-model-feature-source="catalog"', 'data-model-feature-source="custom"',
        'id="model-feature-set-select"', 'id="model-factor-picker"',
        'id="model-factor-recommendations"', 'id="freeze-model-feature-set"',
        'id="model-feature-audit-status"', 'id="model-refresh-factor-audit"',
        'data-model-protocol-preset="complete"', 'data-model-protocol-preset="screen"',
        'data-model-protocol-preset="plan"', 'id="model-launch-review"',
    ):
        assert marker in command
    assert 'getJsonSafe(`${MODEL_API_PREFIX}/feature-sets?limit=100&compact=true`' in app_js
    assert 'limit=${wantsOverview ? "1" : "500"}&sort_by=icir' in app_js
    assert 'factor_ids: [...state.modelSelectedFactorIds]' in app_js
    assert 'source_feature_set_id: source.feature_set_id' in app_js
    assert 'postJson(`${MODEL_API_PREFIX}/tools/feature-snapshot`' in app_js
    assert 'runFactorAuditFromGui("information")' in app_js
    assert 'renderModelFactorAuditBridge()' in app_js
    assert 'source_type=body.get("source_type") or None' in api_server
    assert '.model-command-overview {' in styles
    assert '.model-factor-picker {' in styles
    assert '.model-feature-audit-bridge {' in styles
    assert '.model-launch-review-card {' in styles


def test_gui_command_direction_is_compact_and_launch_receipt_is_verified():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    index_html = project_root.joinpath("gui/index.html").read_text(encoding="utf-8")
    styles = project_root.joinpath("gui/styles.css").read_text(encoding="utf-8")

    direction_rule = styles.split(".command-direction textarea {", 1)[1].split("}", 1)[0]
    objective_head_rule = styles.split(".command-objective-card .command-block-head {", 1)[1].split("}", 1)[0]
    objective_title_rule = styles.split(".command-objective-card .command-block-head h3 {", 1)[1].split("}", 1)[0]
    objective_hint_rule = styles.split(".command-objective-card .command-block-head small {", 1)[1].split("}", 1)[0]
    assert "height: 58px" in direction_rule
    assert "min-height: 58px" in direction_rule
    assert "display: grid" in objective_head_rule
    assert "grid-template-columns: minmax(0, 1fr)" in objective_head_rule
    assert "white-space: nowrap" in objective_title_rule
    assert "max-width: none" in objective_hint_rule
    assert "text-align: left" in objective_hint_rule
    assert "/gui/styles.css?v=20260809-paper-console-layout-v126" in index_html
    for field in (
        "direction", "universe", "start_date", "end_date", "holding_period", "benchmark",
        "target_adopted", "n_candidates", "n_rounds", "top_frac", "cost_rate",
        "neutralize_cap", "submit_wq", "orchestration_mode",
    ):
        assert f'"{field}"' in app_js
    assert "commandLaunchReceiptMismatches(requested, result.inputs || {})" in app_js
    assert "启动回执参数不一致" in app_js
    assert "Orchestrator 已按本页参数启动" in app_js


def test_gui_guidance_is_integrated_into_command_console_with_real_llm_receipt():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    index_html = project_root.joinpath("gui/index.html").read_text(encoding="utf-8")

    command_start = index_html.index('id="workspace-command"')
    command_end = index_html.index('id="workspace-run"')
    guidance_position = index_html.index('id="command-guidance"')
    assert command_start < guidance_position < command_end
    assert 'id="workspace-guidance"' not in index_html
    assert 'id="event-timeline"' not in index_html
    assert "function renderTimeline(" not in app_js
    assert "function guidanceImpactRecords(" in app_js
    assert 'ref.type !== "operator_guidance_delivery"' in app_js
    assert 'tags.includes("llm_result")' in app_js
    assert "一次性干预：只进入下一次 LLM 判断" not in index_html
    assert "command-step-index" not in index_html
    assert "guidance-current-grid" in app_js
    assert "guidance-receipt-history" in app_js
    assert 'maxlength="500"' in index_html
    assert "正在提交一次性干预" in app_js
    assert "submitButton.disabled = true" in app_js
    assert "const guidanceRecords = guidanceImpactRecords(researchSteps()" in app_js
    assert "guidanceApplied?.updated_direction_hint" not in app_js


def test_live_research_step_compacts_recovery_checkpoint_without_losing_resume_identity():
    checkpoint = {
        "type": "orchestrator_recovery_checkpoint",
        "run_id": "fr_test",
        "round_id": "fr_test:r0006",
        "stage": "candidate_plan",
        "resume_stage": "score_review",
        "thesis": {"text": "x" * 8000},
        "hypothesis": {"text": "y" * 8000},
        "candidates": [{"candidate_id": f"c{i}", "expression": "rank(close)"} for i in range(10)],
        "planned_candidates": [{"candidate_id": f"c{i}"} for i in range(4)],
        "completed_task_refs": [f"task-{i}" for i in range(3)],
    }

    compact = factor_research_service._compact_research_step_for_live(
        {"run_id": "fr_test", "round_id": "fr_test:r0006", "evidence_refs": [checkpoint]}
    )
    ref = compact["evidence_refs"][0]

    assert ref["type"] == "orchestrator_recovery_checkpoint"
    assert ref["round_id"] == "fr_test:r0006"
    assert ref["resume_stage"] == "score_review"
    assert ref["candidate_count"] == 10
    assert ref["planned_candidate_count"] == 4
    assert ref["completed_task_refs"] == ["task-0", "task-1", "task-2"]
    assert "thesis" not in ref
    assert "hypothesis" not in ref
    assert len(json.dumps(compact, ensure_ascii=False)) < 3000


def test_live_digest_does_not_duplicate_top_level_run_view():
    compact = factor_research_service._compact_live_digest_for_console(
        {
            "run_id": "fr_test",
            "current_phase": "Score Review",
            "current_candidate_board": {"candidates": [{"candidate_id": "r0001:c1"}]},
            "run_view": {"events": [{"summary": "x" * 10000}]},
        }
    )

    assert compact["run_id"] == "fr_test"
    assert compact["current_phase"] == "Score Review"
    assert "current_candidate_board" not in compact
    assert "run_view" not in compact


def test_api_start_preserves_production_neutralize_cap_default():
    project_root = Path(__file__).resolve().parents[1]
    api_source = project_root.joinpath("api_server.py").read_text(encoding="utf-8")
    defaults = factor_research_service.factor_research_runtime_defaults()

    assert defaults["neutralize_cap"] is True
    assert defaults["default_neutralize_cap"] is True
    assert '_body_bool(body, "neutralize_cap", False)' not in api_source
    assert 'defaults.get("neutralize_cap", defaults.get("default_neutralize_cap", True))' in api_source


def test_frontend_semantics_drop_cloud_pass_and_mark_diagnostics():
    project_root = Path(__file__).resolve().parents[1]
    robustness = project_root.joinpath("third_party/quantgpt/frontend/src/components/RobustnessCard.tsx").read_text(encoding="utf-8")
    results = project_root.joinpath("third_party/quantgpt/frontend/src/components/ResultsDashboard.tsx").read_text(encoding="utf-8")
    research = project_root.joinpath("third_party/quantgpt/frontend/src/components/ResearchDashboard.tsx").read_text(encoding="utf-8")

    assert "Cloud predicted" not in robustness
    assert "Negative return cap" in robustness
    assert "Diagnostic-only" in results
    assert "L/S Sharpe (diag)" in research


def test_model_research_log_is_session_scoped_and_workflow_driven():
    project_root = Path(__file__).resolve().parents[1]
    index_html = project_root.joinpath("gui/index.html").read_text(encoding="utf-8")
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    styles = project_root.joinpath("gui/styles.css").read_text(encoding="utf-8")

    assert "复盘大模型输入、响应、参数理由与平台执行回执" in index_html
    assert 'activeModelLogView: localStorage.getItem("fxalpha.activeModelLogView")' in app_js
    assert "belongsToModelLogSession" in app_js
    assert 'row.session_id === modelLogSessionId' in app_js
    assert 'data-model-log-view="${value}"' in app_js
    assert "大模型交互详情" in app_js
    assert "平台发送给大模型" in app_js
    assert "大模型返回" in app_js
    assert "模型建议后的执行记录" in app_js
    assert "model-log-workspace-v2" in styles


def test_model_names_use_one_display_contract_and_keep_internal_ids_secondary():
    project_root = Path(__file__).resolve().parents[1]
    app_js = project_root.joinpath("gui/app.js").read_text(encoding="utf-8")
    naming_doc = project_root.joinpath("docs/MODEL_NAMING_CONTRACT.md").read_text(encoding="utf-8")

    assert 'const MODEL_DISPLAY_NAMING_VERSION = "model_display_v1"' in app_js
    assert "function canonicalModelDisplayName" in app_js
    assert 'value !== undefined && value !== null && value !== ""' in app_js
    assert 'canonicalModelDisplayName(campaign, { kind: "rolling" })' in app_js
    assert "model_display_v1" in naming_doc
    assert "Round 0" in naming_doc
