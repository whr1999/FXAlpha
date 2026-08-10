import sys
import types
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.factor_research import dedup
from services import factor_research_service as svc
import storage.paths as storage_paths


def test_code_precheck_blocks_only_exact_or_obvious_expression_errors():
    active_summary = {
        "active_factors": [
            {"name": "ActiveCloseMean", "expression": "rank(ts_mean(close, 5))"},
            {"name": "ActiveAmountRank", "expression": "rank(ts_mean(amount, 10))"},
        ]
    }
    checks = svc._candidate_plan_code_precheck(
        [
            {"candidate_id": "c1", "expression": " rank( ts_mean( close , 5 ) ) "},
            {"candidate_id": "c2", "expression": "rank(ts_mean(amount, 20))"},
            {"candidate_id": "c3", "expression": "rank(ts_mean(high, 7))"},
            {"candidate_id": "c4", "expression": "rank(ts_mean(high, 7))"},
        ],
        active_factor_summary=active_summary,
    )

    by_id = {item["candidate_id"]: item for item in checks}
    assert by_id["c1"]["fatal"] is True
    assert "exact_active_expression" in by_id["c1"]["warnings"]
    # Clean candidates are intentionally omitted: this compact payload only
    # carries actionable deterministic precheck findings to Candidate Plan.
    assert "c2" not in by_id
    assert by_id["c4"]["fatal"] is True
    assert "batch_duplicate_expression:c3" in by_id["c4"]["warnings"]


def test_active_pool_novelty_keeps_highest_score_batch_representative(monkeypatch, tmp_path):
    class FakeFetcher:
        def _load_cache(self, code):
            return pd.DataFrame(
                {
                    "trade_date": pd.date_range("2024-01-01", periods=20).strftime("%Y-%m-%d"),
                    "stock_code": [code] * 20,
                    "close": list(range(20)),
                }
            )

    stocks = [f"{idx:06d}.SZ" for idx in range(1, 11)]
    factor_index = pd.MultiIndex.from_tuples(
        [(date, stock) for date in ("2024-01-01", "2024-01-02") for stock in stocks],
        names=["trade_date", "stock_code"],
    )
    active_values = list(range(10)) * 2
    candidate_values = [1.0, -1.0] * 10

    def fake_compute(_market_df, expressions):
        labels = [label for _expr, label in expressions]
        data = {}
        for label in labels:
            if label.startswith(("active_", "session_")):
                data[label] = active_values
            else:
                data[label] = candidate_values
        return pd.DataFrame(data, index=factor_index)

    instruments_dir = tmp_path / "instruments"
    instruments_dir.mkdir()
    (instruments_dir / "all.txt").write_text("".join(f"{idx:06d}sz\n" for idx in range(1, 11)))

    monkeypatch.setattr(storage_paths, "QLIB_DATA_ROOT", tmp_path)
    monkeypatch.setattr(storage_paths, "QUANTGPT_CODE_ROOT", tmp_path)
    monkeypatch.setattr(dedup, "_sample_market_data", lambda df, n_stocks, n_dates: df)
    monkeypatch.setattr(dedup, "_compute_factor_values_batch", fake_compute)
    monkeypatch.setattr(
        dedup,
        "evaluate_st_exposure_from_factor_values",
        lambda *_, **__: {"available": True, "passed": True, "reason": "st_exposure_passed"},
    )
    monkeypatch.setitem(
        sys.modules,
        "quantgpt.market_data",
        types.SimpleNamespace(MarketDataFetcher=FakeFetcher),
    )

    low_score = {"candidate_id": "low", "expression": "rank(low)", "quick_score": 61.0, "holding_period_days": 5}
    high_score = {"candidate_id": "high", "expression": "rank(high)", "quick_score": 75.0, "holding_period_days": 5}

    result = dedup.assess_active_pool_novelty(
        [low_score, high_score],
        start_date="2024-01-01",
        end_date="2024-01-05",
        extra_existing_candidates=[{"expression": "rank(close)"}],
    )

    assert [candidate["candidate_id"] for candidate in result["keepers"]] == ["high"], result
    assert [candidate["candidate_id"] for candidate in result["dropped"]] == ["low"]
    assert result["dropped"][0]["novelty_guard"]["matched_existing_factor"] == "session:high"
    assert result["dropped"][0]["novelty_guard"]["matched_reference_source"] == "batch_candidate"


