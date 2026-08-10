from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_public_repository_audit_passes():
    completed = subprocess.run(
        ["python", str(ROOT / "scripts" / "audit_public_repo.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_vnpy_runtime_packages_are_absent():
    for package in ("vnpy", "vnpy_paperaccount", "vnpy_portfoliostrategy"):
        assert importlib.util.find_spec(package) is None, package


def test_config_example_and_external_override_contract():
    paths_source = (ROOT / "storage" / "paths.py").read_text(encoding="utf-8")
    assert (ROOT / "config.example.yaml").is_file()
    assert "FXALPHA_CONFIG_FILE" in paths_source
    assert "CONFIG_EXAMPLE_FILE" in paths_source
    assert 'PATHS.get("data_root"' in paths_source
    assert 'PATHS.get("runtime_root"' in paths_source
    assert 'PATHS.get("quantgpt_db"' in paths_source
    assert "QLIB_SOURCE_ROOT" in paths_source
    assert not (ROOT / "config.yaml").exists()


def test_immutable_release_unit_set_includes_services_target_and_timers():
    unit_root = ROOT / "deploy" / "systemd" / "release"
    expected = {
        "fxalpha-api-18081.service",
        "fxalpha-quantgpt-8003.service",
        "fxalpha-data-daily.service",
        "fxalpha-paper-fleet-daily.service",
        "fxalpha-factor-stack.target",
        "fxalpha-data-daily.timer",
        "fxalpha-paper-fleet-daily.timer",
    }

    assert expected <= {path.name for path in unit_root.iterdir()}
    for timer in ("fxalpha-data-daily.timer", "fxalpha-paper-fleet-daily.timer"):
        text = (unit_root / timer).read_text(encoding="utf-8")
        assert "Persistent=true" in text
        assert "WantedBy=timers.target" in text


def test_bilingual_readmes_route_new_users_to_operational_guidance():
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    guide_en = (ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    guide_zh = (ROOT / "docs" / "USER_GUIDE.zh-CN.md").read_text(encoding="utf-8")

    for readme in (english, chinese):
        assert "docs/assets/fxalpha-system-architecture.svg" in readme
        assert "docs/assets/screenshots/platform-overview.jpeg" in readme
        assert "BUSINESS_WORKFLOWS" in readme
        assert "SCREENSHOTS" in readme
        assert "data-status" in readme
        assert "paper-fleet-preflight" in readme
        assert "FXALPHA_CONFIG_FILE" in readme
        assert "GITHUB_UPLOAD_RUNBOOK.md" in readme

    assert "docs/VERIFICATION_REPORT_20260810.md" in english
    assert "docs/VERIFICATION_REPORT_20260810.zh-CN.md" in chinese

    assert "docs/USER_GUIDE.md" in english
    assert "docs/USER_GUIDE.zh-CN.md" in chinese
    assert "Standard data-to-paper workflow" in guide_en
    assert "从数据到模拟交易的标准路径" in guide_zh
    assert "accepted" in guide_en and "accepted" in guide_zh
    assert "public repository boundary" in guide_en.lower()
    assert "公开仓库与本地资产边界" in guide_zh

    diagram = (ROOT / "docs" / "assets" / "fxalpha-system-architecture.svg").read_text(
        encoding="utf-8"
    )
    assert "MODULE 1 · DATA FOUNDATION" in diagram
    assert "MODULE 2 · FACTOR RESEARCH" in diagram
    assert "MODULE 4 · MODEL TRAINING" in diagram
    assert "MODULE 6 · PREDICTION" in diagram
    assert "MODULE 8 · QLIB PAPER TRADING" in diagram
    assert "factor registry" in diagram
    assert "production model pointer" in diagram

    gallery_en = (ROOT / "docs" / "SCREENSHOTS.md").read_text(encoding="utf-8")
    gallery_zh = (ROOT / "docs" / "SCREENSHOTS.zh-CN.md").read_text(encoding="utf-8")
    for gallery in (gallery_en, gallery_zh):
        assert "assets/screenshots/platform-overview.jpeg" in gallery
        assert "assets/screenshots/factor-research.jpeg" in gallery
        assert "assets/screenshots/model-research.jpeg" in gallery
        assert "assets/screenshots/paper-trading.jpeg" in gallery
        assert "BUSINESS_WORKFLOWS" in gallery


def test_bilingual_business_workflow_contract_is_detailed_and_discoverable():
    english = (ROOT / "docs" / "BUSINESS_WORKFLOWS.md").read_text(encoding="utf-8")
    chinese = (ROOT / "docs" / "BUSINESS_WORKFLOWS.zh-CN.md").read_text(encoding="utf-8")
    docs_index = (ROOT / "docs" / "DOCUMENTATION_INDEX_CURRENT.md").read_text(encoding="utf-8")

    for document in (english, chinese):
        for stage in (
            "protocol_load",
            "thesis_design",
            "hypothesis_design",
            "expression_design",
            "candidate_plan",
            "score_review",
            "novelty_review",
            "deep_validation_review",
            "import_gate_review",
            "import_review",
            "round_synthesis",
        ):
            assert stage in document
        assert "Quick = 0.20 * IC_mean_score" in document
        assert "Deep = 0.55 * Quick" in document
        assert "rolling_score = clip(robust_ic / 0.08 * 100, 0, 100)" in document
        assert "CampaignRolling" in document
        assert "final_stock_cap = min(model_stock_cap, market_cap, account_cap)" in document
        assert "post_promote_audit.status=passed" in document

    assert "标准成功路径共有 11 个阶段" in chinese
    assert "The successful path has 11 stages" in english
    assert "FXAlpha 当前不使用 vn.py" in chinese
    assert "FXAlpha no longer uses vn.py" in english
    assert "BUSINESS_WORKFLOWS.md" in docs_index
    assert "BUSINESS_WORKFLOWS.zh-CN.md" in docs_index


def test_third_party_sources_are_gitlinks():
    modes = subprocess.check_output(
        ["git", "ls-files", "--stage", "third_party"], cwd=ROOT, text=True
    )
    rows = [line for line in modes.splitlines() if line]
    gitlinks = [line for line in rows if line.startswith("160000 ")]
    regular_files = [
        line.split("\t", 1)[1] for line in rows if not line.startswith("160000 ")
    ]

    assert len(gitlinks) == 3
    assert regular_files == ["third_party/components.lock.json"]


def test_public_security_defaults_and_dependency_update_boundary(monkeypatch):
    import cli

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "cmd_serve_api",
        lambda args: captured.update(host=args.host, port=args.port),
    )
    monkeypatch.setattr(sys, "argv", ["cli.py", "serve-api"])
    cli.main()

    assert captured == {"host": "127.0.0.1", "port": 8080}

    dependabot = yaml.safe_load(
        (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    )
    ecosystems = {entry["package-ecosystem"] for entry in dependabot["updates"]}
    assert ecosystems == {"pip", "github-actions"}

    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "`file://`" in security
    assert "does not launch an MLflow HTTP tracking server" in security
