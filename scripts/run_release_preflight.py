#!/usr/bin/env python3
"""Run the local or network-backed FXAlpha publication gates."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="skip the full pytest suite",
    )
    network_mode = parser.add_mutually_exclusive_group()
    network_mode.add_argument(
        "--release",
        action="store_true",
        help="require public fork pins and a network-fresh recursive clone",
    )
    network_mode.add_argument(
        "--components-only",
        action="store_true",
        help="require public component pins before the main repository is seeded",
    )
    return parser.parse_args()


def run_step(name: str, command: list[str]) -> dict[str, object]:
    command_env = os.environ.copy()
    # Codex desktop and some WSL shells inherit Windows TEMP/TMP paths. Python
    # can use them, but pytest/SQLite-heavy tests become extremely slow on the
    # mounted filesystem and fd capture may fail after unlink. Prefer the native
    # Linux temporary filesystem when it is available.
    if sys.platform != "win32" and Path("/tmp").is_dir():
        command_env["TMPDIR"] = "/tmp"
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=command_env,
        text=True,
        capture_output=True,
        check=False,
    )
    result: dict[str, object] = {
        "name": name,
        "status": "passed" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
    }
    if name == "tests" and completed.stdout.strip():
        result["summary"] = completed.stdout.strip().splitlines()[-1]
    if completed.returncode:
        result["stdout_tail"] = completed.stdout[-4000:]
        result["stderr_tail"] = completed.stderr[-4000:]
    return result


def main() -> int:
    arguments = parse_args()
    python = sys.executable
    topology = [python, "scripts/verify_publication_topology.py"]
    if arguments.release:
        topology.append("--release")
    elif arguments.components_only:
        topology.append("--components-only")
    commands = [
        ("public_tree_audit", [python, "scripts/audit_public_repo.py"]),
        ("git_history_audit", [python, "scripts/audit_git_history.py"]),
        ("publication_topology", topology),
        (
            "compile",
            [
                python,
                "-m",
                "compileall",
                "-q",
                "api_server.py",
                "cli.py",
                "domain",
                "integrations",
                "lib",
                "mcp_servers",
                "services",
                "storage",
            ],
        ),
    ]
    if not arguments.quick:
        # ``fd`` capture can fail before collection in WSL/sandboxed desktop
        # sessions when a temporary backing file disappears. The preflight
        # already captures the pytest subprocess output, so Python-level
        # capture keeps diagnostics while avoiding that environment-specific
        # false failure.
        commands.append(
            ("tests", [python, "-m", "pytest", "-q", "--capture=sys"])
        )

    steps = [run_step(name, command) for name, command in commands]
    failed = [step for step in steps if step["status"] != "passed"]
    mode = (
        "release"
        if arguments.release
        else "components"
        if arguments.components_only
        else "local"
    )
    result = {
        "status": "failed" if failed else "passed",
        "mode": mode,
        "tests": "skipped" if arguments.quick else "included",
        "steps": steps,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