def test_active_pool_novelty_reports_real_registry_identity_without_changing_decision(monkeypatch):
    import storage.factor_registry as registry_module

    class FakeRegistry:
        def list_active(self, min_icir=0.0, holding_period_days=None):
            return [
                {
                    "factor_id": "f_real_001",
                    "name": "RealCloseMean",
                    "expression": "rank(ts_mean(close, 5))",
                    "holding_period_days": 5,
                }
            ]

    monkeypatch.setattr(registry_module, "FactorRegistry", FakeRegistry)
    candidate = {
        "candidate_id": "c1",
        "expression": " rank( ts_mean( close, 5 ) ) ",
        "quick_score": 88.0,
        "holding_period_days": 5,
    }

    result = dedup.assess_active_pool_novelty(
        [candidate],
        start_date="2024-01-01",
        end_date="2024-01-31",
        information_cluster_by_factor_id={"f_real_001": "information_007"},
        information_region_by_factor_id={"f_real_001": "region_007"},
        factor_map_id="fm_test",
        factor_map_audit_id="fa_test",
    )

    assert result["keepers"] == []
    assert [item["candidate_id"] for item in result["dropped"]] == ["c1"]
    guard = result["dropped"][0]["novelty_guard"]
    assert guard["allowed"] is False
    assert guard["reason"] == "low_information_gain"
    assert guard["max_existing_pearson"] == 1.0
    assert guard["max_existing_rank_corr"] == 1.0
    assert guard["matched_existing_factor"] == "f_real_001"
    assert guard["matched_existing_factor_id"] == "f_real_001"
    assert guard["matched_existing_factor_name"] == "RealCloseMean"
    assert guard["matched_existing_expression_summary"] == "rank(ts_mean(close, 5))"
    assert guard["matched_information_cluster_id"] == "information_007"
    assert guard["matched_region_uid"] == "region_007"
    assert guard["factor_map_id"] == "fm_test"
    assert guard["factor_map_audit_id"] == "fa_test"


def test_active_pool_novelty_vetoes_st_exposure_after_novelty_pass(monkeypatch, tmp_path):
    class FakeFetcher:
        def _load_cache(self, code):
            return pd.DataFrame(
                {
                    "trade_date": pd.date_range("2024-01-01", periods=20).strftime("%Y-%m-%d"),
                    "stock_code": [code] * 20,
                    "close": list(range(20)),
                }
            )

    stocks = [f"{idx:06d}.SZ" for idx in range(1, 11)]
    factor_index = pd.MultiIndex.from_tuples(
        [(date, stock) for date in ("2024-01-01", "2024-01-02") for stock in stocks],
        names=["trade_date", "stock_code"],
    )

    def fake_compute(_market_df, expressions):
        labels = [label for _expr, label in expressions]
        data = {}
        for label in labels:
            data[label] = ([1.0, -1.0] * 10) if label.startswith(("active_", "session_")) else (list(range(10)) * 2)
        return pd.DataFrame(data, index=factor_index)

    instruments_dir = tmp_path / "instruments"
    instruments_dir.mkdir()
    (instruments_dir / "all.txt").write_text("".join(f"{idx:06d}sz\n" for idx in range(1, 11)))

    monkeypatch.setattr(storage_paths, "QLIB_DATA_ROOT", tmp_path)
    monkeypatch.setattr(storage_paths, "QUANTGPT_CODE_ROOT", tmp_path)
    monkeypatch.setattr(dedup, "get_live_st_exposure_guard_mode", lambda: "hard")
    monkeypatch.setattr(dedup, "_sample_market_data", lambda df, n_stocks, n_dates: df)
    monkeypatch.setattr(dedup, "_compute_factor_values_batch", fake_compute)
    monkeypatch.setattr(
        dedup,
        "evaluate_st_exposure_from_factor_values",
        lambda *_, **__: {
            "available": True,
            "passed": False,
            "reason": "st_exposure_veto:avg_top50_ratio_ge_0_05",
            "avg_top50_ratio": 0.1,
            "p95_top50_ratio": 0.2,
        },
    )
    monkeypatch.setitem(
        sys.modules,
        "quantgpt.market_data",
        types.SimpleNamespace(MarketDataFetcher=FakeFetcher),
    )

    result = dedup.assess_active_pool_novelty(
        [{"candidate_id": "c1", "expression": "rank(high)", "quick_score": 75.0, "holding_period_days": 5}],
        start_date="2024-01-01",
        end_date="2024-01-05",
        extra_existing_candidates=[{"expression": "rank(close)"}],
    )

    assert result["keepers"] == []
    assert result["dropped"][0]["candidate_id"] == "c1"
    assert result["dropped"][0]["combined_guard"]["allowed"] is False
    assert result["dropped"][0]["st_exposure_guard"]["reason"].startswith("st_exposure_veto")
    assert result["dropped"][0]["st_exposure_guard"]["mode"] == "hard"


