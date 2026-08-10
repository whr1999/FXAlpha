from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class ServiceResult:
    ok: bool
    err: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ok_result(**kwargs) -> ServiceResult:
    return ServiceResult(ok=True, **kwargs)


def err_result(err: str, **kwargs) -> ServiceResult:
    return ServiceResult(ok=False, err=err, **kwargs)
