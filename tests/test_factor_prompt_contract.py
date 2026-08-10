from __future__ import annotations

import re
from pathlib import Path

from services import factor_research_service as svc


ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / "third_party" / "quantgpt" / "PROMPT.md"
README = ROOT / "domain" / "factor_research" / "README.md"
ORCHESTRATOR_README = ROOT / "domain" / "factor_research" / "ORCHESTRATOR_README.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_expression_briefing_requires_leg_direction_consistency():
    briefing = svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]

    assert "对每条腿做方向检查" in briefing
    assert "原始字段怎样变化 → 变换后腿值怎样变化 → 最终因子高值代表什么" in briefing
    assert "目标经济场景必须使每条腿都取高值" in briefing
    assert "where 必须解释条件和两个分支" in briefing
    assert "ts_av_diff(x, window)" in briefing
    assert "使用 ts_std" in briefing
    assert "3至5个具有独立研究价值的候选" in briefing
    assert "定向EXPLOIT或SIMPLIFY只生成1至2个候选" in briefing


def test_candidate_plan_briefing_reviews_leg_direction_without_hard_drop():
    briefing = svc._ORCHESTRATOR_STAGE_BRIEFINGS["candidate_plan"]

    assert "逐候选核对字段合法性、每条腿方向" in briefing
    assert "无法明确判断的合法候选默认 score" in briefing
    assert "最终因子高值是否真的对应 expected_direction" in briefing
    assert "tool_evidence.operator_contract" in briefing
    assert "decision、summary、judgment、why 必须与最终 candidate_lanes 一致" in briefing
    assert "若全部 candidate_lanes.action=score，decision 必须为 run_batch" in briefing
    assert "score 对应 keep=true" in briefing
    assert "revise_expression、skip_batch_duplicate、skip_library_near_copy、precheck_blocked 对应 keep=false" in briefing
    assert "若 reason 已判断方向错误、语义不一致或需要修改，必须 revise_expression、keep=false，不得 score" in briefing

    lanes = svc._ORCHESTRATOR_STAGE_SCHEMAS["candidate_plan"]["candidate_lanes"]
    assert lanes[0]["action"] == "score"
    assert lanes[0]["keep"] is True
    assert lanes[1]["action"] == "revise_expression"
    assert lanes[1]["keep"] is False


def test_review_briefings_keep_invalid_expression_out_of_grade_distribution():
    score = svc._ORCHESTRATOR_STAGE_BRIEFINGS["score_review"]
    synthesis = svc._ORCHESTRATOR_STAGE_BRIEFINGS["round_synthesis"]

    assert "invalid_expression 是构造错误，不是D级" in score
    assert "invalid_expression不是D级" in synthesis


def test_score_review_routes_negative_signed_rankic_to_sign_only_expression_revision():
    score = svc._ORCHESTRATOR_STAGE_BRIEFINGS["score_review"]
    expression = svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]

    assert "正式 signed RankIC 为负" in score
    assert "global_sign_flip_only" in score
    assert "不能同时改字段、算子、窗口或结构" in score
    assert "只给整个 parent 增加一次整体负号" in expression
    assert "direction_normalization_global_sign_flip_only" in expression


def test_review_briefings_are_diagnostic_only_without_operator_contract():
    for stage in ("score_review", "novelty_review", "deep_validation_review"):
        briefing = svc._ORCHESTRATOR_STAGE_BRIEFINGS[stage]
        assert "tool_evidence.operator_contract" not in briefing
        assert (
            "不构造新表达式" in briefing
            or "不输出完整公式" in briefing
            or "返回expression_design" in briefing
        )


def test_candidate_plan_briefing_hard_blocks_exact_prior_round_expression():
    briefing = svc._ORCHESTRATOR_STAGE_BRIEFINGS["candidate_plan"]

    assert "exact_prior_round_expression" in briefing
    assert "必须 precheck_blocked，模型不能恢复" in briefing


def test_prompt_contract_uses_single_handoff_and_expression_history_owners():
    system = svc._ORCHESTRATOR_RESEARCH_SYSTEM
    briefings = "\n".join(svc._ORCHESTRATOR_STAGE_BRIEFINGS.values())

    assert "current_round_context.handoff" not in briefings
    assert "tool_evidence.seen_expression_signatures" not in briefings
    assert "tool_evidence.prior_expression_history" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]
    assert "tool_evidence.protected_parent_mutation_candidate_ids" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["candidate_plan"]
    assert "分散样本" not in system


