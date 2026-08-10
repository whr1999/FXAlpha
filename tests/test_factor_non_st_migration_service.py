import sqlite3
import sys
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
QUANTGPT_ROOT = REPO_ROOT / "third_party" / "quantgpt"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(QUANTGPT_ROOT) not in sys.path:
    sys.path.insert(0, str(QUANTGPT_ROOT))


def test_non_st_migration_plan_is_dry_run(monkeypatch, tmp_path):
    from services import factor_non_st_migration_service as svc

    run_root = tmp_path / "migration"
    monkeypatch.setattr(svc, "MIGRATION_ROOT", run_root)
    monkeypatch.setattr(svc, "LATEST_STATUS_FILE", run_root / "latest_status.json")

    row = {
        "factor_id": "f_pass",
        "name": "FactorPass",
        "expression": "close / open",
        "universe": "all_market",
        "metadata": {"data_path": str(tmp_path / "old.parquet"), "data_column": "FactorPass"},
    }

    class FakeRegistry:
        retired = []

        def list_active(self, min_icir=0.0, holding_period_days=None):
            return [row]

        def get(self, factor_id):
            return row

        def retire(self, factor_id, reason):
            self.retired.append((factor_id, reason))

    def fake_save_factor_frame(expression, data_column, factor_values, output_dir):
        path = Path(output_dir) / f"{data_column}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        factor_values.to_parquet(path)
        return str(path)

    monkeypatch.setattr(svc, "FactorRegistry", FakeRegistry)
    monkeypatch.setattr(
        svc,
        "compute_factor",
        lambda expression, start_date, end_date, **kwargs: pd.DataFrame(
            {
                "trade_date": pd.date_range("2026-01-02", periods=3),
                "stock_code": ["sh.600000", "sh.600001", "sh.600002"],
                "value": [1.0, 2.0, 3.0],
            }
        ),
    )
    monkeypatch.setattr(svc, "audit_factor_value_coverage", lambda frame, start, end: {"passed": True})
    monkeypatch.setattr(svc, "save_factor_frame", fake_save_factor_frame)
    monkeypatch.setattr(
        svc,
        "_evaluate_factor_official",
        lambda *args, **kwargs: {
            "status": "evaluated",
            "passed": True,
            "deep_score": 85.0,
            "backtest_summary": {"ic_mean": 0.04, "ic_ir": 0.8},
            "veto_reasons": [],
        },
    )

    result = svc.factor_non_st_migration_plan(run_id="unit_plan", limit=1)

    assert result.ok
    assert result.outputs["summary"]["pass_count"] == 1
    assert result.outputs["summary"]["fail_count"] == 0
    assert (run_root / "unit_plan" / "plan.json").exists()
    assert FakeRegistry.retired == []


def test_non_st_migration_decision_keeps_deep_score_review_without_hard_veto():
    from services import factor_non_st_migration_service as svc

    decision = svc._migration_decision_from_gate(deep_score=76.3, veto_reasons=[])

    assert decision["passed"] is True
    assert decision["quality_gate_passed"] is False
    assert decision["migration_decision"] == "review_keep"
    assert decision["migration_action"] == "keep_active_review"
    assert decision["hard_veto_reasons"] == []
    assert decision["review_reasons"] == ["deep_score_below_admission_threshold:76.3<80"]


def test_non_st_migration_decision_keeps_soft_icir_review():
    from services import factor_non_st_migration_service as svc

    decision = svc._migration_decision_from_gate(deep_score=85.2, veto_reasons=["icir_below_threshold"])

    assert decision["passed"] is True
    assert decision["quality_gate_passed"] is False
    assert decision["migration_decision"] == "review_keep"
    assert decision["migration_action"] == "keep_active_review"
    assert decision["hard_veto_reasons"] == []
    assert decision["review_reasons"] == ["icir_below_threshold"]


def test_non_st_migration_decision_retires_hard_veto():
    from services import factor_non_st_migration_service as svc

    decision = svc._migration_decision_from_gate(deep_score=85.0, veto_reasons=["ic_below_threshold"])

    assert decision["passed"] is False
    assert decision["quality_gate_passed"] is False
    assert decision["migration_decision"] == "hard_retire_candidate"
    assert decision["migration_action"] == "retire_candidate"
    assert decision["hard_veto_reasons"] == ["ic_below_threshold"]


def test_non_st_migration_execute_requires_confirmation(monkeypatch, tmp_path):
    from services import factor_non_st_migration_service as svc

    run_root = tmp_path / "migration"
    monkeypatch.setattr(svc, "MIGRATION_ROOT", run_root)
    monkeypatch.setattr(svc, "LATEST_STATUS_FILE", run_root / "latest_status.json")
    plan_path = run_root / "unit_confirm" / "plan.json"
    svc._write_json(plan_path, {"status": "planned", "run_id": "unit_confirm", "results": []})

    result = svc.factor_non_st_migration_execute(run_id="unit_confirm", confirm="NOPE")

    assert not result.ok
    assert result.err == "confirmation_required"
    assert svc._read_json(plan_path)["status"] == "planned"


