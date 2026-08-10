# Model naming contract

Current display contract: `model_display_v1`.

## Human-facing names

All GUI and service projections must use:

```text
<stage> · <FEATURE-SET> · <Asia/Shanghai time> [· R<round_no>]
```

Examples:

```text
研究 · FAMILY-TOP3 · 2026-08-03 21:07 · R6
候选 · FAMILY-TOP2 · 2026-08-03 21:35
ROLLING · ACTIVE50 · 2026-08-03 21:42
生产 · ACTIVE50 · 2026-08-04 09:22
```

The primary name must never use a Registry id, run id, campaign id, legacy
`0703` prefix, or raw artifact directory. Those identifiers remain available
in details, tooltips, copy actions, manifests, and audit payloads.

`domain.model.naming` is the authoritative formatter for service projections.
The GUI fallback formatter mirrors the same contract for raw runtime rows that
have not passed through the model service.

## Immutable technical identifiers

Technical identifiers are not renamed because they are referenced by artifact
paths, lineage, Registry rows, Rolling evidence, and trading accounts.

- session: `msession_<UTC timestamp>`
- orchestrator job: `model_orch_<UTC timestamp>`
- round: `mround_<UTC YYYYMMDD_HHMMSS>_<experiment hash8>`
- seed run: `mrun_<round_group_id>_s<seed>_<hash8>`
- Registry row: `m_<registration timestamp>_<uuid6>`
- Rolling campaign: `model_roll_<UTC timestamp>`
- production refit: `model_prod_<source>_<UTC timestamp>`

Historical `model0703`, `m0703`, `mr0703`, `ms0703`, and `roll0703` identifiers
are normalized only for display. Their persisted values remain unchanged.

## Round numbering

`Round 0` is the baseline. `Round 1..N` are tuning rounds. Round number is a
session-local ordinal and is not encoded into `round_group_id`; the GUI joins it
from the model run catalog. `Round 0` must be displayed explicitly.
