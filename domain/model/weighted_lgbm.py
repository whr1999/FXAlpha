"""Compatibility import for immutable Qlib manifests.

The canonical implementation is :mod:`domain.model.reweight`.  Keeping this
as a separate module avoids importing Qlib merely to initialize
``domain.model`` in lightweight API and tooling processes.
"""

from .reweight import *  # noqa: F401,F403
