from __future__ import annotations

import json
from pathlib import Path

import pytest

from domain.platform_evaluation import (
    EvaluationProfileError,
    evaluation_profile_status,
    resolve_evaluation_profile,
    set_default_evaluation_mode,
)


def _write_config(path: Path) -> None:
    path.write_text(
        """
platform_evaluation:
  schema_version: evaluation_contract_v1
  default_mode: research
  profiles:
    research:
      profile_version: research_test_v1
      label: 研究模式
      evidence_class: clean_holdout
      factor:
        selection_start_date: '2022-01-01'
        selection_end_date: '2024-12-31'
        value_start_date: '2022-01-01'
        value_end_date: '2026-06-30'
      model:
        implementation_status: existing_unchanged
    production:
      profile_version: production_test_v1
      label: 生产模式
      evidence_class: discovery_conditioned_rolling
      factor:
        selection_start_date: '2022-01-01'
        selection_end_date: '2026-06-30'
        value_start_date: '2022-01-01'
        value_end_date: '2026-06-30'
      model:
        implementation_status: planned_not_enabled
""".lstrip(),
        encoding="utf-8",
    )


def test_resolver_uses_config_default_without_mutating_state(tmp_path):
    config_file = tmp_path / "config.yaml"
    state_file = tmp_path / "runtime" / "evaluation_mode.json"
    _write_config(config_file)

    profile = resolve_evaluation_profile(config_file=config_file, state_file=state_file)

    assert profile["evaluation_mode"] == "research"
    assert profile["evidence_class"] == "clean_holdout"
    assert profile["factor"]["selection_end_date"] == "2024-12-31"
    assert len(profile["config_snapshot_hash"]) == 64
    assert not state_file.exists()


def test_mode_switch_is_atomic_default_for_new_tasks_only(tmp_path):
    config_file = tmp_path / "config.yaml"
    state_file = tmp_path / "runtime" / "evaluation_mode.json"
    _write_config(config_file)

    status = set_default_evaluation_mode(
        "production",
        changed_by="test",
        config_file=config_file,
        state_file=state_file,
    )

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["evaluation_mode"] == "production"
    assert persisted["switch_scope"] == "new_tasks_only"
    assert status["active_default_mode"] == "production"
    assert status["running_tasks_immutable"] is True
    assert status["model_profile_consumption"] == "not_enabled_stage1"
    assert evaluation_profile_status(config_file=config_file, state_file=state_file)["active_profile"]["factor"]["selection_end_date"] == "2026-06-30"


def test_invalid_mode_does_not_create_state(tmp_path):
    config_file = tmp_path / "config.yaml"
    state_file = tmp_path / "runtime" / "evaluation_mode.json"
    _write_config(config_file)

    with pytest.raises(EvaluationProfileError):
        set_default_evaluation_mode("unknown", config_file=config_file, state_file=state_file)

    assert not state_file.exists()
