from services.transient_worker_environment import systemd_setenv_args


def test_systemd_setenv_args_only_propagates_allowlisted_runtime_values():
    args = systemd_setenv_args(
        {
            "FXALPHA_CONFIG_FILE": "/srv/fxalpha/config.yaml",
            "FXALPHA_POWERSHELL_EXE": "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
            "UNRELATED_VALUE": "must-not-propagate",
        }
    )

    assert args == [
        "--setenv=FXALPHA_CONFIG_FILE=/srv/fxalpha/config.yaml",
        "--setenv=FXALPHA_POWERSHELL_EXE=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    ]
