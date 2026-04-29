# Changelog

## Unreleased

### Added

- Added the `knocker-honker` Rust core for idempotent bootstrap, durable ingest, dedupe, replay/requeue, and event lifecycle transitions.
- Added the Python binding with a thin runtime wrapper over the shared Rust / SQLite contract.
- Added a loadable SQLite extension and a small Node smoke-test binding to pressure-test the cross-language contract.
- Added `SYSTEM.md` plus the initial `.intent/` change record for the current Knocker baseline.
- Added binding-owned verified ingress for generic HMAC-SHA256 and Stripe.
- Added overlapping active-secret support in endpoint configuration for rotation.
- Added append-only `Delivery` rows to preserve every inbound HTTP receipt.
- Added Python `Delivery` reads via `get_delivery(...)` and `list_deliveries(...)`.
- Added provider-preset metadata extraction for Stripe and GitHub in the Python binding.
- Added a stable Python operator surface for `list_events(...)`, `get_event(...)`, `list_deliveries(...)`, `get_delivery(...)`, and `ignore(...)`.
- Added a minimal Python-first pruning surface with `prune_events(...)`, `prune_orphan_deliveries(...)`, and typed prune summary results.
- Added an Astro/Starlight docs site for `knocker.dev`.
- Added the Knocker logo assets to the repo and docs site.
- Added `replay_delivery(delivery_id)` for explicit operator replay of one stored delivery body without mutating the canonical event payload.
- Added local Python worker state snapshots and an optional `on_error` callback for worker-loop failures outside normal handler retry/dead-letter handling.

### Changed

- Moved durable Knocker semantics out of the Python wrapper and into the shared `knocker-honker` core.
- Clarified the project docs around the Knocker / Honker boundary, current implementation status, and remaining work.
- Added a high-level `receive(...)` path while preserving the lower-level `ingest(...)` primitive.
- Split the durable ingress model into append-only `Delivery` rows plus deduped `Event` rows.
- Invalid verified ingress now stores orphan deliveries instead of creating ignored events.
- The canonical event payload now comes from the first valid delivery that created the event and is not mutated by later duplicate deliveries.
- The shared schema now bootstraps and migrates from version `1` to `2`, backfilling one synthetic delivery per existing event.
- Stabilized the Python operator helpers in place instead of replacing them with a new namespace.
- Changed default event and delivery list ordering to newest-first (`received_at DESC, id DESC`) with inclusive `since` filtering and bounded `limit` values.
- `ignore(...)` now prevents later worker dispatch for the ignored event, even if a previously queued Honker job is claimed after the ignore.
- Explicit retention pruning now deletes old `handled` / `ignored` events plus their linked deliveries and attempts, and can separately prune old orphan deliveries.
- `replay(...)` and `requeue(...)` now validate accepted event statuses and remove stale live jobs for the same event and queue before enqueueing replacement work.
- Duplicate ingest into an existing event, including a `dead` event, remains audit-only: it stores the new delivery and does not mutate event state or enqueue work.
- Public Python classes and methods now carry concise docstrings, and the README/docs explain the `(event, tx)` atomic handler contract.
- Split the Python binding and test suite into smaller files under the project line-count standard.

### Fixed

- Missing handlers now fail loudly and dead-letter the stored event instead of silently marking it `ignored`.
- Honker `ack` / `retry` / `fail` transition failures now abort the worker transaction so Knocker event state cannot commit out of sync with queue state.
- `signature_valid=False` delivery filters now include rows whose verification state is `NULL`, not only rows with `signature_valid = 0`.
- Claimed jobs for explicitly pruned events now exit quietly instead of crashing worker dispatch on missing-event lookups.
- Worker dispatch now reads the event inside the work transaction and no longer swallows unrelated `KeyError` bugs as missing events.
- Python operator `limit` validation now rejects floats and strings instead of coercing them.
- Stripe verification config now rejects bool, non-integer, and negative `tolerance_s` values.
- Retention live-job cleanup now leaves malformed Honker payloads alone instead of attempting broad string coercion.
- Lifecycle UDFs now fail fast when asked to mutate an unknown event id.
- Worker stop now responds promptly while idle instead of waiting for a long poll timeout.
