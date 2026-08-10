# Contributing

External code contributions are not accepted until the maintainers replace the
project's all-rights-reserved license with a contribution-compatible license.
Documentation and issue reports are welcome through GitHub.

For maintainer changes:

1. Create a focused branch.
2. Do not commit `config.yaml`, `.env`, data, runtime state, databases, logs,
   model artifacts, or credentials.
3. Do not vendor third-party source into the main tree; update the appropriate
   fork and then advance its submodule pin.
4. Run `python scripts/run_release_preflight.py`.
5. Update the relevant current document in the same change.

Changes to data promotion, factor import, model promotion, or paper-account
writes must preserve their existing gates and audit records.
