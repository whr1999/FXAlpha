#!/usr/bin/env python3
"""Fail closed on secrets, private paths, and oversized blobs reachable from HEAD."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import PurePosixPath


MAX_BLOB_BYTES = 5 * 1024 * 1024
FORBIDDEN_ROOTS = (
    ".claude/", ".idea/", ".openai/", ".vscode/", "artifacts/",
    "checkpoints/", "data/", "factor_values/", "log/", "logs/", "mlruns/",
    "models/", "predictions/", "runtime/",
)
FORBIDDEN_NAMES = {".env", "config.yaml"}
FORBIDDEN_SUFFIXES = (
    ".7z",
    ".arrow",
    ".ckpt",
    ".csv",
    ".db",
    ".feather",
    ".h5",
    ".hdf5",
    ".joblib",
    ".key",
    ".npy",
    ".npz",
    ".onnx",
    ".p12",
    ".parquet",
    ".pem",
    ".pfx",
    ".pickle",
    ".pkl",
    ".pt",
    ".pth",
    ".safetensors",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tsv",
    ".zip",
)
PERSONAL_PATHS = (
    "/home/roy/",
    "/mnt/c/Users/whr_9/",
    "C:/Users/whr_9/",
    r"C:\Users\whr_9",
)
PERSONAL_PATH_EXEMPTIONS = {
    "scripts/audit_git_history.py",
    "scripts/audit_public_repo.py",
}
SECRET_PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(rb"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "gitlab_token": re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    "aws_access_key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "openai_style_key": re.compile(rb"\bsk-[A-Za-z0-9_-]{24,}\b"),
    "slack_token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "stripe_live_key": re.compile(rb"\bsk_live_[A-Za-z0-9]{20,}\b"),
    "credential_url": re.compile(
        rb"https?://[^/@\s:]+:[^/@\s]+@"
        rb"(?!(?:[^/]+\.)?example\.(?:com|net|org)(?:[/:]|\s|$))[^\s]+"
    ),
}
PRIVATE_RUNTIME_ID_PATTERNS = {
    "factor_registry_id": re.compile(rb"\bf_[0-9]{8}_[0-9]{6}_[0-9]+\b"),
    "factor_research_run_id": re.compile(rb"\bfr_[0-9]{8}_[0-9]{6}_[0-9a-f]+\b"),
    "model_registry_id": re.compile(rb"\bm_[0-9]{8}_[0-9]{6}_[0-9]+_[0-9a-f]+\b"),
    "production_model_run_id": re.compile(rb"\bmodel_prod_model_roll_[A-Za-z0-9_.:-]{20,}\b"),
    "legacy_feature_snapshot_id": re.compile(
        rb"\bfs-model0703-(?:active|ab|diagnostic|family|temporal)[A-Za-z0-9_.:-]*\b"
    ),
}


def git(*arguments: str, binary: bool = False) -> bytes | str:
    completed = subprocess.run(
        ["git", *arguments],
        check=True,
        capture_output=True,
        text=not binary,
    )
    return completed.stdout


def reachable_objects() -> list[tuple[str, str]]:
    # Audit exactly the history that the upload runbook maps from HEAD to public
    # main. Private construction branches are separate local refs and must never
    # be pushed with --all.
    output = str(git("rev-list", "--objects", "HEAD"))
    objects: list[tuple[str, str]] = []
    for line in output.splitlines():
        object_id, separator, path = line.partition(" ")
        objects.append((object_id, path if separator else ""))
    return objects


def audit() -> dict[str, object]:
    violations: list[dict[str, object]] = []
    blob_count = 0
    largest_blob = {"bytes": 0, "path": "", "object": ""}

    for object_id, path in reachable_objects():
        object_type = str(git("cat-file", "-t", object_id)).strip()
        if object_type != "blob":
            continue
        blob_count += 1
        size = int(str(git("cat-file", "-s", object_id)).strip())
        if size > int(largest_blob["bytes"]):
            largest_blob = {"bytes": size, "path": path, "object": object_id}
        if size > MAX_BLOB_BYTES:
            violations.append(
                {
                    "kind": "oversized_history_blob",
                    "path": path,
                    "object": object_id,
                    "bytes": size,
                }
            )

        if path:
            filename = PurePosixPath(path).name
            if path.startswith(FORBIDDEN_ROOTS):
                violations.append(
                    {"kind": "generated_state_in_history", "path": path, "object": object_id}
                )
            private_env = filename.startswith(".env.") and filename != ".env.example"
            if filename in FORBIDDEN_NAMES or private_env or path.endswith(FORBIDDEN_SUFFIXES):
                violations.append(
                    {"kind": "private_file_in_history", "path": path, "object": object_id}
                )
            if path.startswith(".codex/") and path != ".codex/config.example.toml":
                violations.append(
                    {"kind": "private_tooling_in_history", "path": path, "object": object_id}
                )

        if size > 2_000_000:
            continue
        content = bytes(git("cat-file", "blob", object_id, binary=True))
        if b"\0" in content:
            continue
        if path not in PERSONAL_PATH_EXEMPTIONS:
            decoded = content.decode("utf-8", errors="replace")
            for marker in PERSONAL_PATHS:
                if marker in decoded:
                    violations.append(
                        {
                            "kind": "personal_path_in_history",
                            "path": path,
                            "object": object_id,
                            "detail": marker,
                        }
                    )
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                violations.append(
                    {
                        "kind": f"secret_pattern_in_history:{name}",
                        "path": path,
                        "object": object_id,
                    }
                )
        for name, pattern in PRIVATE_RUNTIME_ID_PATTERNS.items():
            if pattern.search(content):
                violations.append(
                    {
                        "kind": f"private_runtime_identifier_in_history:{name}",
                        "path": path,
                        "object": object_id,
                    }
                )

    commit_count = int(str(git("rev-list", "--count", "HEAD")).strip())
    return {
        "status": "passed" if not violations else "failed",
        "commits_checked": commit_count,
        "blobs_checked": blob_count,
        "largest_blob": largest_blob,
        "max_blob_bytes": MAX_BLOB_BYTES,
        "violations": violations,
    }


def main() -> int:
    result = audit()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
