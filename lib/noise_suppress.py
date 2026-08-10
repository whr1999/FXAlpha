"""Suppress known third-party import-time noise.

Call this before importing qlib or gym to prevent stderr pollution from
upstream packages that do not respect headless/server environments.
"""

from __future__ import annotations

import sys
import warnings
from contextlib import contextmanager
from io import StringIO


def _suppress_gym_notices() -> None:
    """Patch gym_notices to emit no version warnings."""
    try:
        import gym_notices.notices as notices

        notices.notices = {}  # type: ignore[attr-defined]
    except Exception:
        pass


def suppress_all_known_noise() -> None:
    """Suppress known import-time messages from optional runtime packages."""
    _suppress_gym_notices()
    warnings.filterwarnings("ignore", module="joblib")
    warnings.filterwarnings("ignore", module="loky")


@contextmanager
def suppress_stderr():
    """Temporarily suppress stderr."""
    old_stderr = sys.stderr
    sys.stderr = StringIO()
    try:
        yield
    finally:
        sys.stderr = old_stderr
