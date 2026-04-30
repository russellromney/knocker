# Knocker System

Knocker is an embeddable inbound webhook inbox for applications that already have an HTTP server, a SQLite database, and local business logic.

## Core intent

- Knocker stores inbound webhook requests durably before returning success.
- Knocker acknowledges quickly and runs handlers later.
- Knocker runs in the same process as the host app.
- All durable Knocker state lives in the same SQLite file as the host app.
- Knocker records every inbound HTTP receipt as a `Delivery` and processes deduped `Event` rows later. Background work is a consequence of stored state, not a second source of truth.

## Boundaries

- The host app owns HTTP framework integration and business logic.
- The language binding owns request adaptation, signature verification, endpoint registration, handler dispatch, and the supported Python-first operator surface.
- `knocker-core` owns schema bootstrap, durable ingress semantics, dedupe, replay/requeue, and event state transitions.
- Honker owns claim, lease, retry, and dead-letter queue mechanics.
- Honker must not learn webhook-specific concepts.

## Non-goals

- Knocker is not a daemon, hosted service, broker, workflow engine, or distributed control plane.
- Knocker does not require Postgres, Redis, Kafka, or a second durable store.
- Knocker v1 does not run handlers inline with the inbound HTTP request.
- Knocker v1 does not promise exactly-once processing or delivery while the app is down.

## Source of truth and invariants

- `knocker_deliveries` is the append-only source of truth for inbound HTTP receipt history and verification results during normal ingest and operator inspection flows.
- The append-only `Delivery` rule is currently enforced by core code paths and tests rather than by DDL triggers.
- Explicit retention pruning is the documented exception: old linked deliveries may be deleted when operators prune terminal events, and old orphan deliveries may be pruned separately.
- `knocker_events` is the mutable source of truth for deduped processing state.
- Honker job payloads should only point at stored event ids, with an optional stored delivery id for explicit operator delivery replay.
- Ingress durability means the delivery insert, event correlation decision, optional event insert, and Honker enqueue commit together before HTTP success.
- After a Honker job has been claimed, Knocker event state and Honker job disposition must commit together in one transaction.
- A missing handler is a failure, not an `ignored` outcome.
- Invalid signatures create `Delivery` rows only. They do not create `Event` rows and they are never represented as `ignored`.
- For events created under the v2 model, `ignored` means intentionally not processed by operator or application policy.
- Migrated v1 `ignored` rows may still carry the legacy verification-failed meaning and should be interpreted with their backfilled delivery rows.
- The `Event` row stores canonical payload copied from the first valid `Delivery` that created it. Later deliveries linked to that event do not mutate the event row.
- Duplicate ingest into an existing `Event` never mutates the event row and never enqueues work. This includes duplicates for `dead` events: Knocker stores the new `Delivery`, returns the existing `event_id`, and leaves recovery to explicit operator action.
- Replay and requeue reuse the stored event row rather than minting a replacement event in v1.
- `replay(event_id)` accepts only `handled`, `failed`, `dead`, and `ignored` events. `requeue(event_id)` accepts only `failed`, `dead`, and `ignored` events.
- Explicit replay and requeue remove stale live Honker jobs for this Knocker queue before enqueueing replacement work.
- `replay_delivery(delivery_id)` is explicit operator recovery for "process this stored receipt body." It accepts only linked deliveries whose event is `handled`, `failed`, `dead`, or `ignored`, uses the specified delivery body/metadata for the handler call, and does not mutate the canonical event payload.
- The supported operator surface is Python-first and returns typed `Event` and `Delivery` objects rather than raw SQL rows.
- `list_events(...)` and `list_deliveries(...)` are newest-first by default (`received_at DESC, id DESC`) and use inclusive integer-timestamp `since` filters plus bounded `limit` values.
- `event_type` filtering is exact string equality.
- Delivery filters treat orphan status (`event_id IS NULL`) and verification outcome (`signature_valid`) as separate axes.
- `signature_valid=False` means "not true": rows with `signature_valid = 0` and rows with `signature_valid IS NULL` both match.
- Operator reads are best-effort reads of stored state, not a cross-call snapshot guarantee under concurrent worker activity.
- Ignored events are not later dispatched by the worker. If a pending Honker job is claimed after an event was ignored, the worker acknowledges the job without marking the event `processing` or invoking the handler.
- The Python operator surface now includes explicit pruning for old `handled` / `ignored` events and old orphan deliveries.
- Pruning is Python-first, explicit, and manual in v1:
  - `prune_events(...)` supports only `handled` / `ignored`, uses strict `received_at < older_than`, and selects oldest-first when `limit` truncates matches
  - `prune_orphan_deliveries(...)` follows the orphan axis (`event_id IS NULL`), not the invalid-signature axis
