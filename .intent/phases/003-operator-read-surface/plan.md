# Plan

Phase:
- 003 operator-read-surface

Session:
- A

## Goal

- Turn `002`'s incidental Python read helpers into a documented, tested, stable operator surface for listing and inspecting stored `Event` and `Delivery` data, while keeping `replay`, `requeue`, and `ignore` as the existing event-level recovery actions.

## Context

- `002` made the Delivery/Event model internally correct, but the read surface is still thin and ad hoc:
  - `get_event`, `list_events`, `get_delivery`, and `list_deliveries` exist only in Python
  - filters are minimal
  - ordering and bounds are not aligned with the new spec
- The spec diff now pins the intent decisions that were previously loose:
  - Python-first supported surface
  - stabilize the current helper names rather than inventing a new namespace
  - newest-first `since` / `limit` recency model
  - exact `event_type` matching
  - typed `Event` / `Delivery` returns, not raw rows
  - best-effort reads under concurrent worker activity
- DST-sensitive operator affordances are deferred. This slice uses stored integer timestamps and `since` semantics only; calendar/day-window semantics can come later.
- DST-aware operator windows are deferred specifically to a later deterministic simulation testing slice. `003` stays on integer timestamp filters only.

## References

- `SYSTEM.md`
- `.intent/phases/003-operator-read-surface/spec-diff.md`
- `.intent/phases/003-operator-read-surface/reviews_and_decisions.md`
- `packages/knocker/python/knocker/_knocker.py`
- `knocker-honker/src/knocker_ops.rs`
- `tests/test_knocker_honker.py`

## Mapping from spec diff to implementation

- "stabilize `002`'s existing Python helpers" means:
  - keep `Knocker.get_event`
  - keep `Knocker.list_events`
  - keep `Knocker.get_delivery`
  - keep `Knocker.list_deliveries`
  - document and test those exact entrypoints as the supported v1 surface
- "operators can list and inspect by status, endpoint, exact event_type, recency, verification outcome, and orphan status" means:
  - expand `list_events(...)` filters beyond `status`
  - expand `list_deliveries(...)` filters beyond `event_id`
  - add newest-first ordering plus `since` / `limit`
- "typed returns, not raw rows" means:
  - keep returning `Event` and `Delivery` dataclasses
  - do not leak SQL column names or unparsed JSON blobs
- "Python-first surface" means:
  - implement the stable operator contract in Python
  - do not add new Rust read UDFs or a second operator contract in this slice
- "existing recovery actions remain grouped with the operator surface" means:
  - keep `replay(...)` and `requeue(...)` where they are
  - add `ignore(...)` to the Python public surface
  - document/test these as the supported operator recovery actions

## Phase decisions

- The supported operator surface for `003` is Python-only. Node remains out of scope.
- This slice will not add a new `operator` namespace; it stabilizes the current `Knocker` methods in place.
- Read queries remain implemented in Python SQL for this slice.
- Event lists and delivery lists default to newest-first with `limit=100`, capped at `1000`.
- `since` is an inclusive integer timestamp lower bound (`received_at >= since`), not a human calendar or timezone-aware range helper.
- No DST-aware date math, local-day bucketing, or "today/yesterday" filters land in this slice.
- `event_type` filtering is exact string equality.
- Empty-filter list calls return the latest bounded rows across the full set.
- All non-default list filters compose with `AND`.
- Newest-first ordering means `ORDER BY received_at DESC, id DESC`.
- Delivery filters should distinguish:
  - `signature_valid`
  - orphan status (`event_id IS NULL`)
  These axes may overlap but are not the same concept.
- `signature_valid` and `orphaned` use `Optional[bool]` filters:
  - `None` means no filter
  - `True` means filter for matching rows
  - `False` means filter for non-matching rows
- For `signature_valid`, `False` means "not true": rows with `signature_valid = 0` and rows with `signature_valid IS NULL` both match.
- `Knocker.ignore(event_id)` is part of the supported Python surface in this slice.
- Public `ignore(event_id)` accepts events in `received`, `failed`, or `dead`, is a no-op for already-`ignored` events, and rejects `processing` or `handled` events.
- If a pending Honker job is later claimed for an already-ignored event, the worker acknowledges the claim and does not dispatch the handler.

## Proposed implementation approach

- Expand the Python read methods rather than replacing them:
  - `list_events(status=None, endpoint=None, event_type=None, since=None, limit=100)`
  - `list_deliveries(event_id=None, endpoint=None, signature_valid=None, orphaned=None, since=None, limit=100)`
