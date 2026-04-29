# Spec Diff: Append-Only Delivery Rows

## What changes

- Knocker adds a first-class `Delivery` noun as an append-only record of each inbound HTTP receipt.
- `Event` becomes the deduped, processable unit rather than the only durable ingress record.
- Verification outcomes move semantically onto `Delivery` rows instead of living on `Event` rows.
- Invalid-signature requests create `Delivery` rows only. They do not create `Event` rows and they do not enqueue handler work.
- Dedupe now means correlating a new `Delivery` to an existing `Event`; `Delivery` rows themselves are never deduplicated away.
- The schema version bumps from `1` to `2`. Bootstrap performs an additive migration: `knocker_deliveries` is created, existing `knocker_events` are backfilled with one synthetic delivery each, and `signature_valid` / `signature_error` are removed from `knocker_events`.
- The previous event-only dedupe model is explicitly rejected because it collapsed transport history, overloaded `ignored`, and forced history-destroying mutation to recover from invalid-first / valid-later deliveries.

## What does not change

- Knocker remains a same-process, same-SQLite-file library.
- Verification logic stays in the binding layer, not in `knocker-honker`.
- Honker remains unaware of webhook-specific verification concepts.
- Handlers still operate on `Event` rows, not on raw request objects or delivery ids.
- Replay, requeue, retry, and attempt history remain event-level concepts.
- Business idempotency is still application logic; Knocker does not add `claim_effect`, `run_once`, or other once-only side-effect primitives in this slice.

## Invariants

- Every inbound HTTP receipt becomes one `Delivery` row.
- `Delivery` rows are append-only in normal operation.
- Invalid signatures never create `Event` rows.
- The `Event` row stores the canonical body, headers, method, and query, copied from the first valid `Delivery` that caused event creation. Later deliveries linked to the same `Event` do not mutate the `Event` row.
- `ignored` returns to a single meaning: an `Event` intentionally not processed by operator or application policy.
- A single `Event` may have many `Delivery` rows linked to it.
- Dedupe only governs `Event` creation. It never erases delivery history.
- The previous “recover invalid by mutating the event row in place” path is removed.

## How we will verify it

- A valid generic HMAC request creates one `Delivery` row, one `Event` row, and one queued job.
- An invalid generic HMAC request creates one `Delivery` row, no `Event` row, and no queued job.
- A valid Stripe signature behaves the same way.
- Two deliveries with the same event identity create two `Delivery` rows linked to one `Event`.
- An invalid first delivery followed by a valid delivery with the same semantic identity produces two `Delivery` rows and one `Event` row, where the `Event` is created by the valid delivery.
- Existing worker and lifecycle tests continue to pass after shifting verification state off `Event`.
