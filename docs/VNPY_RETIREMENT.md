# vn.py retirement

## Decision

The publication candidate does not use vn.py. Current simulated execution is an
FXAlpha paper-account state machine using Qlib exchange semantics for price,
lot size, fees, suspension/limit behavior, and account progression.

## Removed from the candidate

- vn.py engine/session, gateway, strategy, event-collector, ledger-builder,
  symbol-mapper, portfolio-mapping, runner, and adapter modules
- API/CLI handlers that invoked the vn.py execution lane
- hard-coded `.vntrader` and vn.py source/site-package paths
- `vt_symbol` persistence in new registry schemas and GUI display contracts
- vn.py dependency/runtime-noise hooks

Existing production databases may contain an unused historical `vt_symbol`
column. The candidate tolerates extra columns and does not destructively migrate
the production database.

## Preserved capability

- promoted-model prediction and recommendation
- paper-account creation/status/fleet/replay lifecycle
- frozen score, target, order, execution, position, and account evidence
- Qlib paper fills and daily account ledger
- risk-policy and confidence-to-exposure controls

## Private rollback evidence

Before retirement work, the deployed execution database, paper-fleet status,
installed package list, and `.vntrader` state were copied to a private directory
outside this Git repository with SHA-256 checksums. They are not publication
artifacts and must never be pushed.

## Acceptance checks

1. No tracked application file imports `vnpy`, `vnpy_paperaccount`, or
   `vnpy_portfoliostrategy`.
2. Those packages are absent from the isolated test environment.
3. Public audit and regression tests pass.
4. Paper-fleet status/preflight can run against a copied test database.
5. The production service and its data remain unchanged until a separate
   cutover approval.

## Production cleanup later

Only after the public candidate has run in parallel and rollback has been
validated should an operator stop the old service, switch the service working
directory/environment, and eventually remove vn.py packages or `.vntrader` from
the production host. That cleanup is not part of this preparation phase.
