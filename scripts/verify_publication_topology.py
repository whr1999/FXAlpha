#!/usr/bin/env python3
"""Verify local submodule pins and, optionally, public release reachability."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "third_party" / "components.lock.json"


def run_git(
    arguments: list[str], *, cwd: Path = ROOT, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def violation(kind: str, target: str, detail: str) -> dict[str, str]:
    return {"kind": kind, "target": target, "detail": detail}


def load_gitmodules() -> dict[str, str]:
    parser = configparser.ConfigParser()
    parser.read(ROOT / ".gitmodules", encoding="utf-8")
    result: dict[str, str] = {}
    for section in parser.sections():
        if not section.startswith("submodule "):
            continue
        result[parser.get(section, "path")] = parser.get(section, "url")
    return result


def local_checks(
    manifest: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    violations: list[dict[str, str]] = []
    blockers: list[dict[str, str]] = []
    modules = load_gitmodules()

    components = manifest.get("components")
    if manifest.get("schema_version") != 1 or not isinstance(components, list):
        return [
            violation("invalid_manifest", str(LOCK_FILE), "unsupported schema")
        ], blockers

    manifest_paths = {item.get("path") for item in components}
    if manifest_paths != set(modules):
        violations.append(
            violation(
                "submodule_manifest_mismatch",
                ".gitmodules",
                f"manifest={sorted(manifest_paths)} gitmodules={sorted(modules)}",
            )
        )

    for component in components:
        name = str(component.get("name", "unknown"))
        path = str(component.get("path", ""))
        pin = str(component.get("pin", ""))
        fork_url = str(component.get("fork_url", ""))
        module_root = ROOT / path

        if modules.get(path) != fork_url:
            violations.append(
                violation(
                    "fork_url_mismatch",
                    path,
                    f".gitmodules={modules.get(path)!r} manifest={fork_url!r}",
                )
            )

        staged = run_git(["ls-files", "--stage", "--", path])
        fields = staged.stdout.strip().split()
        if staged.returncode or len(fields) < 2 or fields[0] != "160000":
            violations.append(violation("not_a_gitlink", path, staged.stderr.strip()))
        elif fields[1] != pin:
            violations.append(
                violation("pin_mismatch", path, f"gitlink={fields[1]} manifest={pin}")
            )

        head = run_git(["rev-parse", "HEAD"], cwd=module_root)
        if head.returncode:
            violations.append(
                violation("submodule_unavailable", path, head.stderr.strip())
            )
            continue
        if head.stdout.strip() != pin:
            violations.append(
                violation(
                    "checkout_mismatch",
                    path,
                    f"checkout={head.stdout.strip()} manifest={pin}",
                )
            )

        status = run_git(["status", "--porcelain"], cwd=module_root)
        if status.returncode or status.stdout.strip():
            detail = status.stdout.strip() or status.stderr.strip()
            violations.append(violation("dirty_submodule", path, detail))

        upstream_base = component.get("upstream_base")
        if component.get("source_kind") == "git":
            if not upstream_base:
                violations.append(violation("missing_upstream_base", path, name))
            else:
                ancestry = run_git(
                    ["merge-base", "--is-ancestor", str(upstream_base), pin],
                    cwd=module_root,
                )
                if ancestry.returncode:
                    violations.append(
                        violation(
                            "upstream_ancestry_failed",
                            path,
                            f"{upstream_base} is not an ancestor of {pin}",
                        )
                    )

        blocker = component.get("publication_blocker")
        if blocker:
            blockers.append(violation(str(blocker), path, name))

    return violations, blockers


def fetch_pin(url: str, pin: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="fxalpha-pin-") as temporary:
        bare = Path(temporary) / "objects.git"
        initialized = run_git(["init", "--bare", str(bare)])
        if initialized.returncode:
            return False, initialized.stderr.strip()
        fetched = run_git(["fetch", "--depth=1", url, pin], cwd=bare, timeout=120)
        return fetched.returncode == 0, fetched.stderr.strip()


def network_checks(
    manifest: dict[str, Any], *, include_main_clone: bool
) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    for component in manifest["components"]:
        ok, detail = fetch_pin(str(component["fork_url"]), str(component["pin"]))
        if not ok:
            violations.append(
                violation("public_pin_unreachable", str(component["path"]), detail)
            )

    if not include_main_clone:
        return violations

    with tempfile.TemporaryDirectory(prefix="fxalpha-clone-") as temporary:
        clone_root = Path(temporary) / "FXAlpha"
        cloned = run_git(
            [
                "clone",
                "--branch",
                str(manifest["publication_branch"]),
                "--recurse-submodules",
                str(manifest["repository_url"]),
                str(clone_root),
            ],
            timeout=300,
        )
        if cloned.returncode:
            violations.append(
                violation(
                    "fresh_recursive_clone_failed",
                    str(manifest["repository_url"]),
                    cloned.stderr.strip(),
                )
            )
        else:
            audited = run_git(["submodule", "status", "--recursive"], cwd=clone_root)
            if audited.returncode or any(
                line.startswith(("-", "+", "U")) for line in audited.stdout.splitlines()
            ):
                violations.append(
                    violation(
                        "fresh_clone_submodule_mismatch",
                        str(manifest["repository_url"]),
                        audited.stdout.strip() or audited.stderr.strip(),
                    )
                )
    return violations


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    network_mode = parser.add_mutually_exclusive_group()
    network_mode.add_argument(
        "--release",
        action="store_true",
        help="also require public pin reachability and a fresh recursive GitHub clone",
    )
    network_mode.add_argument(
        "--components-only",
        action="store_true",
        help="require public component pins without cloning the not-yet-seeded main repo",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    manifest = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    violations, blockers = local_checks(manifest)
    network_violations: list[dict[str, str]] = []
    network_required = arguments.release or arguments.components_only
    if network_required:
        network_violations = network_checks(
            manifest, include_main_clone=arguments.release
        )

    all_violations = [*violations, *network_violations]
    failed = bool(all_violations or (network_required and blockers))
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
        "components_checked": len(manifest.get("components", [])),
        "release_blockers": blockers,
        "violations": all_violations,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
