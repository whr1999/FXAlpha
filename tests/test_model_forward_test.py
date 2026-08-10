from domain.model.contracts import forward_test_contract
from services.model_service import model_tool_forward_test


def test_forward_test_is_explicitly_removed_from_active_model_research():
    contract = forward_test_contract()
    assert contract["enabled"] is False
    assert contract["deprecated"] is True
    result = model_tool_forward_test("round")
    assert result.ok is False
    assert result.err == "forward_test_removed_use_research_confirmation_or_production_rolling"