def test_factor_map_is_advisory_and_absent_from_expression_generation():
    system = svc._ORCHESTRATOR_RESEARCH_SYSTEM
    thesis = svc._ORCHESTRATOR_STAGE_BRIEFINGS["thesis_design"]
    hypothesis = svc._ORCHESTRATOR_STAGE_BRIEFINGS["hypothesis_design"]

    assert "factor_map_context" not in system
    assert "不是机会排名或候选级 novelty 结论" in thesis
    assert "active 因子数量只描述覆盖，不代表机会、质量或饱和" in thesis
    assert "共享字段不等于重复" in thesis
    assert "current_run_trajectory 只统计本 run" in thesis
    assert "主字段、角色和关系" in hypothesis
    assert "region_relations" not in system
    assert "crowding_summary" not in system
    for stage in ("thesis_design", "hypothesis_design"):
        briefing = svc._ORCHESTRATOR_STAGE_BRIEFINGS[stage]
        assert "factor_map_context" in briefing
    assert "factor_map_context" not in svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]
    assert svc._ORCHESTRATOR_STAGE_CONTEXT_POLICY["expression_design"]["factor_map"] is False
    assert "factor_map_context" not in svc._ORCHESTRATOR_STAGE_BRIEFINGS["candidate_plan"]
    assert "明确guidance" in svc._ORCHESTRATOR_STAGE_BRIEFINGS["round_synthesis"]


def test_factor_map_prompt_forbids_density_as_opportunity_and_window_only_delta():
    thesis = svc._ORCHESTRATOR_STAGE_BRIEFINGS["thesis_design"]
    hypothesis = svc._ORCHESTRATOR_STAGE_BRIEFINGS["hypothesis_design"]

    assert "不得用“数量少”作为研究价值" in thesis
    assert "不代表机会、质量或饱和" in thesis
    assert "也不从区域数量推断研究机会" in hypothesis
    assert "窗口、多期累积、绝对/相对措辞" in hypothesis
    assert "不构成新的 hypothesis" in hypothesis


def test_targeted_parent_mutation_is_an_explicit_handoff_exception():
    system = svc._ORCHESTRATOR_RESEARCH_SYSTEM
    expression = svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]

    assert "targeted_parent_mutation" not in system
    assert "targeted_parent_mutation" in expression
    assert "must_preserve、must_change、must_avoid" in expression
    assert "parent_candidate_id" in expression
    assert "定向时间尺度实验只有在 handoff 明确要求时才允许" in expression
    assert "每个候选相对同一 parent 只能改变一个字段的一个窗口" in expression
    assert "不得在同一候选中同时改变两个窗口" in expression
    assert "只修改一个指定角色" in expression
    assert "若 must_change 用“或/or”列出多个可选方案，只选择其中一个" in expression


def test_novelty_requires_one_atomic_orthogonalization_and_synthesis_keeps_llm_authority():
    novelty = svc._ORCHESTRATOR_STAGE_BRIEFINGS["novelty_review"]
    synthesis = svc._ORCHESTRATOR_STAGE_BRIEFINGS["round_synthesis"]

    assert "只选一类原子实验" in novelty
    assert "不得把同一字段同时写进 preserve 和 change" in novelty
    assert "code_advice可以接受、细化或不采用" in synthesis
    assert "不采用时在why中说明原因，不增加额外字段" in synthesis
    assert "通常从expression_design开始" in synthesis
    assert "通常从hypothesis_design开始" in synthesis
    assert "通常从thesis_design开始" in synthesis
    assert "不能改变其元策略、返回层级或parent" not in synthesis
    assert "不属于任何当前thesis" in synthesis
    assert "不得把parent字段挂到无关的旧thesis_id" in synthesis


def test_expression_prompt_rejects_signed_product_mirror_and_low_amount_direction_error():
    briefing = svc._ORCHESTRATOR_STAGE_BRIEFINGS["expression_design"]

    assert "两个有正有负的中心化腿直接相乘" in briefing
    assert "同时奖励“双正”和“双负”" in briefing
    assert "rank(ts_rank(x,w)) 的高值明确代表 x 处于自身历史高位" in briefing
    assert "若 hypothesis 要奖励历史低位，必须先反向" in briefing


def test_deep_prompt_maps_code_evolution_strategy_to_research_return_level():
    deep = svc._ORCHESTRATOR_STAGE_BRIEFINGS["deep_validation_review"]

    assert "targeted_mutation" in deep
    assert "recombine_from_best" in deep
    assert "explore_new_thesis" in deep
    assert "同时看gap_to_gate、所有组件、复杂度和跨候选轨迹" in deep
    assert "不能只看最低分项" in deep
    assert "change必须只指定一个可检验的机制角色" in deep


