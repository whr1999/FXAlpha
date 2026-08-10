from services.model_service import model_tool_sota_gate
from storage.model_registry import MODEL_LIBRARY_STATUSES


def test_legacy_sota_gate_is_removed_and_candidate_requires_rolling():
    result = model_tool_sota_gate("round")
    assert result.ok is False
    assert result.err == "sota_gate_removed_candidate_requires_production_rolling"


def test_registry_lifecycle_has_research_before_candidate():
    assert MODEL_LIBRARY_STATUSES == ("research", "candidate", "production", "archived")