def test_non_st_migration_execute_backs_up_replaces_retires_and_refreshes(monkeypatch, tmp_path):
    from services import factor_non_st_migration_service as svc

    run_root = tmp_path / "migration"
    monkeypatch.setattr(svc, "MIGRATION_ROOT", run_root)
    monkeypatch.setattr(svc, "LATEST_STATUS_FILE", run_root / "latest_status.json")
    monkeypatch.setattr(svc, "FACTOR_PARQUET_DIR", tmp_path / "factor_parquet")

    registry_db = tmp_path / "factor_registry.db"
    conn = sqlite3.connect(registry_db)
    conn.execute("CREATE TABLE factors (factor_id TEXT PRIMARY KEY, universe TEXT, last_evaluated TEXT)")
    conn.execute("INSERT INTO factors (factor_id, universe) VALUES ('f_pass', 'all_market')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(svc, "FACTOR_REGISTRY_DB", registry_db)

    old_parquet = tmp_path / "old_factor.parquet"
    old_parquet.write_text("old-values", encoding="utf-8")
    staging = run_root / "unit_execute" / "staging_parquet" / "new_factor.parquet"
    staging.parent.mkdir(parents=True)
    staging.write_text("new-values", encoding="utf-8")

    class FakeRegistry:
        def __init__(self):
            self.retired = []
            self.metrics = {}
            self.metadata = {
                "f_pass": {"data_path": str(old_parquet), "data_column": "FactorPass"},
                "f_fail": {"data_column": "FactorFail"},
            }

        def get(self, factor_id):
            return {"factor_id": factor_id, "metadata": self.metadata.get(factor_id, {})}

        def update_metrics(self, factor_id, metrics):
            self.metrics[factor_id] = metrics

        def update_meta(self, factor_id, metadata):
            self.metadata[factor_id] = metadata

        def retire(self, factor_id, reason):
            self.retired.append((factor_id, reason))

    fake_registry = FakeRegistry()
    monkeypatch.setattr(svc, "FactorRegistry", lambda: fake_registry)
    monkeypatch.setattr(
        svc,
        "enqueue_active_values_refresh",
        lambda **kwargs: {"state": "queued", "kwargs": kwargs},
    )

    plan = {
        "status": "planned",
        "run_id": "unit_execute",
        "inputs": {"holding_period_days": 5, "value_start_date": "2026-01-02", "value_end_date": "2026-01-30"},
        "summary": {"pass_count": 1, "fail_count": 1},
        "results": [
            {
                "factor_id": "f_pass",
                "passed": True,
                "staging_path": str(staging),
                "old_data_path": str(old_parquet),
                "data_column": "FactorPass",
                "metrics": {"ic_mean": 0.04, "icir": 0.8},
                "gate_result": {"passed": True},
            },
            {"factor_id": "f_fail", "passed": False},
        ],
        "artifacts": {"plan_path": str(run_root / "unit_execute" / "plan.json")},
    }
    svc._write_json(run_root / "unit_execute" / "plan.json", plan)

    result = svc.factor_non_st_migration_execute(
        run_id="unit_execute",
        confirm=svc.CONFIRM_TEXT,
        refresh_model=True,
    )

    assert result.ok
    assert old_parquet.read_text(encoding="utf-8") == "new-values"
    assert fake_registry.metadata["f_pass"]["universe"] == "tradable_non_st"
    assert fake_registry.retired == [("f_fail", "retired_non_st_migration_failed_unit_execute")]
    assert result.outputs["execution"]["backup_artifacts"]["registry_db"]
    assert result.outputs["execution"]["backup_artifacts"]["factor_parquets"]["f_pass"]
    assert result.outputs["execution"]["active_values_refresh"]["state"] == "queued"

    conn = sqlite3.connect(registry_db)
    universe = conn.execute("SELECT universe FROM factors WHERE factor_id='f_pass'").fetchone()[0]
    conn.close()
    assert universe == "tradable_non_st"


def test_eval_market_data_records_pit_st_filter_stats(monkeypatch):
    from services import factor_non_st_migration_service as svc

    svc._EVAL_MARKET_CACHE.clear()

    market = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2026-01-02", "2026-01-02", "2026-01-05"]),
            "stock_code": ["sh.600000", "sz.000001", "sz.000001"],
            "security_name": ["浦发银行", "*ST测试", "平安银行"],
            "list_status": ["L", "L", "L"],
            "st_status": ["NORMAL", "ST", "NORMAL"],
            "close": [10.0, 5.0, 5.2],
        }
    )

    monkeypatch.setattr(svc, "_ensure_quantgpt_path", lambda: None)
    monkeypatch.setattr("quantgpt.schemas.fetch_market_data", lambda universe, start, end: (market.copy(), ["sh.600000", "sz.000001"]))

    filtered, stock_codes, stats = svc._fetch_eval_market_data("tradable_non_st", "2026-01-02", "2026-01-05")

    assert len(filtered) == 2
    assert stock_codes == ["sh.600000", "sz.000001"]
    assert stats["mode"] == "pit_row_level"
    assert stats["raw_row_count"] == 3
    assert stats["filtered_row_count"] == 2
    assert stats["st_filtered_rows"] == 1


def test_runtime_defaults_and_gui_expose_non_st_universe_options():
    from services.factor_research_service import factor_research_runtime_defaults

    defaults = factor_research_runtime_defaults()
    values = [item["value"] for item in defaults["universe_options"]]

    assert defaults["production_universe"] == "tradable_non_st"
    assert "tradable_non_st" in values
    assert "all_market" in values

    html = (Path(__file__).resolve().parents[1] / "gui" / "index.html").read_text(encoding="utf-8")
    assert 'option value="tradable_non_st" selected' in html
    assert "诊断/遗留对比" in html