def test_round_synthesis_factor_map_only_keeps_actionable_guidance(monkeypatch):
    monkeypatch.setattr(
        svc,
        "factor_map_design_context",
        lambda value, run_id="": {
            "available": True,
            "regions": [
                {
                    "region_id": "R01",
                    "current_run": {"novelty_rejected": 4},
                    "guidance": {"action": "none", "level": "insufficient_evidence"},
                },
                {
                    "region_id": "R02",
                    "current_run": {"deep_rejected": 2},
                    "guidance": {"action": "watch_deep_fragility", "level": "observe"},
                },
            ],
        },
    )

    full = svc._compact_library_information_context({}, run_id="run", affected_only=False)
    affected = svc._compact_library_information_context({}, run_id="run", affected_only=True)

    assert "guidance" not in full["regions"][0]
    assert [item["region_id"] for item in affected["regions"]] == ["R02"]


def test_round_synthesis_omits_factor_map_when_no_guidance(monkeypatch):
    monkeypatch.setattr(
        svc,
        "factor_map_design_context",
        lambda value, run_id="": {
            "available": True,
            "regions": [
                {
                    "region_id": "R01",
                    "current_run": {"novelty_rejected": 4},
                    "guidance": {"action": "none", "level": "insufficient_evidence"},
                },
            ],
        },
    )

    assert svc._compact_library_information_context({}, run_id="run", affected_only=True) == {}


def test_prompt_contract_requires_operator_facing_natural_chinese_summary():
    system = svc._ORCHESTRATOR_RESEARCH_SYSTEM

    assert "summary、judgment、why、history_used、stage_transition.reason" in system
    assert "使用简洁自然中文" in system
    assert "不写 context_pack 的内部路径" in system
    assert "decision、next_action、stage_transition.next_stage 使用契约中的机器枚举" in system
    assert "不能翻译或改写" in system


def test_prompt_quick_contract_is_prompt_first_and_mcp_native():
    prompt = _read(PROMPT)
    section = prompt.split("## Codex MCP Execution Contract", 1)[1].split("## FXAlpha Production Market Data Contract", 1)[0]

    assert "Prompt-first startup order" in section
    assert "read this `PROMPT.md`, call `list_operators`" in section
    assert "call `fxalpha_context`" in section
    assert "`research_steps/current.jsonl`" in section
    assert "`schema_version=research_step_v2`" in section
    assert "mcp_native_tools_missing" in section
    assert "do not substitute shell, curl, HTTP, Python, or runner glue" in section


def test_prompt_quick_contract_has_canonical_loop_and_window_fields():
    prompt = _read(PROMPT)
    section = prompt.split("## Codex MCP Execution Contract", 1)[1].split("## FXAlpha Production Market Data Contract", 1)[0]

    expected_stages = [
        "pre_batch_decision",
        "candidate_plan(code_precheck)",
        "validate_expression",
        "score_factor",
        "score_review",
        "fxalpha_novelty_check",
        "novelty_review",
        "run_backtest/run_anti_overfit/run_rolling_validation/run_adversarial_validation",
        "deep_validation_review",
        "fxalpha_quality_gate",
        "import_gate_review",
        "fxalpha_import_factors",
        "import_review",
        "round_synthesis/checkpoint_stop",
    ]
    for stage in expected_stages:
        assert stage in section
    for field in ("selection_start_date", "selection_end_date", "value_start_date", "value_end_date"):
        assert field in section
    assert "omit dates when runtime defaults are correct" in section
    assert "not a replacement for `fxalpha_novelty_check`" in section
    assert "`round_id={run_id}:rNNNN`" in section
    assert "`stage_id={round_id}:sNN_stage`" in section


def test_mcp_prompt_uses_shared_code_advice_and_current_factor_map_stage_policy():
    prompt = _read(PROMPT)

    assert "fxalpha_code_advice(checkpoint=candidate_plan)" in prompt
    assert "fxalpha_code_advice(checkpoint=score_review)" in prompt
    assert "fxalpha_code_advice(checkpoint=novelty_review" in prompt
    assert "fxalpha_code_advice(checkpoint=deep_validation_review" in prompt
    assert "fxalpha_code_advice(checkpoint=import_gate_review" in prompt
    assert "Do not replay the map during expression" in prompt
    assert "During `round_synthesis`, refer only to" in prompt
    assert "experience-card library is archived" in prompt
    assert "research experience cards" not in prompt
    assert "knowledge index" not in prompt


