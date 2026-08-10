from __future__ import annotations

import importlib

from api_server import MODEL_GET_ALIASES, MODEL_POST_ALIASES, MODEL_PRODUCTION_MODULE
from domain.model.contracts import MODEL_SYSTEM_VERSION, is_model_system_version, round_group_id_from
from domain.model.paths import MODEL_RUNTIME_ROOT
from domain.model.qlib_runner import _model_run_id


def test_canonical_model_name_and_new_identifiers():
    assert MODEL_PRODUCTION_MODULE == "model"
    assert MODEL_SYSTEM_VERSION == "model"
    assert MODEL_RUNTIME_ROOT.name == "model"

    round_id = round_group_id_from("fs-model-smoke", "signature")
    run_id = _model_run_id(round_id, 42)
    assert round_id.startswith("mround_")
    assert run_id.startswith("mrun_mround_")
    assert "0703" not in round_id
    assert "0703" not in run_id


def test_legacy_names_are_read_only_compatibility_aliases():
    assert is_model_system_version("model") is True
    assert is_model_system_version("model0703") is True
    assert MODEL_GET_ALIASES["/model0703/status"] == "/model/status"
    assert MODEL_POST_ALIASES["/model0703/orchestrator/start"] == "/model/orchestrator/start"
    assert importlib.import_module("domain.model0703.qlib_strategy") is importlib.import_module("domain.model.qlib_strategy")
    weighted = importlib.import_module("domain.model.weighted_lgbm")
    canonical = importlib.import_module("domain.model.reweight")
    assert weighted.FXAlphaWeightedLGBModel is canonical.FXAlphaWeightedLGBModel


def test_gui_uses_only_the_canonical_model_api_prefix():
    source = (MODEL_RUNTIME_ROOT.parents[1] / "gui" / "app.js").read_text(encoding="utf-8")
    assert 'const MODEL_API_PREFIX = "/model";' in source
    assert 'const MODEL_API_PREFIX = "/model0703";' not in source
