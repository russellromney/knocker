# Spec Diff: Minimal Retention And Pruning

Phase:
- 004-minimal-retention-and-pruning

Session:
- A

## What changes

- Knocker adds a minimal, explicit operator pruning surface for old stored data.
- This phase adds Python-first pruning operations for:
  - `Event` rows in terminal states (`handled`, `ignored`)
  - linked `Attempt` rows for pruned events
  - linked `Delivery` rows for pruned events
  - orphan `Delivery` rows older than a threshold
- Pruning is explicit operator action, not a background scheduler.
- The operator can prune only rows older than a caller-provided integer Unix timestamp threshold.
- In this minimal phase, pruning age always means `received_at` age:
  - event pruning uses `knocker_events.received_at`
  - orphan-delivery pruning uses `knocker_deliveries.received_at`
- Event pruning is status-bounded:
  - `handled` and `ignored` are supported in this phase
  - `failed` and `dead` remain deferred
  - `received` and `processing` are never prunable in this phase
- This phase adds a small verb-shaped Python operator surface:
  - `prune_events(...)`
  - `prune_orphan_deliveries(...)`
- `prune_events(...)` supports only these filter axes in `004`:
  - `statuses`
  - `older_than`
  - `limit`
- `prune_orphan_deliveries(...)` supports only these filter axes in `004`:
  - `older_than`
  - `limit`
- Public prune methods require an explicit `limit` in this phase. `limit` must be within `1..1000` and does not default to "all matching rows."
- Prune operations return typed summary results rather than raw SQL counts only:
  - `PruneEventsResult(events_pruned, attempts_pruned, deliveries_pruned, live_jobs_pruned)`
  - `PruneDeliveriesResult(deliveries_pruned)`
- If a worker later holds a claimed Honker job for an event that was explicitly pruned, dispatch treats that job as stale retention residue and exits without crashing the worker.

## What does not change

- Knocker remains a same-process, same-SQLite-file library.
- This phase does not add automatic retention jobs, cron scheduling, or vacuum automation.
- This phase does not add HTML admin UI or JSON admin endpoints.
- This phase does not add Node or future-binding parity for pruning.
- This phase does not add new event state transitions.
- This phase does not change ingest, dedupe, verification, replay, requeue, or ignore semantics.
- This phase does not add DST- or local-calendar-based retention rules.
- This phase does not add pruning for `failed` or `dead` events; that remains the next retention gap after this minimal slice.

## Invariants

- Pruning is explicit and operator-invoked; Knocker does not delete retained data automatically in this phase.
- Event pruning is allowed only for terminal event states that are intentionally safe in v1: `handled` and `ignored`.
- Each public prune call commits as one transaction: candidate selection, linked-row deletion, parent-row deletion, stale live-job cleanup, and summary return are all-or-nothing.
- Pruning one event removes its linked `Attempt` rows and linked `Delivery` rows in the same operation.
- Orphan-delivery pruning is separate from event pruning and operates only on deliveries with `event_id IS NULL`. It follows the orphan axis from `003`, not the invalid-signature axis.
- Time thresholds use integer Unix timestamps only and apply to `received_at`. This phase does not define calendar-day semantics.
- `older_than` is a strict cutoff: rows match only when `received_at < older_than`.
- Prune filters are conjunctive: all provided filters compose with `AND`.
- When `limit` truncates the candidate set, prune selects the oldest matching rows first using `ORDER BY received_at ASC, id ASC`.
- Prune operations return stable, typed summaries whose shape does not change without a later spec diff.
- The append-only `Delivery` rule from `002` and the current `SYSTEM.md` baseline applies to normal ingest and operator inspection flows. Explicit retention pruning in this phase is the documented exception that may delete old delivery rows.
- After a successful event-prune call, no `_honker_live` row for this Knocker queue may reference a deleted event id. Prune is responsible for deleting stale live-job rows for the deleted events in the same transaction.

## How we will verify it

- Operators can prune old `handled` events without handwritten SQL.
- Operators can prune old `ignored` events without handwritten SQL.
- Pruning an event removes its linked attempts and deliveries.
- Pruning an event removes any stale `_honker_live` rows that still reference that event.
- Operators cannot prune `received` or `processing` events through the public pruning surface.
- Operators can prune old orphan deliveries without affecting linked events.
- Prune operations are bounded by caller-provided `received_at` thresholds and do not apply DST or local-day logic.

## Notes

- This slice is intentionally narrow: explicit, safe pruning first; richer retention policy later.
