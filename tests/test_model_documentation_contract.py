from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def test_current_model_docs_cover_managed_orchestrator_controls() -> None:
    runbook = _read("docs/MODEL_RESEARCH_PRODUCTION_RUNBOOK.md")
    workflow = _read("docs/MODEL_RESEARCH_WORKFLOW_CURRENT.md")

    for endpoint in (
        "/model/orchestrator/start",
        "/model/jobs/stop",
        "/model/jobs/resume",
    ):
        assert endpoint in runbook
        assert endpoint in workflow
    assert "status=accepted" in runbook
    assert "status=already_running" in runbook
    assert "runtime/model/jobs/<job_id>.log" in runbook


def test_current_model_docs_cover_snapshot_and_production_pointer_contract() -> None:
    readme = _read("domain/model/README.md")
    workflow = _read("docs/MODEL_RESEARCH_WORKFLOW_CURRENT.md")
    checklist = _read("docs/MODEL_RESEARCH_PRETEST_CHECKLIST_CURRENT.md")

    for content in (readme, workflow, checklist):
        assert "active_production_model.json" in content
    assert "相同内容重复冻结应复用原快照" in checklist
    assert "all-active 33" not in checklist
    assert "为 47" not in checklist
    assert "不得在代码或手册中硬编码数量" in checklist


def test_gui_readme_uses_managed_desktop_endpoint() -> None:
    gui_readme = _read("gui/README.md")

    assert "fxalpha-api-18081.service" in gui_readme
    assert "http://127.0.0.1:18081/gui/" in gui_readme
    assert "serve-api --host 0.0.0.0 --port 8080" not in gui_readme


def test_platform_mode_doc_does_not_claim_model_rolling_is_unimplemented() -> None:
    modes = _read("docs/PLATFORM_EVALUATION_MODES.md")

    assert "模型训练尚未消费生产 profile" not in modes
    assert "模型生产指针已经实现" in modes
    assert "普通研究回测包装成生产 Rolling" in modes


def test_current_runtime_docs_list_model_worker_and_pointer_assets() -> None:
    paths = _read("docs/RUNTIME_AND_DATA_PATHS_CURRENT.md")
    audit = _read("docs/RUNTIME_AUDIT_CURRENT.md")

    assert "worker logs" in paths
    assert "active production model pointer" in paths
    assert "jobs/<job_id>.log" in audit
    assert "rolling/<campaign_id>/campaign.json" in audit
    assert "active_production_model.json" in audit
