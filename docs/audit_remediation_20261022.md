# 2026-10-22 audit remediation contract

Scope: development source remediation of DATA-01/02/03/04, ENG-01,
RT-01/02/03/04, R1/R2/R3 and UI-01. Production databases, runtime releases,
scheduled tasks and operation state are outside this change.

## Data and receipt preservation

Market rise/fall batches are validated before writes. The raw batch and
daily-summary projection commit in one transaction. Sector replacement uses the
same transaction discipline; company-info replacement uses the atomic merge.
Failed batches must preserve the previously correct rows.

Each RunManifest owns an exclusively created directory. Reusing a run ID,
including an abandoned partial directory, is rejected. A retry must use a new
ID; keep the prior receipt for comparison. Receipt writes use unique temporary
files and atomic replacement. Collector dependency preflight failures are
recorded only after the attempt directory exists, before collection starts.

Cold-storage inventory always opens the hot database read-only. Its destructive
`--execute` mode is permanently retired, including for late-arriving historical
rows. Offsite backup and legacy operator outcome import are also retired.
They fail before accessing their targets; see scripts/README.md and
operator_outcome_import.md. There is currently no replacement offsite transfer
or destructive cold-split capability.

## K-line product identity and migration boundary

`multi_source_kline` identity is `(source_date, asset_type, asset_code, provider,
adjustment)`. Explicit none/qfq/hfq products coexist. Quality deduplication uses
the same identity. Unknown historical adjustment stays unknown; no relabeling
or row deletion occurs in migration 0016.

New databases create `uq_multi_source_kline_product`. Existing databases with
the old narrower unique index are refused by MultiSourceStore until an explicit
maintenance migration. Migration `0016_kline_product_identity.sql` creates the
new index before dropping the old one, inside the existing migration runner's
transaction. Duplicate conflicts fail without deleting rows.

Before an independently authorized production application: obtain an exclusive
writer guard, prove qualified backup/restore, inspect adjustment values and
duplicates using the full product key, and apply pending migrations to an
isolated copy first. Compare row counts and all product values before/after and
re-run the migration to prove idempotency. Resolve unknown/duplicate identities
from original source evidence, not guessed adjustment labels. Once multiple
adjustments coexist, rollback cannot recreate the narrow key without losing
products: stop writes and restore the qualified prior snapshot, retaining later
receipts for explicit recovery. This source change does not apply migration 0016
to production.

## Data qualification

Missing or incomplete limit-pool inputs yield NULL totals and `partial`, never
known zero. Only a complete date-bound HiThink receipt with matching observed
row counts can qualify native pool totals, including a certified empty result.
Threshold-derived daily breadth remains `estimated`. Legacy views without a
status column are `unverified_legacy` in the workspace. Consumers must retain
these statuses rather than advertise the numbers as available official totals.

For native board height, HiThink precedes xiaodefa, then KPL and other fallbacks.
xiaodefa must supply a positive integral native `limit_times`; old locally
derived `board_level` alone is insufficient. Receipt time does not outrank the
approved source order.

## Paper safety and monitoring

Paper buy evaluation/confirmation and submission recheck all held marks at the
current clock. Certificate expiry is limited by every held mark's TTL and
session date. Old longer-lived certificates also undergo submission-time
validation. This never sets execution_ready=true.

Checkpoint matching priority is the verified submission sequence. New checkpoint
envelopes use `state_delta_fifo_v3` and retain order_sequence; old envelopes recover it from the verified
journal prefix. Existing state hashes and journals are not rewritten. Missing,
duplicate or conflicting sequence identities fail closed.

Heartbeats retain last_attempt and last_success separately. Start/failure never
advance success. Completion monitoring reports failure/in-progress/stale/missing
and can read explicit pause/calendar context without changing it. A closed or
paused context is neutral, not successful completion.

All JSON embedded in legacy review HTML script text escapes HTML delimiters and
Unicode line separators while preserving JSON round-trip values. Interaction
data stays inline; no runtime fetch or sidecar is introduced. DOM rendering also
escapes provider names/codes/labels before inserting HTML. The first concept's
truncated stock slice carries its total so the expand control remains available.

## Acceptance

Regression fixtures cover the original counterexamples, failure preservation,
repeat/concurrent attempt allocation, migration idempotency, all four K-line
asset categories, partial/empty totals, field-qualified provider priority,
held-mark expiry, old certificates and checkpoint envelopes, retired CLI/API
refusal, heartbeat states and mixed-case script injection. Run the full release
profile (including history and stress) on a frozen isolated source copy, plus
lint, migration lint, dependency checks and actual offline browser interaction.
Research dependency acceptance remains a separate profile; passing release
tests is not evidence of research validity or permission to resume operations.
