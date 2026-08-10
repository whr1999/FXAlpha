from types import SimpleNamespace

from services import platform_runtime_service as runtime


def test_systemd_user_unit_projects_runtime_fields(monkeypatch):
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "ActiveState=active\n"
                "SubState=waiting\n"
                "Result=success\n"
                "MainPID=0\n"
                "NRestarts=2\n"
                "ExecMainStatus=0\n"
                "NextElapseUSecRealtime=Fri 2026-08-07 07:30:00 CST\n"
                "LastTriggerUSec=Thu 2026-08-06 22:30:50 CST\n"
            ),
            stderr="",
        ),
    )

    result = runtime._systemd_user_unit("fxalpha-paper-fleet-daily.timer")

    assert result["available"] is True
    assert result["sub_state"] == "waiting"
    assert result["restart_count"] == 2
    assert result["swap_peak_bytes"] is None
    assert result["swap_peak_recorded"] is False
    assert result["next_trigger"] == "Fri 2026-08-07 07:30:00 CST"


def test_background_workflow_prefers_running_service(monkeypatch):
    rows = {
        "data.service": {"available": True, "active_state": "activating", "sub_state": "start"},
        "data.timer": {"available": True, "active_state": "active", "sub_state": "running"},
    }
    monkeypatch.setattr(runtime, "_systemd_user_unit", lambda unit: {"unit": unit, **rows[unit]})

    result = runtime._background_workflow(
        service_unit="data.service",
        timer_unit="data.timer",
        schedule="daily",
    )

    assert result["status"] == "running"
    assert result["service"]["unit"] == "data.service"
    assert result["service"]["operational_state"] == "running"


def test_background_workflow_projects_successful_oneshot_as_completed(monkeypatch):
    rows = {
        "data.service": {
            "available": True,
            "active_state": "inactive",
            "sub_state": "dead",
            "result": "success",
            "exit_status": 0,
        },
        "data.timer": {"available": True, "active_state": "active", "sub_state": "waiting"},
    }
    monkeypatch.setattr(runtime, "_systemd_user_unit", lambda unit: {"unit": unit, **rows[unit]})

    result = runtime._background_workflow(
        service_unit="data.service",
        timer_unit="data.timer",
        schedule="daily",
    )

    assert result["status"] == "scheduled"
    assert result["service"]["operational_state"] == "completed"
    assert result["timer"]["operational_state"] == "waiting"


def test_platform_automation_status_is_lightweight_projection(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "_automations_status",
        lambda: {"runtime": "WSL user systemd", "paper_trading": {"status": "scheduled"}},
    )

    result = runtime.platform_automation_status()

    assert result.ok
    assert result.outputs["automations"]["paper_trading"]["status"] == "scheduled"
    assert "usage" not in result.outputs


def test_automation_control_requires_confirmation():
    result = runtime.platform_automation_control(
        workflow="paper_trading",
        action="pause",
        confirm=False,
    )

    assert result.ok is False
    assert result.err == "automation_write_confirmation_required"


def test_automation_control_pauses_future_timer_without_stopping_service(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setenv("FXALPHA_AUTOMATION_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(runtime, "_run_systemctl_user", lambda *args, **kwargs: (commands.append(args) or True, ""))
    monkeypatch.setattr(
        runtime,
        "_background_workflow",
        lambda **kwargs: {"status": "idle", "service": {}, "timer": {"operational_state": "idle"}},
    )

    result = runtime.platform_automation_control(
        workflow="paper_trading",
        action="pause",
        confirm=True,
    )

    assert result.ok is True
    assert commands == [("disable", "--now", "fxalpha-paper-fleet-daily.timer")]
    assert "stop" not in commands[0]


def test_automation_control_updates_managed_schedule_override(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setenv("FXALPHA_SYSTEMD_USER_DIR", str(tmp_path / "systemd"))
    monkeypatch.setenv("FXALPHA_AUTOMATION_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(runtime, "_run_systemctl_user", lambda *args, **kwargs: (commands.append(args) or True, ""))
    monkeypatch.setattr(runtime, "_background_workflow", lambda **kwargs: {"status": "scheduled"})

    result = runtime.platform_automation_control(
        workflow="data_foundation",
        action="update_schedule",
        schedule_time="03:15",
        confirm=True,
    )

    override = tmp_path / "systemd" / "fxalpha-data-daily.timer.d" / "fxalpha-schedule.conf"
    assert result.ok is True
    assert "OnCalendar=Tue..Sat *-*-* 03:15:00 Asia/Shanghai" in override.read_text(encoding="utf-8")
    assert commands == [("daemon-reload",), ("restart", "fxalpha-data-daily.timer")]


def test_automation_control_rejects_invalid_schedule_time():
    result = runtime.platform_automation_control(
        workflow="data_foundation",
        action="update_schedule",
        schedule_time="25:90",
        confirm=True,
    )

    assert result.ok is False
    assert result.err == "automation_schedule_time_invalid"