def test_workspace_quantgpt_mcp_uses_canonical_wrapper():
    config = _read(ROOT / ".codex" / "config.example.toml")
    quantgpt = config.split("[mcp_servers.quantgpt]", 1)[1].split(
        "[mcp_servers.fxalpha-factor0710]",
        1,
    )[0]

    assert 'command = "python3"' in quantgpt
    assert 'args = ["-m", "quantgpt.mcp_server"]' in quantgpt
    assert 'PYTHONPATH = "third_party/quantgpt"' in quantgpt
    assert 'PYTHONUNBUFFERED = "1"' in quantgpt


def test_prompt_and_readme_do_not_carry_literal_trading_dates_or_bad_rolling_wording():
    docs = {"PROMPT.md": _read(PROMPT), "README.md": _read(README)}

    for name, text in docs.items():
        without_placeholder = text.replace("YYYY-MM-DD", "").replace("2026-06-01", "")
        assert not re.search(r"\b20\d{2}-\d{2}-\d{2}\b", without_placeholder), name
        assert "run_rolling_validation" in text
        assert "run_rolling_validation as the full" not in text
        assert "rolling-validation, economic_thesis" not in text


def test_prompt_batch_and_state_recording_contract_are_not_ambiguous():
    prompt = _read(PROMPT)

    assert "Design 6 to 10 thesis-derived candidates by default" not in prompt
    assert "Design three to five thesis-derived candidates by default" in prompt
    assert "adjustment in `research_state`" not in prompt
    assert "adjustment in `fxalpha_record_research_step`" in prompt
    assert "Begin by reading current MCP context, this prompt" not in prompt
    assert "Begin by reading this prompt, then call `list_operators` and `fxalpha_context`" in prompt


def test_prompt_field_reference_points_to_current_schema_and_field_context():
    prompt = _read(PROMPT)
    section = prompt.split("## Expression Syntax Reference", 1)[1]

    assert "schema comes from `list_operators`" in section
    assert "fxalpha_context.field_context" in section
    for field in ("net_mf_amount", "cost_85pct", "free_share", "short_balance"):
        assert field in section


def test_readme_keeps_orch_mcp_and_context_contract_sources_separate():
    readme = _read(README)

    assert "ORCH is the default controller" in readme
    assert "explicit MCP debugging/review mode" in readme
    assert "the mode changes the controller, not the quality standard" in readme
    assert "`fxalpha_context.must_read_contract`" in readme
    assert "candidate_plan` code precheck runs before score spending" in readme
    assert "This is conservative pre-score budget triage only" in readme


def test_docs_define_orchestrator_default_and_keep_mcp_debug_mode():
    prompt = _read(PROMPT)
    readme = _read(README)
    orchestrator_readme = _read(ORCHESTRATOR_README)

    assert "default production `orchestrator`" in orchestrator_readme
    assert "explicit human-supervised debugging" in orchestrator_readme
    assert "Production factor mining defaults to the governed FXAlpha Orchestrator" in readme
    assert "Codex native MCP remains a supported explicit debugging" in readme
    assert "explicit Codex native MCP" in prompt
    assert "production factor mining defaults" in prompt
    assert "not an archived path or a second quality standard" in prompt


def test_docs_and_context_expose_expression_precheck_contract():
    docs = {
        "PROMPT.md": _read(PROMPT),
        "README.md": _read(README),
        "ORCHESTRATOR_README.md": _read(ORCHESTRATOR_README),
    }
    for name, text in docs.items():
        compact = " ".join(text.split())
        assert "code_precheck" in compact, name
        assert "exact active" in compact.lower() or "exact_active_expression" in compact, name
        assert "same-batch" in compact or "same-batch exact duplicates" in compact, name
        assert "not a replacement" in compact or "does not replace" in compact, name
        assert "fxalpha_novelty_check" in compact, name
        assert "precheck_blocked" in compact, name
        assert "planned_for_score" in compact, name

    contract = svc._fxalpha_must_read_contract()
    discipline = " ".join(contract["research_discipline"])
    import_rules = " ".join(contract["import_rules"])
    assert "code_precheck" in discipline
    assert "precheck_blocked" in discipline
    assert "planned_for_score" in discipline
    assert "final novelty remains fxalpha_novelty_check" in discipline
    assert "screening_stage=quick_score" in import_rules
    assert "combined_guard.allowed true" in import_rules


