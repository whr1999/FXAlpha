from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_experience_runtime_is_retired_and_private_history_is_not_published():
    assert not (ROOT / "domain/factor_research/experience_store.py").exists()
    assert not (ROOT / "scripts/factor_research/experience_audit_worker.py").exists()

    service = (ROOT / "services/factor_research_service.py").read_text(encoding="utf-8")
    api = (ROOT / "api_server.py").read_text(encoding="utf-8")
    mcp = (ROOT / "third_party/quantgpt/quantgpt/mcp_server.py").read_text(encoding="utf-8")
    gui = (ROOT / "gui/app.js").read_text(encoding="utf-8")

    for retired_symbol in (
        "factor_research_distill_experience",
        "factor_research_experience_audit",
        "factor_research_experience_library",
        "_auto_distill_research_experience_after_round",
        "write_knowledge",
    ):
        assert retired_symbol not in service
        assert retired_symbol not in api

    assert "fxalpha_distill_research_experience" not in mcp
    assert "fxalpha_experience_audit" not in mcp
    assert "/factor/research/experience" not in api
    assert "experienceLibrary" not in gui
    assert "data-experience-" not in gui

    assert not (ROOT / "third_party/quantgpt/research_notes").exists()
    assert not (ROOT / "runtime/factor_map/legacy_experience_migration_v1.json").exists()
