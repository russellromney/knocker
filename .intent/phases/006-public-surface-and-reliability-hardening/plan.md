# Plan

Phase:
- 006-public-surface-and-reliability-hardening

Session:
- A

## Goal

- Resolve every actionable item from the intensive code review before `0.1.0`: public documentation, fail-fast hardening, worker affordances, `replay_delivery(...)`, comprehensive seam tests, and standards-compliant file sizes.

## Phase decisions

- `add_endpoint(...)` is the only endpoint registration method. Remove `endpoint(...)` and update tests/docs.
- Public Python docstrings are required for:
  - `IngestResult`
  - `Event`
  - `Delivery`
  - `PruneEventsResult`
  - `PruneDeliveriesResult`
  - `Knocker`
  - `Knocker.add_endpoint`
  - `Knocker.add_handler`
  - `Knocker.handle`
  - `Knocker.ingest`
  - `Knocker.receive`
  - `Knocker.get_event`
  - `Knocker.list_events`
  - `Knocker.get_delivery`
  - `Knocker.list_deliveries`
  - `Knocker.ignore`
  - `Knocker.replay`
  - `Knocker.requeue`
  - `Knocker.replay_delivery`
  - `Knocker.prune_events`
  - `Knocker.prune_orphan_deliveries`
  - `Knocker.run_worker`
- Handler documentation must say handlers receive `(event, tx)` and business writes through `tx` commit atomically with Knocker's event transition and queue ack.
- Handler documentation must say handlers are synchronous and should stay short; slow outbound work belongs in app-owned follow-up jobs.
- Lifecycle UDFs must return an error if their `UPDATE knocker_events ... WHERE id=?` matches zero rows.
- Worker state is local and non-durable. It should expose enough for host apps to inspect a running worker, not enough to become an admin plane.
- `on_error` is for worker-loop failures outside normal handler retry/dead-letter handling. Handler exceptions remain governed by Knocker retry/dead-letter semantics.
- `replay_delivery(delivery_id)` is explicit and operator-only:
  - requires the delivery to be linked to an event
  - rejects orphan deliveries
  - rejects unknown deliveries
  - uses the specified delivery's body, headers, query, event type, and provider metadata for the handler call
  - records the attempt against the linked event
  - does not mutate the canonical event payload or provider metadata
  - uses the normal queue/job accounting path where practical, or an explicitly tested equivalent if direct operator execution is simpler
- Test reorganization must be behavior-preserving. Do not combine it with runtime semantics in the same commit if avoidable.

## Build order

1. Update docs first so the intended public shape is explicit:
   - README links to `knocker.dev`
   - getting-started guide shows `(event, tx)` business write pattern
   - operator guide lists accepted replay/requeue statuses
   - verified-ingress/reference docs warn that `ingest(...)` is trusted low-level ingress
   - worker guide or reference section documents short synchronous handlers and restart harness expectations
2. Add public Python docstrings while the module is still in one file:
   - keep docstrings concise and IDE-friendly
   - avoid duplicating entire docs-site pages
3. Remove `Knocker.endpoint(...)`:
   - update tests to call `add_endpoint(...)`
   - update docs if any alias references remain
4. Harden Rust lifecycle UDFs:
   - check affected row count for processing/handled/failed/ignored updates
   - return a SQLite error on unknown event id
   - add Rust tests and Python integration coverage where useful
5. Add worker affordances:
   - introduce a typed worker state snapshot
   - track current event id during dispatch
   - track last worker-loop error
   - support optional `on_error` callback in `run_worker(...)`
   - add tests for state transitions and callback invocation
6. Implement `replay_delivery(delivery_id)`:
   - decide whether it enqueues a synthetic replay job or executes directly through a shared dispatch helper
   - preserve normal attempt/ack/fail consistency
   - reject unknown, orphan, and invalid unsupported delivery cases with explicit errors
   - test that canonical event payload remains unchanged
7. Add missing real-infrastructure tests:
   - concurrent same-dedupe ingest
   - long handler exceeds visibility timeout and is reclaimed
   - WAL wakeup wakes idle worker after commit without waiting for the full idle poll interval
   - replay racing with mid-handler worker rolls back stale ack path and leaves one committed handling
   - Python-entrypoint v1-to-v2 migration
   - concurrent prune calls serialize safely
   - burst ingest plus worker drain smoke
8. Split Python implementation files:
   - `models.py`
   - `verifiers.py`
   - `providers.py`
   - `coercion.py`
   - `queue.py`
   - `_knocker.py` for the public `Knocker` class and orchestration
   - keep `knocker.__init__` exports stable
9. Split tests by behavior:
   - `test_ingest.py`
   - `test_verification.py`
   - `test_worker.py`
   - `test_operator_reads.py`
   - `test_recovery.py`
   - `test_pruning.py`
   - `test_migration.py`
10. Run verification and update phase evidence.

## Verification

- `make test`
- `npm --prefix site run build`
- `wc -l packages/knocker/python/knocker/*.py tests/*.py`
- Public docstring smoke:
  - `uv run --group dev python - <<'PY'`
  - import `knocker`
  - assert public classes/methods have non-empty `__doc__`
  - `PY`
- Test inventory should show the added real-infrastructure tests by name.

## Traps

- Do not add a built-in admin server.
- Do not make worker state durable.
- Do not make `replay_delivery(...)` automatic provider-redelivery behavior.
- Do not mutate canonical `Event` payload while implementing `replay_delivery(...)`.
- Do not make handler execution async in this phase.
- Do not use mocks for the concurrency/recovery tests.
- Do not split files and change behavior in the same step unless the change is mechanically required by the split.
- Do not let the refactor drop Node smoke coverage.