def test_docs_lock_candidate_plan_to_conservative_evidence_based_triage():
    prompt = _read(PROMPT)
    readme = _read(README)
    orchestrator_readme = _read(ORCHESTRATOR_README)

    assert "Uncertain cases" in prompt
    assert "promising prior-round parents" in readme
    assert "Missing evidence, uncertainty" in orchestrator_readme
    for name, text in {
        "PROMPT.md": prompt,
        "README.md": readme,
        "ORCHESTRATOR_README.md": orchestrator_readme,
    }.items():
        assert "candidate_plan_dropped" in text, name

    discipline = " ".join(svc._fxalpha_must_read_contract()["research_discipline"])
    assert "uncertain or promising-parent mutations default to score" in discipline
    assert "only evidenced batch duplicates or library near-copies" in discipline


def test_docs_lock_read_only_status_and_import_sync_contract():
    docs = {
        "PROMPT.md": _read(PROMPT),
        "README.md": _read(README),
        "ORCHESTRATOR_README.md": _read(ORCHESTRATOR_README),
    }

    for name, text in docs.items():
        compact = " ".join(text.split())
        assert "active values stale" in compact, name
        assert "active values fresh" in compact, name
        assert "model snapshot stale" in compact, name
        assert "read-only" in compact.lower(), name
        assert "hidden backfill" in compact, name
        assert "active-values" in compact, name
        assert "model feature" in compact, name
        assert "model_side" in compact, name
        assert "refresh_required" in compact, name


def test_codex_and_orchestrator_docs_share_run_round_stage_naming_contract():
    docs = {
        "PROMPT.md": _read(PROMPT),
        "README.md": _read(README),
        "ORCHESTRATOR_README.md": _read(ORCHESTRATOR_README),
    }

    for name, text in docs.items():
        assert "{run_id}:rNNNN" in text, name
        assert "{round_id}:sNN_stage" in text, name
        assert "candidate_N_<candidate_id>" in text, name
        assert "previous_stage_id" in text, name
        assert "immediately previous visible" in text, name


def test_fxalpha_context_must_read_contract_exposes_shared_naming_contract():
    logging_contract = svc._fxalpha_must_read_contract()["logging_contract"]
    naming = logging_contract["naming_contract"]

    assert naming["shared_by"] == ["codex_mcp", "orchestrator_projection"]
    assert "{run_id}:rNNNN" in naming["round_id"]
    assert "{round_id}:sNN_stage" in naming["stage_id"]
    assert "candidate_N_<candidate_id>" in naming["progress_stage_id"]
    assert "previous visible research-step row" in naming["previous_stage_id"]


def test_fxalpha_field_context_exposes_alias_units_and_missing_semantics():
    field_context = svc._quantgpt_field_context()

    assert field_context["field_aliases"]["dividend_yield"] == "dv_ttm"
    assert "ten-thousand CNY" in field_context["field_descriptions"]["net_mf_amount"]
    assert "net_mf_amount * 10 / amount" in field_context["unit_guidance"]["moneyflow_amount_fields"]
    assert "zero reported dividend yield" in field_context["missing_value_semantics"]["dividend_yield"]


def test_docs_and_context_lock_novelty_st_stock_code_resolution_contract(monkeypatch):
    docs = {
        "PROMPT.md": _read(PROMPT),
        "README.md": _read(README),
        "ORCHESTRATOR_README.md": _read(ORCHESTRATOR_README),
    }
    for name, text in docs.items():
        assert "sh.600000" in text, name
        assert "sz.000004" in text, name
        assert "600000.SH" in text, name
        assert "000004.SZ" in text, name
        assert "stock identity map" in text, name

    import_rules = svc._fxalpha_must_read_contract()["import_rules"]
    st_rule = " ".join(import_rules)
    assert "sh.600000/sz.000004" in st_rule
    assert "600000.SH/000004.SZ" in st_rule
    assert "stock identity map" in st_rule
    assert "distress_proxy_exposure diagnostic by default" in st_rule
    assert "Default st_exposure_guard_mode is advisory" in st_rule
    assert "must not by itself block deep_validation" in st_rule
    assert "Only when st_exposure_guard_mode is hard" in st_rule

    monkeypatch.setattr(svc, "_stage_guard_result", lambda *_, **__: None)
    monkeypatch.setattr(
        "domain.factor_research.dedup.assess_active_pool_novelty",
        lambda *_, **__: {"keepers": [], "dropped": [], "details": [], "feedback": ""},
    )
    payload = svc.factor_tool_novelty_check(candidates=[]).to_dict()
    guard = payload["inputs"]["distress_proxy_guard"]
    assert payload["inputs"]["st_exposure_guard_mode"] == "advisory"
    assert guard["scope"] == "counterfactual_all_market"
    assert guard["label"] == "distress_proxy_exposure"
    assert guard["top_n"] == 50
    assert "sh.600000" in guard["stock_code_formats"]
    assert "000004.SZ" in guard["stock_code_formats"]
