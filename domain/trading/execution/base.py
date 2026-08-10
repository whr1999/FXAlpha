from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExecutionInput:
    version_id: str
    trade_date: str
    score_file: Path
    target_file: Path
    initial_capital: float
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    ok: bool
    adapter: str
    version_id: str
    trade_date: str
    output_files: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
