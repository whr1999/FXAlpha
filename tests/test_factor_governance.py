from __future__ import annotations

import json
from pathlib import Path

import yaml

from domain.factor_research import quality_gate
from services import factor_research_service as service


ROOT = Path(__file__).resolve().parents[1]


def _candidate() -> dict:
    return {
        "candidate_id": "candidate-1",
        "expression": "rank(close)",
        "holding_period_days": 5,
        "novelty_guard": {"allowed": True, "novelty_score": 0.4},
        "combined_guard": {"allowed": True},
    }


def test_verified_novelty_evidence_reuses_only_matching_context(monkeypatch):
    candidate = _candidate()
    base = {
        "schema_version": "factor_novelty_evidence_v1",
        "selection_start_date": "2022-01-01",
        "selection_end_date": "2025-06-30",
        "holding_period_days": 5,
        "active_pool_fingerprint": "active-a",
        "extra_existing_fingerprint": "extra-a",
        "candidate_batch_fingerprint": "batch-a",
        "thresholds": {"pearson": 0.75, "rank_corr": 0.8, "p90_pearson": 0.7, "p90_rank_corr": 0.75},
    }
    candidate["novelty_evidence"] = {
        **base,
        "candidate_fingerprint": quality_gate._novelty_candidate_fingerprint(candidate),
    }
    monkeypatch.setattr(quality_gate, "_novelty_evidence_base", lambda **_: dict(base))

    reusable, reason = quality_gate._can_reuse_novelty_evidence(
        [candidate],
        start_date="2022-01-01",
        end_date="2025-06-30",
        extra_existing_candidates=[],
        requested=True,
    )

    assert reusable is True
    assert reason == "same_candidate_window_active_pool_and_thresholds"

    def unexpected_recompute(*_args, **_kwargs):
        raise AssertionError("matching attested novelty evidence must not be recomputed")

    monkeypatch.setattr(quality_gate, "assess_active_pool_novelty", unexpected_recompute)
    monkeypatch.setattr(quality_gate, "_dedup_round", lambda candidates: (candidates, [], ""))
    monkeypatch.setattr(quality_gate, "apply_gate", lambda candidates, *_: ([], candidates))
    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2022-01-01",
        end_date="2025-06-30",
        min_abs_ic=0.02,
        min_ir=0.3,
        trusted_novelty_evidence=True,
    )
    assert report["novelty"]["reused_trusted_evidence"] is True

    changed_pool = {**base, "active_pool_fingerprint": "active-b"}
    monkeypatch.setattr(quality_gate, "_novelty_evidence_base", lambda **_: changed_pool)
    reusable, reason = quality_gate._can_reuse_novelty_evidence(
        [candidate],
        start_date="2022-01-01",
        end_date="2025-06-30",
        extra_existing_candidates=[],
        requested=True,
    )
    assert reusable is False
    assert reason == "novelty_evidence_active_pool_fingerprint_mismatch"


def test_quality_gate_recomputes_when_trusted_evidence_is_not_attested(monkeypatch):
    candidate = _candidate()
    calls = {"novelty": 0}

    def fake_novelty(candidates, **_kwargs):
        calls["novelty"] += 1
        return {"keepers": candidates, "dropped": [], "details": [], "feedback": ""}

    monkeypatch.setattr(quality_gate, "assess_active_pool_novelty", fake_novelty)
    monkeypatch.setattr(quality_gate, "_dedup_round", lambda candidates: (candidates, [], ""))
    monkeypatch.setattr(quality_gate, "apply_gate", lambda candidates, *_: ([], candidates))

    report = quality_gate.evaluate_candidate_quality(
        [candidate],
        start_date="2022-01-01",
        end_date="2025-06-30",
        min_abs_ic=0.02,
        min_ir=0.3,
        trusted_novelty_evidence=True,
    )

    assert calls["novelty"] == 1
    assert report["novelty"]["reused_trusted_evidence"] is False
    assert report["novelty"]["reuse_reason"] == "novelty_evidence_missing"


