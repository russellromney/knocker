Read `~/.claude/CLAUDE.md` and project docs.

Implement the schema split that adds append-only `Delivery` rows and makes `Event` the deduped processing unit.

## Context

- Current baseline commit: `cf6a7b5` (`Build Knocker foundation on Honker`).
- The current model stores ingress, verification result, and processing state on a single `knocker_events` row.
- The current verified-ingress slice proved useful, but it exposed a deeper model problem: the only way to recover an invalid-first / valid-later retry was to mutate the existing event row in place and overwrite history.
- Knocker needs to preserve transport receipts separately from event processing state.
- Verification logic in the Python binding is still good and should survive this shift; the problem is where receipt facts are stored and how dedupe is modeled.

## References (read first)

- [.intent/phases/002-append-only-delivery-rows/spec-diff.md](/Users/russellromney/Documents/Github/knocker/.intent/phases/002-append-only-delivery-rows/spec-diff.md): intended change
- [knocker_v_1_design.md](/Users/russellromney/Documents/Github/knocker/knocker_v_1_design.md): original product intent
- [SYSTEM.md](/Users/russellromney/Documents/Github/knocker/SYSTEM.md): current English model to update after the code lands
- [knocker-core/src/lib.rs](/Users/russellromney/Documents/Github/knocker/knocker-core/src/lib.rs): current schema bootstrap
- [knocker-core/src/knocker_ops.rs](/Users/russellromney/Documents/Github/knocker/knocker-core/src/knocker_ops.rs): current ingest and event transition contract
- [packages/knocker/python/knocker/_knocker.py](/Users/russellromney/Documents/Github/knocker/packages/knocker/python/knocker/_knocker.py): current verified-ingress API
- [tests/test_knocker_core.py](/Users/russellromney/Documents/Github/knocker/tests/test_knocker_core.py): existing lifecycle and verification tests

## Decisions

### User knobs

- `provider="stripe"` or other provider preset fills in default verification plus default `delivery_key` and `event_key` extractors from provider conventions.
- Custom verification is configured with `verification=...` objects in the binding.
- Custom correlation is configured with `delivery_key=...` and `event_key=...` callables. These override provider-preset extractors.
- Multiple active secrets per endpoint are supported for rotation.

### Not knobs

- Storage follows pure `(a)`: `Event` stores canonical payload copied from the first valid `Delivery` that created it. Later deliveries do not mutate the event row.
- Invalid deliveries are always stored as `Delivery` rows with `event_id = NULL`. This is not configurable.
- Handlers are event-shaped in v1. There is no per-delivery handler mode and no `process_mode` flag in this slice.
- `Event.signature_valid` and `Event.signature_error` are removed entirely. Verification truth lives on `Delivery`.
- The dedupe unique constraint stays on `Event(endpoint_id, dedupe_key) WHERE dedupe_key IS NOT NULL`. `Delivery` rows do not have a dedupe uniqueness constraint.
- `provider_event_id`, `provider_delivery_id`, and `event_type` live on `Delivery` always and are copied to `Event` at creation for indexing and querying. Later deliveries do not update the copied event-level values.

### Migration

- `schema_version` bumps from `1` to `2`.
- Bootstrap performs the `v1 -> v2` migration.
- Migration creates `knocker_deliveries`, backfills one synthetic `Delivery` per existing `Event`, and then removes event-level verification fields that no longer belong on `Event`.

## Scope

1. Add `knocker_deliveries` to the Rust schema bootstrap.
   - One row per inbound HTTP receipt.
   - Store raw request data and verification result there.
   - Add an optional `event_id` foreign key so a `Delivery` can link to an `Event` when correlation succeeds.

2. Slim `knocker_events` back toward the processable business unit.
   - Remove verification semantics from the event model.
   - Keep canonical payload copied from the first valid `Delivery` that created the `Event`.
   - Keep the existing event state machine for processing attempts.

3. Rewrite `knocker_ingest(...)`.
   - Decide verification and event correlation before inserting the `Delivery` row.
   - If verification fails: insert one `Delivery` row with `event_id = NULL`, return `401`, create no `Event`, enqueue nothing.
   - If verification passes and no matching `Event` exists: create an `Event`, then insert one `Delivery` row linked to that new `Event`, enqueue once.
   - If verification passes and a matching `Event` exists: insert one `Delivery` row linked to the existing `Event`, return duplicate, enqueue nothing.
   - Delete the current “recover invalid by mutating the event row” code path.

4. Preserve and adapt the Python verified-ingress surface.
   - Keep endpoint-level verification config in the binding.
   - Keep `receive(...)` and low-level `ingest(...)` if they still make sense after the schema split.
   - Remove `signature_valid` and `signature_error` from the public Python `Event` type and update tests accordingly.
   - Support provider presets plus explicit `verification=...`, `delivery_key=...`, and `event_key=...` overrides.

5. Add minimal read helpers for auditability.
   - `knocker_get_delivery(delivery_id)` or equivalent
   - `knocker_list_deliveries(...)` or equivalent
   Keep handler APIs event-shaped; do not expose deliveries as the normal business callback primitive.

6. Migrate tests to the new model.
   - valid receipt -> one delivery, one event, one queue row
   - invalid receipt -> one delivery, zero events, zero queue rows
   - multiple valid deliveries with same semantic key -> many deliveries, one event
   - invalid first / valid later -> many deliveries, one event created by the valid receipt
   - no Python code or tests rely on `Event.signature_valid` / `signature_error`
   - existing event worker behavior still passes

7. Update docs after code lands.
   - `SYSTEM.md`
   - `README.md`
   - `CHANGELOG.md`

## Traps

- Do not add a `Delivery` state machine.
- Do not deduplicate away `Delivery` rows.
- Do not keep using `ignored` for verification-failed ingress.
- Do not expose `delivery_id` as the main handler primitive.
- Do not add business idempotency primitives like `claim_effect` or `run_once` in this slice.
- Do not add `process_mode`, per-delivery dispatch, or "store invalid deliveries?" as configuration knobs.
- Do not move signature verification into Rust or SQLite UDFs.
- Do not widen the change into retention, admin UI, or workflow-like behavior.
- Do not keep the current in-place event mutation recovery path alive “just in case.”

## Acceptance

- `make test` passes.
- Rust tests prove `Delivery` rows are append-only ingress facts and invalid receipts do not create events.
- Python tests prove verified ingress still works for generic HMAC and Stripe under the new schema.
- Tests prove duplicate deliveries are observable as distinct deliveries even when they map to one event.
- Tests prove invalid-first / valid-later correlation no longer requires mutating an event row in place.

## Out of scope

- GitHub and Slack verification
- durable secret storage in SQLite
- effect/idempotency guard primitives
- admin UI or retention rules
- delivery reassignment between events
- node binding changes beyond keeping shared contract smoke tests green
- worker concurrency, pause/resume, and visibility-timeout controls

## Commands

```bash
source ~/.zshrc
make test
```

## Report

Summarize:

- the final `Delivery` vs `Event` schema split
- how ingress now behaves for invalid, first-valid, and duplicate-valid deliveries
- what parts of the prior verification slice survived unchanged
