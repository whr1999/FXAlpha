from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_local_publication_topology_is_consistent() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify_publication_topology.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["components_checked"] == 3
    assert payload["violations"] == []
    assert payload["release_blockers"] == []


def test_component_lock_records_tushare_wheel_provenance() -> None:
    manifest = json.loads(
        (ROOT / "third_party" / "components.lock.json").read_text(encoding="utf-8")
    )
    tushare = next(item for item in manifest["components"] if item["name"] == "Tushare")

    assert tushare["source_kind"] == "pypi_wheel_overlay"
    assert tushare["release_artifact"] == {
        "filename": "tushare-1.4.29-py3-none-any.whl",
        "sha256": "82554af953ea5ac3d8771d42330493181031c7e68dccce03a491c7356e9ba4b2",
        "source_files": 74,
    }
    assert tushare["publication_blocker"] is None
