# Changelog

## Unreleased

### Added

- Added a public Python provider plugin surface: `Provider`, `ProviderRequest`, `ProviderResult`, `Knocker.register_provider(...)`, and `Knocker.provider_versions(...)`. App-local and community providers register on a single `Knocker` instance and use the same interface as built-in curated providers.
- Added a built-in curated `github` provider that verifies `X-Hub-Signature-256` (`sha256=<hex hmac>`), extracts `X-GitHub-Delivery` and `X-GitHub-Event`, and uses the delivery id as the dedupe identity (stable across GitHub dashboard redelivery).
- Added `add_endpoint(..., provider_options={...})` for provider-specific tuning such as Stripe `tolerance_s`. Unknown option keys are rejected with `ValueError`.
- Added a comprehensive `tests/test_providers.py` covering the registry surface, GitHub valid/invalid/missing-header/missing-delivery cases, app-local providers, orphan-delivery-with-metadata invariants, provider exception safety, and compatibility paths.
- Added a no-matching-handler regression test for `replay_delivery(...)` so a delivery whose `event_type` does not match a registered handler dead-letters predictably.
- Added a `replay_delivery(...)` test that exercises an actively-running worker so synthetic replay jobs are picked up without restarting the worker loop.
- Added an async `on_error` callback test for `run_worker(...)` that also pins the user-raise-shadows-original-exception path.
- Added two multi-worker isolation tests confirming concurrent workers maintain independent `WorkerState` and that one worker's failure does not contaminate another's terminal state.
- Added an operator runbook covering dead events, invalid/orphan deliveries, worker failures, replay/requeue/replay-delivery recovery, and pruning.

### Changed

- The Stripe verified-ingress path now routes through the new provider registry without any intentional behavior change. Existing `provider="stripe", secrets=[...]` and `verification={"kind": "stripe", ...}` configurations continue to work, including secret rotation and timestamp tolerance.
- `provider="github"` is now a curated built-in provider rather than a metadata-only preset. Endpoints using it must pass `secrets=[...]`; verification, delivery id extraction, and event type extraction now all come from the provider implementation.
- `add_endpoint(provider="name", ...)` resolves through the per-`Knocker` provider registry. Unknown provider names fail at registration. Built-in providers that require secrets reject missing/`None`/empty `secrets=...` at registration so misconfigured endpoints can no longer silently accept all deliveries.
- Built-in provider names cannot be overridden via `register_provider(...)`; an explicit override knob is intentionally deferred. Adding a new built-in provider name is now called out as a compatibility-affecting release because it can collide with an app-local/community provider of the same name.
- Provider implementations return both verification outcome and extracted metadata in one `ProviderResult`. Invalid receipts retain extracted provider metadata on the orphan delivery row when the provider was able to read it before signature failure.
- An unexpected exception inside `Provider.verify(...)` is now turned into a verification failure with a useful `signature_error` and an orphan delivery, rather than crashing the caller.
- `ProviderRequest.json()` raises `ValueError` on non-JSON bodies and caches its parsed result; provider authors should guard with `try/except` for endpoints that may receive non-JSON payloads.
- Documented that `replay(...)`, `requeue(...)`, and `replay_delivery(...)` reset `attempt_count` to `0` and restart the dead-letter clock.
- Documented that `replay_delivery(...)` resolves handlers using the selected delivery's `event_type`, not the canonical event's.
- Documented that `run_worker(on_error=...)` runs the callback before re-raising, so a user raise in `on_error` shadows the original worker-loop exception; coroutines are awaited.
- Added an explanatory comment in `_event_from_delivery` describing the canonical-event-identity plus delivery-payload synthesized handler input.
- Updated `ROADMAP.md` post-release backlog to reflect that PyPI publication, Linux/macOS wheels, and the tag-driven release workflow are in place; Windows wheels and PyPI trusted publishing remain queued.

## 0.1.0 - 2026-04-29

### Added

- Added the `knocker-core` Rust core for idempotent bootstrap, durable ingest, dedupe, replay/requeue, and event lifecycle transitions.
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
- Added GitHub Actions CI plus a tag-driven Python release workflow for `knockerlite` wheels and source distributions.

### Changed

- Renamed the planned Python distribution to `knockerlite` while keeping the import package as `knocker`.
- Moved durable Knocker semantics out of the Python wrapper and into the shared `knocker-core` crate.
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
- Reframed framework integration as documented host-app route glue instead of a shipped adapter package.

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
