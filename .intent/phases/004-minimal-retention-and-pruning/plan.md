# Plan

Phase:
- 004-minimal-retention-and-pruning

Session:
- A

## Goal

- Add a minimal, explicit Python pruning surface that lets operators safely remove old `handled` / `ignored` events and old orphan deliveries, while preserving the `004` safety boundaries around status scope, transactionality, and queue consistency.

## Context

- `003` made the stored Delivery/Event model operable, but Knocker still has no supported retention path before `0.1.0`.
- The `004` spec diff intentionally keeps retention narrow:
  - explicit operator action only
  - Python-first
  - event pruning only for `handled` / `ignored`
  - orphan-delivery pruning as a separate operation
  - integer `received_at` thresholds only
- The current schema matters for the implementation shape:
  - `knocker_attempts.event_id` has `ON DELETE CASCADE`
  - `knocker_deliveries.event_id` does not cascade on event delete
  - `_honker_live` stores JSON payloads with `{ "event_id": ... }`
- That means `004` should be implemented as Python-owned SQL transactions with an explicit delete order rather than by leaning on new Rust/UDF work.

## References

- `SYSTEM.md`
- `.intent/phases/004-minimal-retention-and-pruning/spec-diff.md`
- `.intent/phases/004-minimal-retention-and-pruning/reviews_and_decisions.md`
- `packages/knocker/python/knocker/_knocker.py`
- `knocker-honker/src/lib.rs`
- `tests/test_knocker_honker.py`

## Mapping from spec diff to implementation

- "Python-first pruning operations" means:
  - add public Python methods on `Knocker`
  - do not add Rust pruning UDFs in this slice
- "`handled` and `ignored` only" means:
  - validate requested statuses in Python before any delete work
  - reject `received`, `processing`, `failed`, and `dead`
- "single transaction per public prune call" means:
  - each method opens one database transaction
  - candidate selection, stale live-job cleanup, child deletion, parent deletion, and summary computation all happen inside that transaction
- "pruning one event removes linked attempts and deliveries" means:
  - delete deliveries manually before deleting events
  - rely on `ON DELETE CASCADE` for attempts
- "after prune, no `_honker_live` row references a deleted event" means:
  - identify matching live jobs by parsing Honker payload JSON in Python
  - delete those `_honker_live` rows for `self.queue.name` in the same transaction as event deletion
- "orphan-delivery pruning is separate and follows the orphan axis" means:
  - prune only `knocker_deliveries` rows where `event_id IS NULL`
  - do not fold verification-outcome filtering into this API
- "typed summary results" means:
  - add result dataclasses rather than returning bare integers or dicts
- "stale claimed jobs must not crash the worker" means:
  - if dispatch sees a claimed job whose `event_id` no longer exists because of explicit prune, it exits quietly after best-effort queue cleanup rather than raising `KeyError`

## Phase decisions

- This slice stays Python-only. No new core UDFs, no schema changes, no migration work.
- Public method shape is:
  - `prune_events(...)`
  - `prune_orphan_deliveries(...)`
- `older_than` uses strict `received_at < older_than`.
- Candidate selection is oldest-first when `limit` truncates matches: `ORDER BY received_at ASC, id ASC`.
- `limit` is required and must be within `1..1000`.
- Event pruning supports only `handled` and `ignored` statuses.
- `prune_events(statuses=...)` accepts a non-empty `list[str]` or `tuple[str, ...]` only.
- A bare string for `statuses` is rejected.
- An empty `statuses` collection is rejected.
- Orphan-delivery pruning supports only the orphan axis (`event_id IS NULL`), not the invalid-signature axis.
- Result types are:
  - `PruneEventsResult(events_pruned, attempts_pruned, deliveries_pruned, live_jobs_pruned)`
  - `PruneDeliveriesResult(deliveries_pruned)`
- `_honker_live` cleanup is part of prune, not a precondition left to operators.
- `_honker_live` cleanup is queue-scoped to `self.queue.name`, not global across all queues in the database.
- This slice does not add dry-run mode. If dry-run becomes necessary, it should be a later spec diff rather than silent scope growth here.
- Validation and public errors should follow the `003` style:
  - type mismatches raise `TypeError`
  - range or membership violations raise `ValueError`
- `attempts_pruned` is counted with an explicit pre-delete `COUNT(*)` query over candidate event ids before event deletion triggers the cascade.
- The retention preview path in this minimal slice is the existing read surface from `003`, not a new dry-run flag.

## Proposed implementation approach

- Add two new result dataclasses near the current `Event` / `Delivery` types:
  - `PruneEventsResult`
  - `PruneDeliveriesResult`
- Add two new public methods on `Knocker`:
  - `prune_events(statuses: list[str] | tuple[str, ...], older_than: int, limit: int) -> PruneEventsResult`
  - `prune_orphan_deliveries(older_than: int, limit: int) -> PruneDeliveriesResult`
- Keep both methods fully transaction-wrapped with `self.db.transaction()`.

- `prune_events(...)` implementation shape:
  1. validate `older_than`, `limit`, and `statuses`
     - reject bools / non-ints for `older_than` and `limit` with `TypeError`
     - reject `limit` outside `1..1000` with `ValueError`
     - reject a bare string for `statuses` with `TypeError`
     - reject an empty collection or unsupported statuses with `ValueError`
  2. select candidate event ids with:
     - `status IN (?, ?...)`
     - `received_at < ?`
     - oldest-first ordering
     - `LIMIT ?`
  3. if there are no candidates, return zero counts
  4. count linked deliveries for those event ids
  5. count linked attempts for those event ids before deleting parent rows
  6. load `_honker_live` rows for `self.queue.name`, parse payload JSON in Python, and collect live-job ids whose `event_id` is in the candidate set
  7. delete matching `_honker_live` rows by id
  8. delete `knocker_deliveries` rows for the candidate event ids
  9. delete `knocker_events` rows for the candidate ids
  10. return the typed summary

