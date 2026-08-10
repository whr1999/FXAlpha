from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_preflight_uses_portable_pytest_capture() -> None:
    source = (ROOT / "scripts" / "run_release_preflight.py").read_text(
        encoding="utf-8"
    )

    assert '"--capture=sys"' in source
    assert 'command_env["TMPDIR"] = "/tmp"' in source


def test_reachable_git_history_passes_public_audit() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/audit_git_history.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "passed"
    assert result["commits_checked"] >= 1
    assert result["blobs_checked"] >= 1
    assert result["largest_blob"]["bytes"] < result["max_blob_bytes"]
    assert result["violations"] == []


def test_quick_release_preflight_passes_locally() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/run_release_preflight.py", "--quick"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "passed"
    assert result["mode"] == "local"
    assert result["tests"] == "skipped"
    assert [step["name"] for step in result["steps"]] == [
        "public_tree_audit",
        "git_history_audit",
        "publication_topology",
        "compile",
    ]
    assert all(step["status"] == "passed" for step in result["steps"])