- Event pruning removes linked deliveries, cascades linked attempts, and removes stale `_honker_live` rows for this Knocker queue in one transaction.
- If a worker later holds a claimed job for an explicitly pruned event, dispatch treats that job as stale retention residue and exits quietly instead of crashing.
- Retention live-job cleanup treats malformed Honker payloads as non-matches rather than guessing through string coercion.
- Lifecycle UDFs fail fast when asked to mutate an unknown event id.
- Python worker state is local and non-durable. It is inspection help for host apps, not a durable control plane.
- Handler functions receive `(event, tx)`. Business writes through `tx` commit atomically with Knocker's event transition and queue disposition.
- Handlers are synchronous and should stay short and DB-local; slow outbound work belongs in app-owned follow-up jobs.

## Lifecycle

1. The host app receives an inbound request.
2. The binding maps it to a configured endpoint and verifies it if configured.
3. Knocker stores the receipt as a `Delivery` row and extracts correlation metadata.
4. If the receipt is valid and creates a new event identity, Knocker creates an `Event` row and enqueues a Honker job that references that `event_id`.
5. The host app returns a fast success response only after that transaction commits.
6. A worker in the same process claims the job later and loads the stored event.
7. The handler runs against the stored event inside a database transaction.
8. Knocker records the attempt and marks the event `handled`, `failed`, `dead`, or `ignored`.
9. Replay or requeue creates new work for the same stored event.
10. Delivery replay creates new work for the same stored event while presenting the selected delivery body to the handler.

## Current baseline

- The repo currently has the Rust core, Python binding, and Node contract smoke test.
- The Python binding exposes a public provider plugin surface (`Provider`, `ProviderRequest`, `ProviderResult`, `Knocker.provider_versions(...)`) and ships built-in curated providers for `stripe` and `github`. Built-in providers are auto-registered per `Knocker` instance and cannot be overridden.
- `add_endpoint(provider="name", secrets=[...], provider_options={...})` resolves only curated built-in names. App-local and community providers pass a `Provider` instance directly: `add_endpoint(provider=AcmeProvider(), secrets=[...])`. The string namespace is reserved for curated built-ins, so non-curated providers cannot collide with future curated additions. An instance whose `.name` matches a curated name is rejected at registration. Re-`add_endpoint(...)` with a fresh instance always rebuilds the verifier — same `.name` is not the same implementation. Providers that require secrets reject missing/`None`/empty secrets at registration. `provider_options` is schema-checked.
- Curated provider behavior is pinned by repo-owned conformance material under `providers/<name>/` (small `metadata.json` plus binding-neutral JSON `fixtures/`). Stripe fixtures use explicit clock injection so timestamp-tolerance assertions stay stable over time. The catalog is repo-only conformance material; runtime plugin loading and cross-language code generation remain deferred.
- The Honker queue object is internal; the public inspection surface is `Knocker.queue_name`. Honker job payload helpers live in `knocker.job_payload`; `knocker.coercion` is scoped to generic argument coercion.
- Provider implementations return both verification outcome and extracted metadata in one `ProviderResult`. Invalid receipts still surface extracted provider metadata on the orphan delivery row when the provider was able to read it before signature failure.
- Unexpected provider exceptions become invalid/orphan deliveries with a useful `signature_error`; a buggy app-local provider does not crash callers.
- Generic HMAC verification stays available via the legacy `verification={"kind": "hmac-sha256", ...}` config path because its per-provider knobs do not fit the curated catalog. Legacy `verification={"kind": "stripe", ...}` continues to work and routes through the same Stripe provider implementation.
- Explicit `receive(...)` metadata arguments still override provider-extracted metadata, and endpoint `delivery_key` / `event_key` callables still override provider extraction.
- The Python API now exposes a stable operator surface for `get_event(...)`, `list_events(...)`, `get_delivery(...)`, `list_deliveries(...)`, `ignore(...)`, `replay(...)`, `requeue(...)`, `replay_delivery(...)`, `prune_events(...)`, and `prune_orphan_deliveries(...)`.
- The Python worker exposes local `worker_states()` snapshots and optional `on_error` callbacks for worker-loop failures outside normal handler retry/dead-letter handling.
- The Python binding validates operator timestamps and limits as integers, and validates Stripe tolerance windows as non-negative integers.
- Automatic retention jobs, richer retention policy, native/WASM provider loading, and admin endpoints are not yet part of the baseline.