def test_active_pool_novelty_keeps_st_exposure_as_advisory_tag(monkeypatch, tmp_path):
    class FakeFetcher:
        def _load_cache(self, code):
            return pd.DataFrame(
                {
                    "trade_date": pd.date_range("2024-01-01", periods=20).strftime("%Y-%m-%d"),
                    "stock_code": [code] * 20,
                    "close": list(range(20)),
                }
            )

    stocks = [f"{idx:06d}.SZ" for idx in range(1, 11)]
    factor_index = pd.MultiIndex.from_tuples(
        [(date, stock) for date in ("2024-01-01", "2024-01-02") for stock in stocks],
        names=["trade_date", "stock_code"],
    )

    def fake_compute(_market_df, expressions):
        labels = [label for _expr, label in expressions]
        data = {}
        for label in labels:
            data[label] = ([1.0, -1.0] * 10) if label.startswith(("active_", "session_")) else (list(range(10)) * 2)
        return pd.DataFrame(data, index=factor_index)

    instruments_dir = tmp_path / "instruments"
    instruments_dir.mkdir()
    (instruments_dir / "all.txt").write_text("".join(f"{idx:06d}sz\n" for idx in range(1, 11)))

    monkeypatch.setattr(storage_paths, "QLIB_DATA_ROOT", tmp_path)
    monkeypatch.setattr(storage_paths, "QUANTGPT_CODE_ROOT", tmp_path)
    monkeypatch.setattr(dedup, "get_live_st_exposure_guard_mode", lambda: "advisory")
    monkeypatch.setattr(dedup, "_sample_market_data", lambda df, n_stocks, n_dates: df)
    monkeypatch.setattr(dedup, "_compute_factor_values_batch", fake_compute)
    monkeypatch.setattr(
        dedup,
        "evaluate_st_exposure_from_factor_values",
        lambda *_, **__: {
            "available": True,
            "passed": False,
            "reason": "st_exposure_veto:avg_top50_ratio_ge_0_05",
            "avg_top50_ratio": 0.1,
            "p95_top50_ratio": 0.2,
        },
    )
    monkeypatch.setitem(
        sys.modules,
        "quantgpt.market_data",
        types.SimpleNamespace(MarketDataFetcher=FakeFetcher),
    )

    result = dedup.assess_active_pool_novelty(
        [{"candidate_id": "c1", "expression": "rank(high)", "quick_score": 75.0, "holding_period_days": 5}],
        start_date="2024-01-01",
        end_date="2024-01-05",
        extra_existing_candidates=[{"expression": "rank(close)"}],
    )

    assert result["dropped"] == []
    assert result["keepers"][0]["candidate_id"] == "c1"
    assert result["keepers"][0]["st_exposure_guard"]["mode"] == "advisory"
    assert result["keepers"][0]["st_exposure_guard"]["advisory_flag"] == "distress_proxy_exposure"
    assert result["keepers"][0]["combined_guard"]["allowed"] is True
    assert result["keepers"][0]["risk_tags"] == ["distress_proxy_exposure"]


def test_active_pool_novelty_real_st_gate_resolves_qlib_stock_codes(monkeypatch, tmp_path):
    class FakeFetcher:
        def _load_cache(self, code):
            return pd.DataFrame(
                {
                    "trade_date": pd.date_range("2024-01-01", periods=20).strftime("%Y-%m-%d"),
                    "stock_code": [code] * 20,
                    "close": list(range(20)),
                }
            )

    stocks = [f"sz.{idx:06d}" for idx in range(1, 51)]
    factor_index = pd.MultiIndex.from_tuples(
        [(date, stock) for date in ("2024-01-01", "2024-01-02") for stock in stocks],
        names=["trade_date", "stock_code"],
    )

    def fake_compute(_market_df, expressions):
        labels = [label for _expr, label in expressions]
        data = {}
        for label in labels:
            if label.startswith(("active_", "session_")):
                data[label] = ([1.0, -1.0] * 25) * 2
            else:
                data[label] = list(range(1, 51)) * 2
        return pd.DataFrame(data, index=factor_index)

    instruments_dir = tmp_path / "instruments"
    instruments_dir.mkdir()
    (instruments_dir / "all.txt").write_text("".join(f"{idx:06d}sz\n" for idx in range(1, 51)))

    monkeypatch.setattr(storage_paths, "QLIB_DATA_ROOT", tmp_path)
    monkeypatch.setattr(storage_paths, "QUANTGPT_CODE_ROOT", tmp_path)
    monkeypatch.setattr(dedup, "get_live_st_exposure_guard_mode", lambda: "hard")
    monkeypatch.setattr(dedup, "_sample_market_data", lambda df, n_stocks, n_dates: df)
    monkeypatch.setattr(dedup, "_compute_factor_values_batch", fake_compute)
    monkeypatch.setattr(
        "domain.factor_research.st_exposure_guard.load_stock_identity_map",
        lambda: {f"{idx:06d}.SZ": "*ST_HIGH" for idx in range(46, 51)},
    )
    monkeypatch.setitem(
        sys.modules,
        "quantgpt.market_data",
        types.SimpleNamespace(MarketDataFetcher=FakeFetcher),
    )

    result = dedup.assess_active_pool_novelty(
        [{"candidate_id": "c1", "expression": "rank(high)", "quick_score": 75.0, "holding_period_days": 5}],
        start_date="2024-01-01",
        end_date="2024-01-05",
        extra_existing_candidates=[{"expression": "rank(close)"}],
    )

    assert result["keepers"] == []
    assert result["dropped"][0]["candidate_id"] == "c1"
    assert result["dropped"][0]["st_exposure_guard"]["avg_top50_ratio"] == 0.1
    assert result["dropped"][0]["st_exposure_guard"]["top_st_hits"][0]["stock_code"] == "sz.000046"
