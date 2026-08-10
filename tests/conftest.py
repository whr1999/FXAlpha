from __future__ import annotations

import pytest

from domain.data_foundation import ops_common


@pytest.fixture(autouse=True)
def _isolate_data_foundation_job_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(ops_common, "DATA_JOB_LOCK_DIR", tmp_path / "data_job.lock")
