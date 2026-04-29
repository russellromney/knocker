# Spec Diff: Operator Read Surface

## What changes

- Knocker adds a supported operator-facing read surface for stored `Event` and `Delivery` data.
- `002`'s existing Python read helpers (`list_events`, `get_event`, `list_deliveries`, `get_delivery`) are promoted from incidental helpers to the supported operator API for v1. This slice may refine them, but it does not replace them with a new namespace.
- This slice chooses the Python-first path for the operator surface. `003` does not add a separate cross-binding operator API contract in `knocker-core`.
- Default list ordering changes from the earlier insertion-order shape to newest-first (`received_at DESC, id DESC`); existing `002` helper names stay the same, but callers should now treat list ordering as an operator-surface behavior of `003`.
- Operators can list and inspect:
  - `Event` rows by status, endpoint, exact `event_type` string, and recency
  - `Delivery` rows by linked `event_id`, endpoint, verification outcome, orphan status, and recency
- Recency means newest-first ordering by `received_at DESC, id DESC`, with an inclusive `since` filter (`received_at >= since`), a default limit of `100` rows, and a maximum limit of `1000`. Empty-filter list calls return the latest bounded rows across the full set. This slice does not add cursor pagination.
- Delivery verification-outcome filtering is a first-class operator capability, not just a test convenience.
- Replay, requeue, and ignore remain the existing event-level recovery actions. This slice documents and tests them as part of the supported operator surface rather than moving or reshaping them.
- Invalid receipts remain visible through the operator surface as orphan `Delivery` rows with `event_id = NULL`.
- DST-sensitive calendar semantics remain out of scope. This slice stays on integer timestamp filters only; deterministic simulation testing for local-day / DST behavior belongs in a later phase.

## What does not change

- Knocker remains a same-process, same-SQLite-file library.
- Handlers remain event-shaped; operators do not dispatch handlers directly from deliveries.
- The `Delivery` / `Event` schema split from `002` does not change.
- Verification logic remains in the binding layer.
- This phase does not add retention rules, HTML admin UI, or a hosted control plane.
- This phase does not add business idempotency helpers or per-delivery processing modes.
- This phase does not add new event state transitions beyond the existing replay, requeue, and ignore actions.
- This phase does not promise operator-surface parity across Node or future bindings.

## Invariants

- "Supported" means documented, tested, and typed public operator APIs whose method names and return shapes do not change in later slices without an explicit deprecation or replacement decision in a future spec diff.
- Operator-facing reads return typed `Event` and `Delivery` objects, not raw SQL-row dicts or internal column names.
- The Python binding is the supported operator surface for v1. Any `knocker-core` query helpers added in this slice exist to support that surface rather than to define a second promised operator API.
- `event_type` filtering uses exact string equality. This slice does not add normalization, case folding, or fuzzy matching.
- Orphan deliveries and invalid deliveries are separate concepts in the operator surface, even though invalid deliveries currently appear as orphan deliveries. Both filter axes remain available.
- Operator reads are best-effort reads of stored state, not a cross-call snapshot guarantee under concurrent worker activity.
- `signature_valid=False` means "not true" in the operator surface: deliveries with `signature_valid = 0` and deliveries with `signature_valid IS NULL` both match that filter.
- `ignore` prevents later handler dispatch for the ignored event. If a pending Honker job is claimed after an event was ignored, the worker acknowledges the job without marking the event `processing` or invoking the handler.

## How we will verify it

- Operators can list recent events with default-bounded results and filter them by status, endpoint, exact `event_type`, and `since` / `limit` recency parameters.
- Operators can inspect one event and see its linked deliveries.
- Operators can list deliveries by `event_id`, by verification outcome, and by orphan status without handwritten SQL.
- Invalid-signature deliveries remain visible and distinguishable from merely orphaned deliveries.
- Replay, requeue, and ignore continue to work through the existing event-level operations while appearing as part of the supported operator surface.
- The Python operator APIs stay consistent with the underlying stored state after worker activity, failed deliveries, and invalid-signature ingress.
- Existing `002` helper call sites continue to work through the stabilized API surface rather than being replaced by a new namespace.

## Notes

- The purpose of this slice is to make the Delivery/Event model operable, not just internally correct.