def test_explicit_journal_compaction_backfills_history_then_bounds_current(monkeypatch, tmp_path):
    root = tmp_path / "factor_research"
    events_dir = root / "orchestrator_events"
    events_file = events_dir / "current.jsonl"
    history_dir = events_dir / "history"
    events_dir.mkdir(parents=True)
    history_dir.mkdir(parents=True)
    rows = [
        {"ts": "2026-07-01T10:00:00", "run_id": "run-old", "stage": "score_review", "seq": 1},
        {"ts": "2026-07-01T10:01:00", "run_id": "run-old", "stage": "novelty_review", "seq": 2},
        {"ts": "2026-07-01T10:02:00", "run_id": "run-old", "stage": "deep_validation_review", "seq": 3},
    ]
    serialized = [json.dumps(row, ensure_ascii=False) for row in rows]
    events_file.write_text("\n".join(serialized) + "\n", encoding="utf-8")
    (history_dir / "2026-07-01.jsonl").write_text(serialized[0] + "\n", encoding="utf-8")

    monkeypatch.setattr(service, "FACTOR_RESEARCH_ROOT", root)
    monkeypatch.setattr(service, "FACTOR_ORCHESTRATOR_EVENTS_FILE", events_file)
    monkeypatch.setattr(service, "FACTOR_ORCHESTRATOR_EVENTS_HISTORY_DIR", history_dir)
    monkeypatch.setattr(service, "FACTOR_ORCHESTRATOR_EVENTS_MAX_LINES", 2)
    monkeypatch.setattr(service, "FACTOR_ORCHESTRATOR_EVENTS_MAX_BYTES", 1024 * 1024)
    monkeypatch.setattr(service, "FACTOR_RESEARCH_STEPS_FILE", root / "research_steps" / "current.jsonl")
    monkeypatch.setattr(service, "FACTOR_RESEARCH_STEPS_HISTORY_DIR", root / "research_steps" / "history")
    monkeypatch.setattr(service, "FACTOR_ORCHESTRATOR_LLM_TRACES_FILE", root / "orchestrator_llm_traces" / "current.jsonl")
    monkeypatch.setattr(service, "FACTOR_ORCHESTRATOR_LLM_TRACES_HISTORY_DIR", root / "orchestrator_llm_traces" / "history")

    result = service.factor_research_compact_journals(dry_run=False)
    report = next(item for item in result.outputs["journals"] if item["journal"] == "orchestrator_events")

    assert report["status"] == "compacted"
    assert report["history_backfill_rows"] == 2
    assert len(events_file.read_text(encoding="utf-8").splitlines()) == 2
    receipt = json.loads(
        (Path(result.outputs["receipt_root"]) / "orchestrator_events" / "receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "compacted"
    assert receipt["current_bytes_after"] == events_file.stat().st_size
    history_rows = (history_dir / "2026-07-01.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(history_rows) == 3
    records, _ = service._read_recent_journal_records(
        current_file=events_file,
        history_dir=history_dir,
        run_id="run-old",
        limit=3,
    )
    assert {item["seq"] for item in records} == {1, 2, 3}


def test_new_run_clears_only_live_journal_caches(monkeypatch, tmp_path):
    root = tmp_path / "factor_research"
    journals = []
    for name in ("research_steps", "orchestrator_events", "orchestrator_llm_traces"):
        current = root / name / "current.jsonl"
        history = root / name / "history" / "2026-07-13.jsonl"
        history.parent.mkdir(parents=True, exist_ok=True)
        row = json.dumps({"run_id": "run-old", "stage": name}, ensure_ascii=False) + "\n"
        current.write_text(row, encoding="utf-8")
        history.write_text(row, encoding="utf-8")
        journals.append((current, history, row))

    monkeypatch.setattr(service, "FACTOR_RESEARCH_STEPS_FILE", journals[0][0])
    monkeypatch.setattr(service, "FACTOR_ORCHESTRATOR_EVENTS_FILE", journals[1][0])
    monkeypatch.setattr(service, "FACTOR_ORCHESTRATOR_LLM_TRACES_FILE", journals[2][0])

    service._begin_factor_research_live_journals("run-new")

    for current, history, row in journals:
        assert current.read_text(encoding="utf-8") == ""
        assert history.read_text(encoding="utf-8") == row


def test_journal_compaction_keeps_only_latest_run_in_current(tmp_path):
    current = tmp_path / "events" / "current.jsonl"
    history = tmp_path / "events" / "history"
    current.parent.mkdir(parents=True)
    history.mkdir(parents=True)
    rows = [
        {"ts": "2026-07-13T10:00:00", "run_id": "run-old", "seq": 1},
        {"ts": "2026-07-13T10:01:00", "run_id": "run-old", "seq": 2},
        {"ts": "2026-07-14T10:00:00", "run_id": "run-new", "seq": 3},
        {"ts": "2026-07-14T10:01:00", "run_id": "run-new", "seq": 4},
    ]
    serialized = [json.dumps(row, ensure_ascii=False) for row in rows]
    current.write_text("\n".join(serialized) + "\n", encoding="utf-8")
    (history / "2026-07-13.jsonl").write_text("\n".join(serialized[:2]) + "\n", encoding="utf-8")
    (history / "2026-07-14.jsonl").write_text("\n".join(serialized[2:]) + "\n", encoding="utf-8")

    report = service._compact_factor_research_journal(
        name="orchestrator_events",
        current_file=current,
        history_dir=history,
        max_lines=100,
        max_bytes=1024 * 1024,
        lock=service.threading.Lock(),
        receipt_root=tmp_path / "receipts",
        dry_run=False,
    )

    current_rows = [json.loads(line) for line in current.read_text(encoding="utf-8").splitlines()]
    assert report["retained_run_id"] == "run-new"
    assert report["dropped_prior_run_rows"] == 2
    assert [row["seq"] for row in current_rows] == [3, 4]
    assert sum(1 for path in history.glob("*.jsonl") for line in path.read_text(encoding="utf-8").splitlines() if line) == 4


def test_factor_governance_has_no_archived_production_import_or_status_snapshot_binding():
    config = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    factor_config = config["factor_research"]
    assert "production_module" not in factor_config
    assert factor_config["default_orchestration_mode"] == "orchestrator"
    assert "latest_status_file" not in factor_config
    assert "from .archived" not in (ROOT / "third_party/quantgpt/quantgpt/rolling_validator.py").read_text(encoding="utf-8")
    assert "factor_tool_record_research_step" in (ROOT / "scripts/factor_automation_gui_log.py").read_text(encoding="utf-8")
    assert "RESEARCH_STEPS_PATH.write_text" not in (ROOT / "scripts/factor_automation_gui_log.py").read_text(encoding="utf-8")
    assert not (ROOT / "runtime/factor_research/latest_status.json").exists()
    assert not (ROOT / "third_party/quantgpt/research_notes/knowledge").exists()
    restart_script = (ROOT / "third_party/quantgpt/restart.sh").read_text(encoding="utf-8")
    assert "research_notes/knowledge" not in restart_script
    assert '"$QUANTGPT_RESEARCH_NOTES_DIR/knowledge' not in restart_script
    assert '"$QUANTGPT_RESEARCH_NOTES_DIR/experience/records"' in restart_script
