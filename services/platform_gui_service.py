from __future__ import annotations

from domain.platform_ops.gui_service import ensure_gui_services_started, gui_service_status_snapshot
from services._base import err_result, ok_result


def platform_gui_start():
    try:
        outputs = ensure_gui_services_started()
        if outputs.get("status") != "ready":
            return err_result(
                "platform_gui_services_not_ready",
                outputs=outputs,
                warnings=["gui_services_degraded"],
            )
        return ok_result(outputs=outputs)
    except Exception as exc:
        return err_result(
            "platform_gui_start_failed",
            outputs={
                "detail": str(exc),
                "operator_note": "GUI service start failed before readiness checks completed.",
            },
        )


def platform_gui_status():
    try:
        outputs = gui_service_status_snapshot()
        if outputs.get("status") != "ready":
            return err_result(
                "platform_gui_services_degraded",
                outputs=outputs,
                warnings=["gui_services_degraded"],
            )
        return ok_result(outputs=outputs)
    except Exception as exc:
        return err_result(
            "platform_gui_status_failed",
            outputs={
                "detail": str(exc),
                "operator_note": "GUI service status inspection failed.",
            },
        )
