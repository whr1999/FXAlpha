#!/usr/bin/env python3
"""Fail closed on common public-repository hygiene regressions."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".cfg", ".css", ".html", ".ini", ".js", ".json", ".md", ".py",
    ".sh", ".toml", ".txt", ".yaml", ".yml",
}
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "openai_style_key": re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
}
PRIVATE_RUNTIME_ID_PATTERNS = {
    "factor_registry_id": re.compile(r"\bf_[0-9]{8}_[0-9]{6}_[0-9]+\b"),
    "factor_research_run_id": re.compile(r"\bfr_[0-9]{8}_[0-9]{6}_[0-9a-f]+\b"),
    "model_registry_id": re.compile(r"\bm_[0-9]{8}_[0-9]{6}_[0-9]+_[0-9a-f]+\b"),
    "production_model_run_id": re.compile(r"\bmodel_prod_model_roll_[A-Za-z0-9_.:-]{20,}\b"),
    "legacy_feature_snapshot_id": re.compile(
        r"\bfs-model0703-(?:active|ab|diagnostic|family|temporal)[A-Za-z0-9_.:-]*\b"
    ),
}
FORBIDDEN_PERSONAL_PATHS = (
    "/home/roy/",
    r"C:\\Users\\whr_9",
    "C:/Users/whr_9",
    "/mnt/c/Users/whr_9/",
)
PERSONAL_PATH_EXEMPTIONS = {
    "scripts/audit_git_history.py",
    "scripts/audit_public_repo.py",
}
RETIRED_IMPORT = re.compile(
    r"(?:^|\s)(?:from|import)\s+(?:vnpy|vnpy_paperaccount|vnpy_portfoliostrategy)\b",
    re.MULTILINE,
)
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
ACTION_REFERENCE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
FULL_COMMIT_REFERENCE = re.compile(r"^[^@]+@[0-9a-f]{40}$")
REQUIRED_PUBLIC_FILES = {
    ".github/CODEOWNERS",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/dependabot.yml",
    ".github/pull_request_template.md",
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "README.zh-CN.md",
    "SECURITY.md",
    "SUPPORT.md",
    "docs/SCREENSHOTS.md",
    "docs/SCREENSHOTS.zh-CN.md",
    "docs/VERIFICATION_REPORT_20260810.md",
    "docs/VERIFICATION_REPORT_20260810.zh-CN.md",
    "docs/assets/screenshots/manifest.json",
    "third_party/components.lock.json",
}
SCREENSHOT_ROOT = "docs/assets/screenshots/"
SCREENSHOT_MANIFEST = f"{SCREENSHOT_ROOT}manifest.json"


def candidate_paths() -> list[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    )
    return sorted({item.decode() for item in output.split(b"\0") if item})


def audit() -> dict:
    violations: list[dict[str, str]] = []
    paths = candidate_paths()

    forbidden_roots = (
        "artifacts/", "checkpoints/", "data/", "factor_values/", "logs/",
        "log/", "mlruns/", "models/", "predictions/", "runtime/",
    )
    forbidden_names = {"config.yaml", ".env"}
    forbidden_suffixes = (
        ".7z", ".arrow", ".ckpt", ".csv", ".db", ".feather", ".h5",
        ".hdf5", ".joblib", ".key", ".npy", ".npz", ".onnx", ".p12",
        ".parquet", ".pem", ".pfx", ".pickle", ".pkl", ".pt", ".pth",
        ".safetensors", ".sqlite", ".sqlite3", ".tar", ".tar.gz", ".tgz",
        ".tsv", ".zip",
    )

    for rel in paths:
        if rel.startswith("third_party/"):
            continue
        path = ROOT / rel
        if any(rel.startswith(prefix) for prefix in forbidden_roots):
            violations.append({"kind": "tracked_generated_state", "path": rel})
        filename = Path(rel).name
        private_env = filename.startswith(".env.") and filename != ".env.example"
        if filename in forbidden_names or private_env or rel.endswith(forbidden_suffixes):
            violations.append({"kind": "tracked_private_or_data_file", "path": rel})
        if rel.startswith((".claude/", ".openai/", ".idea/", ".vscode/")) or (
            rel.startswith(".codex/") and rel != ".codex/config.example.toml"
        ):
            violations.append({"kind": "tracked_private_tooling_state", "path": rel})
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 2_000_000:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if rel not in PERSONAL_PATH_EXEMPTIONS:
            for marker in FORBIDDEN_PERSONAL_PATHS:
                if marker in text:
                    violations.append({"kind": "personal_absolute_path", "path": rel, "detail": marker})
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                violations.append({"kind": f"secret_pattern:{name}", "path": rel})
        for name, pattern in PRIVATE_RUNTIME_ID_PATTERNS.items():
            if pattern.search(text):
                violations.append({"kind": f"private_runtime_identifier:{name}", "path": rel})
        if path.suffix.lower() == ".md":
            for match in MARKDOWN_LINK.finditer(text):
                target = match.group(1).strip().strip("<>")
                if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                target = unquote(target.split("#", 1)[0])
                if not target:
                    continue
                resolved = (ROOT / target.lstrip("/")) if target.startswith("/") else (path.parent / target)
                resolved = resolved.resolve()
                try:
                    resolved.relative_to(ROOT)
                except ValueError:
                    violations.append({"kind": "markdown_link_outside_repository", "path": rel, "detail": target})
                    continue
                if not resolved.exists():
                    violations.append({"kind": "broken_markdown_link", "path": rel, "detail": target})
        if (
            path.suffix == ".py"
            and not rel.startswith("tests/")
            and rel != "scripts/audit_public_repo.py"
            and RETIRED_IMPORT.search(text)
        ):
            violations.append({"kind": "retired_vnpy_import", "path": rel})
        if rel.startswith(".github/workflows/"):
            for reference in ACTION_REFERENCE.findall(text):
                if reference.startswith("./"):
                    continue
                if not FULL_COMMIT_REFERENCE.fullmatch(reference):
                    violations.append(
                        {
                            "kind": "github_action_not_pinned_to_full_sha",
                            "path": rel,
                            "detail": reference,
                        }
                    )

    for missing in sorted(REQUIRED_PUBLIC_FILES - set(paths)):
        violations.append({"kind": "missing_public_governance_file", "path": missing})

    manifest_path = ROOT / SCREENSHOT_MANIFEST
    declared_screenshots: dict[str, dict] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = manifest.get("screenshots", [])
            if not isinstance(entries, list):
                raise ValueError("screenshots must be a list")
            for entry in entries:
                rel = entry.get("path") if isinstance(entry, dict) else None
                if not isinstance(rel, str) or not rel.startswith(SCREENSHOT_ROOT):
                    raise ValueError(f"invalid screenshot path: {rel!r}")
                if rel in declared_screenshots:
                    raise ValueError(f"duplicate screenshot path: {rel}")
                declared_screenshots[rel] = entry
        except (json.JSONDecodeError, ValueError) as exc:
            violations.append(
                {"kind": "invalid_screenshot_manifest", "path": SCREENSHOT_MANIFEST, "detail": str(exc)}
            )

    actual_screenshots = {
        rel for rel in paths
        if rel.startswith(SCREENSHOT_ROOT)
        and Path(rel).suffix.lower() in {".jpeg", ".jpg", ".png", ".webp"}
    }
    for rel in sorted(actual_screenshots - set(declared_screenshots)):
        violations.append({"kind": "unreviewed_public_screenshot", "path": rel})
    for rel in sorted(set(declared_screenshots) - actual_screenshots):
        violations.append({"kind": "missing_declared_screenshot", "path": rel})
    for rel in sorted(actual_screenshots & set(declared_screenshots)):
        path = ROOT / rel
        entry = declared_screenshots[rel]
        observed_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed_sha256 != entry.get("sha256"):
            violations.append({"kind": "screenshot_hash_mismatch", "path": rel})
        if path.stat().st_size != entry.get("bytes"):
            violations.append({"kind": "screenshot_size_mismatch", "path": rel})

    module_status = subprocess.check_output(
        ["git", "submodule", "status"], cwd=ROOT, text=True
    ).splitlines()
    for line in module_status:
        if line.startswith("-") or line.startswith("+") or line.startswith("U"):
            violations.append({"kind": "submodule_not_cleanly_pinned", "path": line.strip()})

    expected = {"third_party/quantgpt", "third_party/qlib", "third_party/tushare"}
    observed = {line.strip().split()[1] for line in module_status if len(line.strip().split()) >= 2}
    for missing in sorted(expected - observed):
        violations.append({"kind": "missing_submodule", "path": missing})

    return {
        "status": "passed" if not violations else "failed",
        "files_checked": len(paths),
        "submodules_checked": len(module_status),
        "violations": violations,
    }


def main() -> int:
    result = audit()
    # Secret matches are reduced to rule names and repository-relative paths;
    # matched credential text is never retained in or emitted by result.
    # codeql[py/clear-text-logging-sensitive-data]
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
