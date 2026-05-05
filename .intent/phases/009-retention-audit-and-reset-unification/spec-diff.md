# Spec Diff: Retention Audit And Reset Unification

Phase:
- 009-retention-audit-and-reset-unification

Session:
- A

## What changes

- Knocker adds a durable audit trail for explicit retention/pruning operations.
- Each successful prune call records what kind of prune happened, when it happened, and a compact summary of what was removed.
- Knocker adds one core reset primitive, `knocker_reset_event(...)`, so Python recovery paths stop hand-rolling equivalent reset behavior.
- Python `replay(...)`, `requeue(...)`, and `replay_delivery(...)` continue to expose the same operator surface, but they now rely on the same core reset semantics underneath.
- Python gains a small `list_prune_audits(...)` read helper so operators can inspect the audit trail without raw SQL.
- Operator docs and the runbook gain an explicit answer to:
  - what got deleted
  - when it was deleted
  - why it was deleted
  - what replay/requeue/replay-delivery reset actually means

## What does not change

- Knocker does not add automatic retention jobs or background retention scheduling in this phase.
- Knocker does not add a general-purpose control plane or retention daemon.
- Knocker does not change the append-only `Delivery` rule except for the already-accepted explicit prune carve-out.
- Knocker does not change the public Python method names for:
  - `prune_events(...)`
  - `prune_orphan_deliveries(...)`
  - `replay(...)`
  - `requeue(...)`
  - `replay_delivery(...)`
- Knocker does not add richer cross-binding operator parity in this phase.

## Invariants

- Explicit prune operations are operator actions and must leave behind a durable audit row.
- A prune audit row records summary facts, not a second copy of every deleted row.
- Prune audit rows must be written in the same transaction as the prune they describe. If the prune rolls back, its audit row must not survive.
- Prune audit rows are Knocker-owned records, not application logs. Operators should be able to answer basic retention questions from the database without depending on external logging.
- Prune audit rows are never themselves targeted by `prune_events(...)` or `prune_orphan_deliveries(...)`. They form a separate audit trail.
- The prune-audit table uses stable top-level columns plus a structured JSON summary field:
  - `id` INTEGER PRIMARY KEY
  - `kind` TEXT NOT NULL — `'prune_events'` or `'prune_orphan_deliveries'`
  - `queue_name` TEXT NOT NULL
  - `executed_at` INTEGER NOT NULL DEFAULT (unixepoch())
  - `events_pruned` INTEGER — count of event rows removed; NULL for orphan-delivery prunes
  - `deliveries_pruned` INTEGER — count of delivery rows removed
  - `attempts_pruned` INTEGER — count of attempt rows removed; NULL for orphan-delivery prunes
  - `live_jobs_pruned` INTEGER — count of live job rows removed; NULL for orphan-delivery prunes
  - `summary_json` TEXT NOT NULL — structured operator inputs for this prune call
- The `summary_json` minimum keys per kind are:
  - `prune_events`: `{"statuses": [...], "older_than": <int>, "limit": <int>}`
  - `prune_orphan_deliveries`: `{"older_than": <int>, "limit": <int>}`
- Counts are defined as all rows removed from each table as a consequence of the prune operation, whether by direct `DELETE` or by `ON DELETE CASCADE`. This means:
  - `events_pruned` counts direct event-row deletions.
  - `deliveries_pruned` counts direct delivery-row deletions.
  - `attempts_pruned` counts attempt rows removed (cascaded from event deletion).
  - `live_jobs_pruned` counts explicit live-job deletions performed by the prune path.
- No-op prune behavior: if `prune_events(...)` or `prune_orphan_deliveries(...)` deletes zero rows but succeeds, an audit row is still written with zero counts and the same `summary_json`. The operator trail must not be ambiguous.
- `knocker_reset_event(...)` is the single core reset primitive for event recovery.
- `knocker_reset_event(...)` is a **low-level primitive**: it performs the reset state transition and does **not** validate source-event status. Callers (`knocker_replay(...)`, `knocker_requeue(...)`, Python `replay_delivery(...)`) are responsible for enforcing their own allowed-source-state preconditions.
- Reset means:
  - event status becomes `received`
  - `attempt_count` resets to `0`
  - `last_error` is cleared
  - `handled_at` is cleared
  - canonical event payload does not change
- `knocker_reset_event(...)` does **not** record attempt history. Attempt-history insertion remains the responsibility of handler dispatch (`mark_handled`, `mark_failed`, `mark_ignored`).
- `replay(...)`, `requeue(...)`, and `replay_delivery(...)` may differ in which payload they enqueue, but they should not differ in how they reset the stored event row.
- Reset must remain atomic with enqueue/live-job cleanup in the same surrounding transaction.

## How we will verify it

- Tests cover:
  - successful `prune_events(...)` writes one audit row with correct summary counts
  - successful `prune_orphan_deliveries(...)` writes one audit row with correct summary counts
  - a no-op `prune_events(...)` (zero rows deleted) still writes one audit row with zero counts
  - a no-op `prune_orphan_deliveries(...)` (zero rows deleted) still writes one audit row with zero counts
  - failed prune transactions do not leave audit residue
  - audit rows are queryable through the Python `list_prune_audits(...)` helper
  - `list_prune_audits(...)` returns results newest-first and respects `since` and `limit`
  - `replay(...)`, `requeue(...)`, and `replay_delivery(...)` all produce the same reset state on the event row
  - reset does not mutate canonical event payload
  - `knocker_reset_event(...)` does not insert into `knocker_attempts`
  - existing replay/requeue/replay-delivery behavioral tests still pass
- Docs explain prune audit semantics and reset semantics in operator terms.

## Notes

- This phase is about operator trust: retention should be inspectable, and recovery should mean one thing everywhere.
- The goal is not “never delete anything”; the goal is “deletion and reset are explicit, durable, and explainable.”