- Keep `get_event(...)` and `get_delivery(...)` unchanged in signature and typed return shape.
- Add `ignore(event_id)` to the Python public surface, backed by the existing core lifecycle function plus Python-side status validation.
- Make `_dispatch_job(...)` short-circuit ignored events by acknowledging the claimed Honker job without moving the event back to `processing`.
- Add focused tests for:
  - default newest-first ordering
  - `since` filtering
  - endpoint and exact `event_type` filters
  - verification-outcome filtering
  - orphaned-delivery filtering distinct from verification-outcome filtering
  - `ignore`, `replay`, and `requeue` remaining event-level operations
- Update docs so the supported operator surface is explicit and matches the stabilized Python API.

## Build order

1. Add the Session A intent response to `reviews_and_decisions.md`.
2. Expand the Python operator read methods to match the spec:
   - add filters
   - add newest-first ordering
   - add default/explicit limits
3. Add `ignore(...)` to the Python public surface.
4. Add or tighten tests for the three operator read cases:
   - event listing and filtering
   - delivery listing and filtering
   - event-level recovery actions
5. Update README/package docs to describe the supported operator surface.
6. Run the full test suite and capture evidence.

## Acceptance

- `Knocker.list_events(...)` supports status, endpoint, exact `event_type`, `since`, and `limit`.
- `Knocker.list_deliveries(...)` supports `event_id`, endpoint, verification outcome, orphan status, `since`, and `limit`.
- List methods are newest-first by default, use `received_at DESC, id DESC`, treat `since` inclusively, compose filters with `AND`, and are bounded by default.
- `limit` defaults to `100` and rejects values outside `1..1000`.
- `Knocker.get_event(...)` and `Knocker.get_delivery(...)` continue to return the same typed dataclasses and method signatures.
- `replay(...)`, `requeue(...)`, and `ignore(...)` are the supported recovery actions and remain event-level.
- `ignore(...)` accepts `received`, `failed`, and `dead`, is idempotent on `ignored`, and rejects `processing` / `handled`.
- An ignored event is not later dispatched by a worker, even if a previously queued Honker job is claimed after the ignore.
- No new namespace, no Node operator parity, no cursor pagination, and no DST-sensitive date filtering land in this slice.

## Tests and evidence

- Add Python tests covering:
  - `list_events` filtering by status, endpoint, exact `event_type`
  - `list_events` `since` / `limit` behavior, inclusive `since`, and newest-first stable ordering
  - `list_deliveries` filtering by `event_id`, endpoint, `signature_valid`, orphan status
  - `signature_valid=False` including `NULL` verification-state rows
  - distinction between invalid deliveries and merely orphaned deliveries where representable
  - empty-filter behavior returning the latest bounded rows
  - default event-list limit capping results at `100`
  - `ignore(...)` behavior, including valid prior statuses and rejection cases
  - ignored received-events remaining ignored when a worker later claims the pending Honker job
  - `replay(...)` and `requeue(...)` still working through the supported surface
  - existing `002` helper call sites continuing to pass without a new namespace
- Run:
  - `make test`
- Best-effort read consistency remains a documented contract in this slice rather than a race-tested invariant.

## Traps

- Do not create a new `Knocker.operator.*` namespace in this slice.
- Do not add a separate cross-binding operator contract in Rust.
- Do not return raw SQL rows or partially parsed JSON blobs from the operator API.
- Do not add new event state transitions or operator-only status mutations.
- Do not add cursor pagination.
- Do not add timezone-aware or DST-aware calendar filters.
- Do not silently broaden `event_type` matching beyond exact equality.

## Files likely to change

- `packages/knocker/python/knocker/_knocker.py`
- `tests/test_knocker_honker.py`
- `README.md`
- `packages/knocker/README.md`
- `.intent/phases/003-operator-read-surface/reviews_and_decisions.md`

## Areas that should not be touched

- `knocker-honker` schema and ingest semantics from `002`, unless a concrete implementation blocker proves Python-only reads are insufficient
- `knocker-honker` schema and ingest semantics from `002`
- Node binding parity
- retention/pruning logic
- admin UI or hosted control-plane work

## Assumptions and risks

- The current Python SQL queries are sufficient for a stable Python-first operator surface.
- Best-effort reads under concurrent worker activity are acceptable for v1 operator usage.
- The biggest risk is accidental scope creep into retention/admin ergonomics or a new operator namespace.
- A smaller risk is overfitting filters around current test data rather than the spec's operator stories.

## Commands

- `make test`

## Ambiguities noticed during planning

- None that require a spec diff change before implementation. The upstream intent decisions from the spec review are now pinned in the spec diff.

## Notes

- The plan is implementation reasoning, not new intent.
- This slice intentionally stabilizes and expands the current Python helper surface instead of creating a second operator contract.