- `prune_orphan_deliveries(...)` implementation shape:
  1. validate `older_than` and `limit`
     - reject bools / non-ints with `TypeError`
     - reject `limit` outside `1..1000` with `ValueError`
  2. select candidate delivery ids where:
     - `event_id IS NULL`
     - `received_at < ?`
     - oldest-first ordering
     - `LIMIT ?`
  3. if there are no candidates, return zero
  4. delete those delivery rows
  5. return the typed summary

- Keep all prune SQL in Python for this slice, next to the existing operator read methods from `003`.
- Update Python docs to describe the two prune methods, their intentionally narrow scope, and the operator preview path (`list_events(...)` / `list_deliveries(...)`) instead of a dry-run flag.
- While implementing prune, make `_dispatch_job(...)` tolerate a missing event row caused by explicit prune:
  - if `get_event(event_id)` misses, attempt a best-effort `ack(...)`
  - do not raise if the claim is already gone because prune removed the queue row
  - exit quietly

## Build order

1. Add result dataclasses and small validation helpers in `packages/knocker/python/knocker/_knocker.py`.
2. Implement `prune_events(...)` with event selection, live-job discovery, manual delivery deletion, event deletion, and summary return.
3. Implement `prune_orphan_deliveries(...)`.
4. Add focused tests for event pruning, orphan-delivery pruning, rejection cases, and transactional behavior.
5. Update README/package docs for the new pruning surface and the preview path.
6. Run the full test suite and record evidence.

## Acceptance

- `Knocker.prune_events(...)` exists on the public Python surface.
- `Knocker.prune_orphan_deliveries(...)` exists on the public Python surface.
- `prune_events(...)` accepts only `handled` / `ignored` statuses and rejects unsupported statuses.
- `prune_events(statuses=...)` rejects a bare string and rejects an empty collection.
- `older_than` is strict on `received_at`, and candidates are selected oldest-first when `limit` truncates matches.
- `limit` is required and rejected outside `1..1000`.
- `prune_events(...)` deletes linked deliveries, cascades linked attempts, removes stale `_honker_live` rows for deleted events, and returns the pinned typed summary.
- `prune_orphan_deliveries(...)` deletes only deliveries with `event_id IS NULL` and returns the pinned typed summary.
- A claimed worker job that races with explicit prune does not crash the worker; missing-event dispatch exits quietly.
- No automatic pruning, no dry-run mode, no failed/dead pruning, no Rust pruning UDFs, and no Node/admin parity land in this slice.

## Tests and evidence

- Add Python tests covering:
  - pruning old `handled` events
  - pruning old `ignored` events
  - rejecting unsupported event statuses
  - rejecting empty or badly typed `statuses`
  - orphan-delivery pruning without touching linked deliveries
  - strict `older_than` behavior
  - oldest-first selection when `limit` truncates matches
  - `limit` validation
  - stale `_honker_live` cleanup for pruned events scoped to this Knocker queue
  - zero-match prune calls returning zero summaries
  - cross-event isolation: pruning event A does not touch linked deliveries for event B
  - missing-event dispatch after prune exiting quietly instead of crashing the worker
  - all-or-nothing transaction behavior for event pruning
- For the transactionality check, prefer monkey-patching a helper or queue-cleanup step to raise mid-transaction so the test can assert no partial prune escaped the transaction, rather than relying on brittle delete-order triggers.
- Run:
  - `make test`

## Traps

- Do not add pruning UDFs to `knocker-honker` in this slice.
- Do not add schema columns such as `ignored_at`.
- Do not broaden pruning to `failed` or `dead`.
- Do not add verification-outcome pruning or conflate orphan with invalid.
- Do not add a dry-run mode, scheduler, vacuum helper, or retention automation.
- Do not mutate replay/requeue/ignore semantics while touching nearby operator code.
- Do not rely on SQLite JSON functions if simple Python payload parsing is enough; keep the implementation portable to the current runtime assumptions.

## Files likely to change

- `packages/knocker/python/knocker/_knocker.py`
- `tests/test_knocker_honker.py`
- `README.md`
- `packages/knocker/README.md`
- `.intent/phases/004-minimal-retention-and-pruning/reviews_and_decisions.md`

## Areas that should not be touched

- `knocker-honker` ingest, dedupe, replay, requeue, and event-state semantics
- schema versioning / migration code
- Node binding surface
- admin JSON / HTML work from `005`
- performance / publishability work from `006`

## Assumptions and risks

- Python-side SQL is sufficient for this explicit operator retention slice.
- The biggest implementation risk is accidental partial deletion due to manual child-row cleanup order; tests should pin the all-or-nothing behavior.
- Another risk is queue cleanup drift if `_honker_live` payload parsing is sloppy; keep the matching logic narrow and queue-scoped.
- `_stale_live_job_ids(...)` currently scans the full live queue for this Knocker queue and filters in Python; that is acceptable for this explicit v1 operator action but may need a more targeted approach later if queue size becomes large.
- Concurrent prune calls are not a supported optimization target in this minimal slice; SQLite single-writer behavior is the practical expectation.
- `failed` / `dead` retention remains a real next-step gap, but widening `004` now would slow the safe minimal slice.

## Commands

- `make test`

## Ambiguities noticed during planning

- None that require another spec diff change before implementation. The prune cutoff, candidate ordering, method shape, result shapes, and queue-cleanup responsibility are now pinned upstream in `spec-diff.md`.

## Notes

- The plan is implementation reasoning, not new intent.
- The plan should not have to make intent decisions for us.
