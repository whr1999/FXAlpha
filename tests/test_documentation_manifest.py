from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def test_documentation_manifest_classifies_every_top_level_markdown_document() -> None:
    manifest = yaml.safe_load((DOCS / "DOCUMENTATION_MANIFEST.yaml").read_text(encoding="utf-8"))
    classified: list[str] = []
    for group in (manifest.get("groups") or {}).values():
        assert group.get("status") in set((manifest.get("statuses") or {}).keys())
        classified.extend(str(item) for item in group.get("documents") or [])

    expected = {path.name for path in DOCS.glob("*.md")}
    documented = {Path(path).name for path in classified if not path.startswith("../")}
    assert documented == expected
    assert len(documented) == len([path for path in classified if not path.startswith("../")])


def test_current_documentation_routes_to_operations_entrypoints() -> None:
    for path in (
        DOCS / "DOCUMENTATION_INDEX_CURRENT.md",
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
    ):
        assert "OPERATIONS_INDEX" in path.read_text(encoding="utf-8")
