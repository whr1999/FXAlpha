from __future__ import annotations

import os
from collections.abc import Mapping


# A transient user service does not inherit the EnvironmentFile of the API
# service which launched it. Keep the allowlist explicit so background workers
# resolve the same external config and machine-local runtime integrations.
TRANSIENT_WORKER_ENV_KEYS = (
    "FXALPHA_CONFIG_FILE",
    "FXALPHA_POWERSHELL_EXE",
    "QGPT_URL",
    "DATABASE_URL",
    "QUANTGPT_REPORTS_DIR",
    "QUANTGPT_RESEARCH_NOTES_DIR",
    "AUTH_DISABLED",
    "TUSHARE_TOKEN",
    "FXALPHA_LLM_API_KEY",
    "FXALPHA_LLM_BASE_URL",
)


def systemd_setenv_args(env: Mapping[str, str] | None = None) -> list[str]:
    """Return allowlisted ``systemd-run --setenv`` arguments.

    ``systemd-run --user`` talks to the user manager; it does not copy the
    launching service's full process environment into the transient unit.
    """

    source = os.environ if env is None else env
    return [f"--setenv={key}={source[key]}" for key in TRANSIENT_WORKER_ENV_KEYS if source.get(key)]
