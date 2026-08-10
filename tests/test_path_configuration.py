from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _probe_paths(config_file: Path, *, cwd: Path) -> dict[str, str]:
    source = """
import json
from storage.paths import (
    ACTIVE_MODEL_FEATURE_SET_FILE,
    CONFIG_FILE,
    CURRENT_PRODUCTION_DATASET_FILE,
    LATEST_MODEL_STATUS_FILE,
    LATEST_STATUS_FILE,
    MODEL_RUNTIME_ROOT,
    QLIB_SOURCE_ROOT,
    RUNTIME_ROOT,
)
print(json.dumps({
    'config_file': str(CONFIG_FILE),
    'runtime_root': str(RUNTIME_ROOT),
    'data_latest': str(LATEST_STATUS_FILE),
    'data_pointer': str(CURRENT_PRODUCTION_DATASET_FILE),
    'model_runtime': str(MODEL_RUNTIME_ROOT),
    'model_latest': str(LATEST_MODEL_STATUS_FILE),
    'active_feature': str(ACTIVE_MODEL_FEATURE_SET_FILE),
    'qlib_source': str(QLIB_SOURCE_ROOT),
}))
"""
    env = {
        **os.environ,
        "FXALPHA_CONFIG_FILE": str(config_file),
        "PYTHONPATH": str(ROOT),
    }
    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_external_runtime_root_is_independent_of_process_cwd(tmp_path: Path) -> None:
    runtime_root = tmp_path / "shadow-state" / "runtime"
    qlib_root = tmp_path / "forks" / "qlib"
    config_file = tmp_path / "production.yaml"
    config_file.write_text(
        "\n".join(
            (
                "paths:",
                f'  runtime_root: "{runtime_root}"',
                f'  qlib_source_root: "{qlib_root}"',
                "data_foundation:",
                '  latest_status_file: "runtime/data_foundation/latest_status.json"',
                '  current_production_dataset_file: "runtime/data_foundation/CURRENT_PRODUCTION_DATASET.json"',
                "model:",
                '  latest_status_file: "runtime/model/latest_status.json"',
                '  active_feature_set_file: "runtime/model/active_feature_set.json"',
                "factor_research:",
                '  default_start_date: "2022-01-01"',
                '  default_end_date: "2026-06-30"',
            )
        ),
        encoding="utf-8",
    )
    unrelated_cwd = tmp_path / "unrelated"
    unrelated_cwd.mkdir()

    paths = _probe_paths(config_file, cwd=unrelated_cwd)

    assert paths["config_file"] == str(config_file)
    assert paths["runtime_root"] == str(runtime_root)
    assert paths["data_latest"] == str(runtime_root / "data_foundation" / "latest_status.json")
    assert paths["data_pointer"] == str(runtime_root / "data_foundation" / "CURRENT_PRODUCTION_DATASET.json")
    assert paths["model_runtime"] == str(runtime_root / "model")
    assert paths["model_latest"] == str(runtime_root / "model" / "latest_status.json")
    assert paths["active_feature"] == str(runtime_root / "model" / "active_feature_set.json")
    assert paths["qlib_source"] == str(qlib_root)
    model_paths_source = (ROOT / "domain" / "model" / "paths.py").read_text(encoding="utf-8")
    assert "QLIB0627_ROOT = QLIB_SOURCE_ROOT" in model_paths_source


def test_relative_runtime_root_is_resolved_against_release_root(tmp_path: Path) -> None:
    config_file = tmp_path / "relative.yaml"
    config_file.write_text(
        "paths:\n"
        "  runtime_root: shadow-runtime\n"
        "factor_research:\n"
        '  default_start_date: "2022-01-01"\n'
        '  default_end_date: "2026-06-30"\n',
        encoding="utf-8",
    )

    paths = _probe_paths(config_file, cwd=tmp_path)

    assert paths["runtime_root"] == str(ROOT / "shadow-runtime")
    assert paths["data_latest"] == str(ROOT / "shadow-runtime" / "data_foundation" / "latest_status.json")
