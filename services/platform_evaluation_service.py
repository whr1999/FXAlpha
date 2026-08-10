from __future__ import annotations

from domain.platform_evaluation import (
    EvaluationProfileError,
    evaluation_profile_status,
    set_default_evaluation_mode,
)
from services._base import ServiceResult, err_result, ok_result


def platform_evaluation_status() -> ServiceResult:
    try:
        return ok_result(outputs=evaluation_profile_status())
    except EvaluationProfileError as exc:
        return err_result("evaluation_profile_invalid", outputs={"detail": str(exc)})


def platform_evaluation_set_mode(*, evaluation_mode: str, changed_by: str = "web_gui") -> ServiceResult:
    try:
        status = set_default_evaluation_mode(evaluation_mode, changed_by=changed_by)
    except EvaluationProfileError as exc:
        return err_result(
            "invalid_evaluation_mode",
            inputs={"evaluation_mode": evaluation_mode, "changed_by": changed_by},
            outputs={"detail": str(exc)},
        )
    return ok_result(
        inputs={"evaluation_mode": evaluation_mode, "changed_by": changed_by},
        outputs=status,
        warnings=["Mode changes apply only to newly-created tasks; running and completed tasks are unchanged."],
    )
